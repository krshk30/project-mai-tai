"""Bounded read-only WETO / MI fixture extraction, no Redis or broker calls."""
import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings

queries = {
    "veea_late_ask": "SELECT id,event_ts,bid_price,ask_price FROM market_quote_ticks WHERE provider='schwab' AND symbol='VEEA' AND event_ts>='2026-10-05T12:33:23Z' AND event_ts<='2026-10-05T12:33:33.821Z' ORDER BY event_ts DESC,id DESC LIMIT 1",
    "veea_after_settle_grace": "SELECT id,event_ts,price,size,raw FROM market_trade_ticks WHERE provider='schwab' AND symbol='VEEA' AND event_ts>='2026-10-05T12:33:33Z' AND event_ts<'2026-10-05T12:34:02Z' AND price>=5.2613 ORDER BY event_ts,id LIMIT 1",
    "weto_first_late_tick": "SELECT id,event_ts,price,size,raw FROM market_trade_ticks WHERE provider='schwab' AND symbol='WETO' AND event_ts>='2026-09-24T13:24:00.624Z' AND event_ts<'2026-09-24T13:40Z' AND price>=2.0778 ORDER BY event_ts,id LIMIT 1",
    "mi_orders": "SELECT b.id,b.submitted_at,b.status,b.quantity,b.payload,a.name account FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id JOIN strategies s ON s.id=b.strategy_id WHERE s.code='schwab_1m_v2' AND b.symbol='MI' AND b.submitted_at>='2026-10-05T13:15:00Z' AND b.submitted_at<'2026-10-05T13:16Z' ORDER BY b.submitted_at LIMIT 4",
    "mi_quotes": "SELECT id,event_ts,bid_price,ask_price,last_price,raw FROM market_quote_ticks WHERE provider='schwab' AND symbol='MI' AND event_ts>='2026-10-05T13:15:19Z' AND event_ts<'2026-10-05T13:15:23Z' ORDER BY event_ts LIMIT 4",
}
output = {"read_at": datetime.now(UTC).isoformat(), "queries": {}}
engine = create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
with engine.connect() as connection:
    for name, query in queries.items():
        connection.execute(text("SET TRANSACTION READ ONLY"))
        connection.execute(text("SET LOCAL statement_timeout='5s'"))
        output["queries"][name] = [dict(row) for row in connection.execute(text(query)).mappings()]
        connection.rollback()
print(json.dumps(output, default=str, indent=2))
