"""Bounded present-time durable-ticket read, not a historical restart snapshot."""
from datetime import UTC, datetime
import json

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings


engine = create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
with engine.connect() as conn:
    conn.execute(text("SET TRANSACTION READ ONLY"))
    conn.execute(text("SET LOCAL statement_timeout='3s'"))
    rows = [dict(row) for row in conn.execute(text(
        "SELECT id, created_at, payload FROM dashboard_snapshots "
        "WHERE snapshot_type='atr_reprice_handoff' "
        "AND payload->'old'->>'symbol' IN ('SXTC','DKI') "
        "ORDER BY created_at, id LIMIT 101"
    )).mappings()]
    if len(rows) > 100:
        raise RuntimeError("durable-ticket read exceeded 100 rows")
    conn.rollback()
print(json.dumps({"read_at_utc": datetime.now(UTC).isoformat(),
                  "scope": "current durable SXTC/DKI tickets; NOT historical 13:45 reconstruction or positive clearance",
                  "rows": rows}, default=str))
