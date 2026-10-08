"""Bounded recorded quotes at the fresh AIXI BUY; no invented price controls."""
import json
from datetime import UTC, datetime
from sqlalchemy import create_engine, text
from project_mai_tai.settings import Settings

engine = create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
with engine.connect() as conn:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(text("SET LOCAL statement_timeout='5s'"))
    rows = conn.execute(text("""select event_ts,received_at,bid_price,ask_price,last_price
        from market_quote_ticks where symbol='AIXI' and event_ts>='2026-10-08 12:40:02.534Z'
        and event_ts<'2026-10-08 12:43Z' and ask_price between 2.0205 and 2.0307
        and last_price>=2.0205 order by event_ts limit 51""")).mappings().all()
    print(json.dumps({"as_of_utc": datetime.now(UTC).isoformat(),
        "read_only": conn.execute(text("SHOW transaction_read_only")).scalar_one(),
        "quotes": [dict(r) for r in rows]}, default=str, indent=2))
