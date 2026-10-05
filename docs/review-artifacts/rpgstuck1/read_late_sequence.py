"""Bounded read-only evidence pull; run through stdin, never install on the VPS."""
from datetime import UTC, datetime
import json

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings

queries = {
    "tickets": """SELECT id,created_at,payload FROM dashboard_snapshots
        WHERE snapshot_type='atr_reprice_handoff' AND payload->'old'->>'symbol'='APUS'
        AND created_at>='2026-10-05T14:25Z' AND created_at<'2026-10-05T14:28Z'
        ORDER BY created_at LIMIT 4""",
    "orders": """SELECT b.client_order_id,b.broker_order_id,b.submitted_at,b.status,
        b.quantity,b.payload,a.name account FROM broker_orders b
        JOIN broker_accounts a ON a.id=b.broker_account_id JOIN strategies s ON s.id=b.strategy_id
        WHERE s.code='schwab_1m_v2' AND b.symbol='APUS'
        AND b.submitted_at>='2026-10-05T14:25Z' AND b.submitted_at<'2026-10-05T14:28Z'
        ORDER BY b.submitted_at LIMIT 10""",
    "intents": """SELECT t.created_at,t.status,t.reason,t.quantity,t.payload,a.name account
        FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id
        JOIN strategies s ON s.id=t.strategy_id WHERE s.code='schwab_1m_v2' AND t.symbol='APUS'
        AND t.created_at>='2026-10-05T14:25Z' AND t.created_at<'2026-10-05T14:28Z'
        ORDER BY t.created_at LIMIT 20""",
}
output = {"read_at": datetime.now(UTC).isoformat(), "queries": {}}
engine = create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
with engine.connect() as connection:
    for name, query in queries.items():
        try:
            connection.execute(text("SET TRANSACTION READ ONLY"))
            connection.execute(text("SET LOCAL statement_timeout='8s'"))
            output["queries"][name] = [dict(row) for row in connection.execute(text(query)).mappings()]
        finally:
            connection.rollback()
print(json.dumps(output, default=str, indent=2))
