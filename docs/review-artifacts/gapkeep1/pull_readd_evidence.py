"""Read-only LINE=CHART census; no Redis reads, broker calls or application writes."""
import gzip
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 10, 5, 15, 50, tzinfo=UTC)
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
out = {"read_at": datetime.now(UTC).isoformat(), "as_of": AS_OF.isoformat(),
       "events": [], "probes": [], "bars": {}, "files": [], "query_errors": []}
markers = ("db-seed:", "watchlist updated", "[V2-GAP-HOLD]", "[V2-GAP-RESUME]",
           "[V2-ATR-BAR-GAP]", "[V2-RESTING-PLACE]", "[V2-RESTING-EH-ARM]",
           "[V2-DB-SEED-GAP]", "[V2-BOOT-HOLD]", "[V2-STREAMER-WARMED]",
           "[V2-REST-WARMED]", "[V2-ATR-SEED]", "[V2-WS-SUB]", "starting schwab")
days = set()
for path in sorted(Path("/var/log/project-mai-tai").glob("schwab-1m-v2.log*"),
                   key=lambda p: "zzzz" if p.name == "schwab-1m-v2.log" else p.name):
    opener = gzip.open if path.suffix == ".gz" else open
    info = {"path": str(path), "first": None, "last": None}
    with opener(path, "rt", errors="replace") as handle:
        for line in handle:
            if "[V2-ATR-PROBE]" not in line and not any(marker in line for marker in markers):
                continue
            try:
                at = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
            except ValueError:
                continue
            if at > AS_OF:
                continue
            info["first"] = info["first"] or at
            info["last"] = at
            day = str(at.astimezone(ET).date())
            days.add(day)
            if any(marker in line for marker in markers):
                out["events"].append({"at": at, "day": day, "line": line.strip(), "file": path.name})
            if "[V2-ATR-PROBE]" in line:
                values = dict(re.findall(r"(\w+)=([^ ]+)", line.strip()))
                ts = int(values["ts_ms"])
                # Fresh-age observations can include the final warmup bars; not proof of live phase.
                if 0 <= at.timestamp() * 1000 - ts <= 180000:
                    out["probes"].append({"at": at, "day": day, "file": path.name, **values})
            if len(out["events"]) > 200000 or len(out["probes"]) > 300000:
                raise RuntimeError("retained-log evidence cap exceeded")
    out["files"].append(info)

engine = create_engine(settings.database_url)
with engine.connect() as conn:
    def query(sql, params):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout='5s'"))
        try:
            return [dict(row) for row in conn.execute(text(sql), params).mappings()]
        finally:
            conn.rollback()

    for day in sorted(days):
        start = datetime.fromisoformat(day).replace(tzinfo=ET, hour=4).astimezone(UTC)
        end = min(start + timedelta(hours=16), AS_OF)
        if end <= start:
            continue
        try:
            rows = query("SELECT symbol,bar_time,created_at,open_price,high_price,low_price,close_price,volume,source FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND bar_time>=:lo AND bar_time<:hi ORDER BY symbol,bar_time LIMIT 50001", {"lo": start, "hi": end})
            if len(rows) > 50000:
                raise RuntimeError("daily bar cap exceeded")
            out["bars"][day] = rows
        except Exception as exc:
            out["query_errors"].append({"day": day, "error": str(exc)[:300]})
    try:
        out["reto_intents"] = query("SELECT t.created_at,a.name broker_account_name,t.intent_type,t.status,t.payload FROM trade_intents t JOIN strategies s ON s.id=t.strategy_id JOIN broker_accounts a ON a.id=t.broker_account_id WHERE t.symbol='RETO' AND s.code='schwab_1m_v2' AND t.created_at>='2026-10-05T15:00Z' AND t.created_at<:hi ORDER BY t.created_at LIMIT 40", {"hi": AS_OF})
    except Exception as exc:
        out["query_errors"].append({"query": "RETO intents", "error": str(exc)[:300]})
print(json.dumps(out, default=str, separators=(",", ":")))
