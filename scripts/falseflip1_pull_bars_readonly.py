"""Run through stdin on the box. SELECT only, bounded cohort/session history."""
import json
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from project_mai_tai.settings import Settings


COHORT = [
    ("AIFF", "2026-09-25"), ("CLRO", "2026-09-28"), ("BKYI", "2026-09-29"),
    ("GOW", "2026-09-30"), ("TNON", "2026-09-30"), ("AIXI", "2026-10-02"),
    ("AMOD", "2026-10-02"), ("CYCU", "2026-10-02"), ("IPDN", "2026-10-06"),
    ("OLOX", "2026-10-06"), ("BIYA", "2026-10-07"), ("NCPL", "2026-10-07"),
    ("FLYE", "2026-10-08"),
]


def main():
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    symbols, days = zip(*COHORT, strict=True)
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5,
                        options="-c default_transaction_read_only=on -c statement_timeout=15000 "
                                "-c lock_timeout=1000 -c timezone=UTC") as connection:
        asof = connection.execute("SELECT clock_timestamp() AS asof").fetchone()["asof"]
        rows = connection.execute("""
            SELECT b.symbol,b.bar_time,b.open_price,b.high_price,b.low_price,b.close_price,
                   b.volume,b.source,b.created_at
            FROM strategy_bar_history b
            JOIN unnest(%s::text[],%s::date[]) AS c(symbol,day) ON c.symbol=b.symbol
             AND b.bar_time >= (c.day+time '04:00') AT TIME ZONE 'America/New_York'
             AND b.bar_time < (c.day+1+time '04:00') AT TIME ZONE 'America/New_York'
            WHERE b.strategy_code='schwab_1m_v2' AND b.interval_secs=60
            ORDER BY b.symbol,b.bar_time LIMIT 14001
        """, (list(symbols), list(days))).fetchall()
    if len(rows) > 14000:
        raise RuntimeError("cohort history limit exceeded")
    print(json.dumps({"source": "strategy_bar_history SELECT current cohort session series",
                      "captured_at": datetime.now(UTC), "asof": asof, "limit": 14001,
                      "cohort_symbol_days": COHORT, "bars": rows}, default=str))


if __name__ == "__main__":
    main()
