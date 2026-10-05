"""Read-only MI exit assessment; indexed bounded SQL and selected log lines."""
import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings

settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
engine = create_engine(settings.database_url)
result = {"read_at": datetime.now(UTC).isoformat()}
with engine.connect() as conn:
    def query(sql):
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout='5s'"))
        rows = [dict(row) for row in conn.execute(text(sql)).mappings()]
        conn.rollback()
        return rows

    result["confirmation"] = query("SELECT symbol,broker_account_name,filled_at,evaluation_bar_start_ms,evaluated_at,atr_state,should_exit,confirmation_bars FROM v2_confirmation_exit_evaluations WHERE symbol='MI' AND filled_at>='2026-10-05T00:00Z' AND filled_at<'2026-10-05T14:35Z' ORDER BY evaluated_at LIMIT 20")
    result["mi_bars"] = query("SELECT bar_time,position_quantity,decision_status,decision_reason,indicators FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND symbol='MI' AND bar_time>='2026-10-05T13:30Z' AND bar_time<'2026-10-05T14:35Z' ORDER BY bar_time LIMIT 65")
result["mi_exit_lines"] = []
for line in Path('/var/log/project-mai-tai/schwab-1m-v2.log').open(errors='replace'):
    if line[:19] > '2026-10-05 14:35:00':
        continue
    if ('sym=MI ' in line or '] MI ' in line) and any(marker in line for marker in ('CONFIRMATION', 'ATR-SELL', 'GAP-', 'ATR-PROBE')):
        result["mi_exit_lines"].append(line.strip())
result["mi_exit_lines"] = result["mi_exit_lines"][-100:]
print(json.dumps(result, default=str, indent=2))
