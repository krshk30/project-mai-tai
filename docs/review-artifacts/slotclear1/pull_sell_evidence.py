"""Bounded remote reads only; execute via ssh stdin without writing remote files."""
import json
from datetime import UTC, datetime
from pathlib import Path
import re

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings

CASES = {"SXTC": ("2026-10-07 17:45:00", "2026-10-07 18:45:00"),
         "DKI": ("2026-10-07 18:21:00", "2026-10-07 18:33:00")}
log_path = Path("/var/log/project-mai-tai/schwab-1m-v2.log")
logs = {symbol: [] for symbol in CASES}
with log_path.open() as stream:
    for number, line in enumerate(stream, 1):
        stamp = line[:19]
        for symbol, (start, end) in CASES.items():
            if start <= stamp < end and re.search(r"\b" + symbol + r"\b", line):
                logs[symbol].append({"line": number, "text": line.rstrip("\n")})
                if len(logs[symbol]) > 4000:
                    raise RuntimeError(f"bounded {symbol} log read exceeded 4000 rows")
engine = create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
bars = {}
with engine.connect() as conn:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(text("SET LOCAL statement_timeout='3s'"))
    for symbol, (start, end) in CASES.items():
        rows = [dict(row) for row in conn.execute(text(
            "SELECT symbol, bar_time, created_at, open_price, high_price, low_price, "
            "close_price, volume, source FROM strategy_bar_history "
            "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=:symbol "
            "AND bar_time >= :start AND bar_time < :end ORDER BY bar_time LIMIT 121"
        ), {"symbol": symbol, "start": start + "+00", "end": end + "+00"}).mappings()]
        if len(rows) > 120:
            raise RuntimeError(f"bounded {symbol} bar read exceeded 120 rows")
        bars[symbol] = rows
    conn.rollback()
print(json.dumps({"read_at_utc": datetime.now(UTC).isoformat(), "log_path": str(log_path),
                  "windows_utc_inclusive_exclusive": CASES, "logs": logs, "bars": bars}, default=str))
