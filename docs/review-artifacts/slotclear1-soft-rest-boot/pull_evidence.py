"""Bounded read-only Oct8 proof and stored bars; run on the box over SSH stdin."""
import json
from datetime import UTC, datetime
from sqlalchemy import create_engine, text
from project_mai_tai.settings import Settings

engine = create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
queries = {
    "aixi_bars": "select bar_time,open_price,high_price,low_price,close_price,volume,source,created_at from strategy_bar_history where strategy_code='schwab_1m_v2' and symbol='AIXI' and interval_secs=60 and bar_time>='2026-10-08 11:00Z' and bar_time<='2026-10-08 12:40Z' order by bar_time,created_at limit 501",
    "journal": "select snapshot_type,created_at,payload from dashboard_snapshots where snapshot_type in ('v2_wait_dispatch','v2_flip_entry_ownership') and payload->>'symbol'='AIXI' and created_at>='2026-10-08 12:18Z' and created_at<'2026-10-08 12:31Z' order by created_at limit 65",
    "orders": "select a.name account,b.symbol,b.client_order_id,b.status,b.quantity,b.submitted_at,i.created_at intent_created_at,i.payload from broker_orders b join broker_accounts a on a.id=b.broker_account_id left join trade_intents i on i.id=b.intent_id where b.symbol='AIXI' and a.name in ('live:schwab_1m_v2','live:orb') and (b.submitted_at>='2026-10-08 11:00Z' or i.created_at>='2026-10-08 11:00Z') order by i.created_at limit 201",
    "fills": "select a.name account,f.symbol,f.side,f.quantity,f.price,f.filled_at from fills f join broker_accounts a on a.id=f.broker_account_id where f.symbol='AIXI' and a.name in ('live:schwab_1m_v2','live:orb') and f.filled_at>='2026-10-08 11:00Z' order by f.filled_at limit 201",
}
with engine.connect() as conn:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(text("SET LOCAL statement_timeout='5s'"))
    conn.execute(text("SET LOCAL lock_timeout='500ms'"))
    result = {"as_of_utc": datetime.now(UTC).isoformat(), "read_only": conn.execute(text("SHOW transaction_read_only")).scalar_one()}
    for name, sql in queries.items():
        result[name] = [dict(row) for row in conn.execute(text(sql)).mappings()]
print(json.dumps(result, default=str, indent=2))
