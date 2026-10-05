"""Bounded, read-only recorded-bar capture for the restoration lane."""

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings


def main():
    engine = create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = [dict(row) for row in conn.execute(text(
            "SELECT symbol, bar_time, created_at, open_price, high_price, low_price, "
            "close_price, volume, source FROM strategy_bar_history "
            "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 "
            "AND symbol IN ('RETO','JAGX') "
            "AND bar_time >= '2026-10-05T08:00:00Z' "
            "AND bar_time < '2026-10-06T00:00:00Z' "
            "ORDER BY symbol, bar_time LIMIT 1921"
        )).mappings()]
        if len(rows) > 1920:
            raise RuntimeError("read exceeds two complete 04:00-20:00 sessions")
        conn.rollback()
    print(json.dumps({"read_at": datetime.now(UTC).isoformat(), "bars": rows}, default=str))


if __name__ == "__main__":
    main()
