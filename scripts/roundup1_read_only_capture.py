"""Bounded independent ROUNDUP1 capture; stdout only, no Redis or broker writes."""

import json
from datetime import UTC, datetime

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings


QUERIES = {
    "orders": """
        SELECT b.id,b.client_order_id,b.broker_order_id,b.symbol,b.quantity,
               b.status,b.submitted_at,b.updated_at,b.order_type,b.payload,
               a.name account,s.code strategy
        FROM broker_orders b
        JOIN broker_accounts a ON a.id=b.broker_account_id
        JOIN strategies s ON s.id=b.strategy_id
        WHERE s.code='schwab_1m_v2' AND b.side='buy'
          AND a.name IN ('live:schwab_1m_v2','live:orb')
          AND b.submitted_at >= '2026-09-23T04:00:00Z'
          AND b.submitted_at <= '2026-10-05T17:44:00Z'
          AND b.payload->>'resting_entry'='true'
          AND b.payload->>'resting_offset_pct' IN ('0.5','0.50')
        ORDER BY b.submitted_at,b.id LIMIT 2001
    """,
    "sckt_events": """
        SELECT e.event_at,e.event_type,e.event_source,e.payload
        FROM broker_order_events e JOIN broker_orders b ON b.id=e.order_id
        WHERE b.client_order_id='schwab_1m_v2-SCKT-open-9a10bc5b0102'
        ORDER BY e.event_at LIMIT 65
    """,
    "sckt_fills": """
        SELECT f.id,f.side,f.quantity,f.price,f.filled_at,b.client_order_id
        FROM fills f JOIN broker_orders b ON b.id=f.order_id
        JOIN broker_accounts a ON a.id=b.broker_account_id
        JOIN strategies s ON s.id=b.strategy_id
        WHERE f.symbol='SCKT' AND s.code='schwab_1m_v2' AND a.name='live:orb'
          AND f.filled_at >= '2026-10-05T16:40:00Z'
          AND f.filled_at <= '2026-10-05T17:44:00Z'
        ORDER BY f.filled_at LIMIT 65
    """,
    "sckt_tape": """
        SELECT count(*) n,max(price) max_price,
               count(*) FILTER (WHERE price >= 1.0636) at_rule_or_above,
               min(event_ts) first_at,max(event_ts) last_at
        FROM market_trade_ticks WHERE symbol='SCKT'
          AND event_ts >= '2026-10-05T16:47:00Z'
          AND event_ts < '2026-10-05T16:57:00Z'
    """,
}


def main():
    engine = create_engine(
        Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url,
        connect_args={"connect_timeout": 5},
    )
    result = {"read_at": datetime.now(UTC).isoformat(), "source": "independent READ ONLY SQL", "queries": {}}
    with engine.connect() as connection:
        for name, sql in QUERIES.items():
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout='8s'"))
            rows = [dict(row) for row in connection.execute(text(sql)).mappings()]
            connection.rollback()
            bound = 2000 if name == "orders" else 64
            if len(rows) > bound:
                raise RuntimeError(f"{name}: capture bound exceeded; no truncated evidence")
            result["queries"][name] = rows
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
