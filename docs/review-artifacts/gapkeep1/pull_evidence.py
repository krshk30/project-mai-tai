"""Own GAPKEEP1 evidence: bounded SQL, streamed log scan, no Redis/broker writes."""
import gzip
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings

ET = ZoneInfo("America/New_York")
AS_OF = datetime(2026, 10, 5, 14, 35, tzinfo=UTC)
settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
output = {"read_at": datetime.now(UTC).isoformat(), "population_as_of": AS_OF.isoformat(),
          "holds": [], "recoveries": [], "bars": {}, "entry_rows": [], "config": {},
          "entry_probes": []}
selected = {('2026-09-28', 'CLRO', '12:42'), ('2026-09-29', 'DXST', '12:45'),
            *[('2026-09-30', 'TGE', t) for t in ('11:54', '12:48', '14:36', '15:27')],
            *[('2026-10-01', 'NXL', t) for t in ('11:42', '13:46')],
            *[('2026-10-02', 'AIXI', t) for t in ('11:05', '15:17')],
            *[('2026-10-02', 'AMOD', t) for t in ('11:22', '12:26', '15:43')]}
for key in ("strategy_schwab_1m_v2_atr_flip_period", "strategy_schwab_1m_v2_atr_flip_factor",
            "strategy_schwab_1m_v2_gap_hold_enabled", "strategy_schwab_1m_v2_gap_hold_detect_seconds"):
    output["config"][key] = getattr(settings, key)

probe = {}
active = {}
files = sorted(Path("/var/log/project-mai-tai").glob("schwab-1m-v2.log*"),
               key=lambda p: "zzzz" if p.name == "schwab-1m-v2.log" else p.name)
output["log_files"] = [str(p) for p in files]
for path in files:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", errors="replace") as handle:
        for line in handle:
            if not any(m in line for m in ("[V2-GAP-", "[V2-ATR-PROBE]", "[V2-WS-")):
                continue
            try:
                at = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
            except ValueError:
                continue
            if at > AS_OF:
                continue
            if "[V2-ATR-PROBE]" in line:
                sym = re.search(r"sym=(\S+)", line).group(1)
                p = {"at": at, "line": line.strip(), "source": str(path)}
                bar_ms = re.search(r"ts_ms=(\d+)", line)
                if bar_ms:
                    bar_et = datetime.fromtimestamp(int(bar_ms.group(1)) / 1000, UTC).astimezone(ET)
                    if (str(bar_et.date()), sym, bar_et.strftime('%H:%M')) in selected:
                        output['entry_probes'].append(p)
                if sym in active and "first_after" not in active[sym]:
                    active[sym]["first_after"] = p
                probe[sym] = p
            elif "[V2-GAP-HOLD]" in line:
                sym = re.search(r"\[V2-GAP-HOLD\] (\S+)", line).group(1)
                h = {"symbol": sym, "at": at, "source": str(path), "hold_line": line.strip(),
                     "before": probe.get(sym)}
                if "reason=recovery_gap" in line:
                    output["recoveries"].append(h)
                    continue
                age = re.search(r"last_bar_age_s=([\d.]+)", line)
                h["last_bar_close"] = at - timedelta(seconds=float(age.group(1)))
                if h["before"]:
                    bar_ms = re.search(r"ts_ms=(\d+)", h["before"]["line"])
                    if bar_ms:
                        close = datetime.fromtimestamp(int(bar_ms.group(1)) / 1000 + 60, UTC)
                        if abs((close - h["last_bar_close"]).total_seconds()) < 1:
                            h["last_bar_close"] = close
                # Reproduce the reviewer's explicitly declared census bound, not a proposed timer.
                h["census_end"] = at - timedelta(seconds=10)
                output["holds"].append(h)
                active[sym] = h
            elif "[V2-GAP-RESUME]" in line:
                sym = re.search(r"\[V2-GAP-RESUME\] (\S+)", line).group(1)
                if sym in active:
                    active[sym]["resume"] = {"at": at, "line": line.strip()}
                    del active[sym]

engine = create_engine(settings.database_url)
with engine.connect() as conn:
    def query(sql, params):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = [dict(row) for row in conn.execute(text(sql), params).mappings()]
        conn.rollback()
        return rows

    for h in output["holds"]:
        if h["at"] < datetime(2026, 9, 28, tzinfo=UTC):
            continue
        sym, lo, hi = h["symbol"], h["last_bar_close"], h["census_end"]
        for table in ("market_trade_ticks", "market_capture_trades"):
            h[table] = query(f"SELECT count(*) count, count(*) FILTER (WHERE received_at<=:detect) received_by_detect, min(event_ts) first, max(event_ts) last FROM {table} WHERE symbol=:symbol AND event_ts>:lo AND event_ts<:hi",
                             {"symbol": sym, "lo": lo, "hi": hi, 'detect': h['at']})[0]
        # Recent tape endpoints allow independent evaluation of decision-time prior-print signals.
        h["schwab_endpoints"] = query("SELECT id,event_ts,received_at,price,size,raw FROM market_trade_ticks WHERE symbol=:symbol AND provider='schwab' AND event_ts>=:lo AND event_ts<=:hi ORDER BY event_ts,id LIMIT 12",
                                       {"symbol": sym, "lo": hi, "hi": h["at"]})
        h["schwab_prior_print"] = query("SELECT id,event_ts,received_at,price FROM market_trade_ticks WHERE symbol=:symbol AND provider='schwab' AND event_ts<=:hi AND event_ts>=:lo ORDER BY event_ts DESC,id DESC LIMIT 1",
                                         {"symbol": sym, "lo": lo - timedelta(minutes=5), "hi": hi})
        key = f"{h['at'].astimezone(ET).date()}:{sym}"
        if key not in output["bars"]:
            day = h["at"].astimezone(ET).date()
            start = datetime.combine(day, datetime.min.time(), ET).replace(hour=4).astimezone(UTC)
            stop = min(start + timedelta(hours=16), AS_OF)
            output["bars"][key] = query("SELECT bar_time,open_price,high_price,low_price,close_price,volume,source,position_quantity,decision_status,decision_reason,indicators FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=:symbol AND bar_time>=:lo AND bar_time<:hi ORDER BY bar_time LIMIT 1000",
                                         {"symbol": sym, "lo": start, "hi": stop})
    output["entry_rows"] = query("SELECT p.id,p.symbol,p.broker_account_name,p.entry_time,p.original_quantity,p.entry_price,p.status FROM oms_managed_positions p WHERE p.strategy_code='schwab_1m_v2' AND p.entry_time>='2026-09-28T00:00Z' AND p.entry_time<:hi ORDER BY p.entry_time LIMIT 200",
                                 {"hi": AS_OF})
print(json.dumps(output, default=str, indent=2))
