"""[codex] Bounded preservation receipt only; no trading authority or DB writes."""
import hashlib
import json
import sys
from sqlalchemy import event, text
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings

TYPE="oms_webull_mirror_retained_hold__archived_20261007"


def capture(connection):
    count,size=connection.execute(text("SELECT count(*),coalesce(sum(octet_length(payload::text)),0) "
        "FROM dashboard_snapshots WHERE snapshot_type=:kind"),dict(kind=TYPE)).one()
    if count>64 or size>1_000_000: raise ValueError("archived preservation evidence exceeds64/1MB")
    rows=connection.execute(text("SELECT id,payload::text body FROM dashboard_snapshots WHERE snapshot_type=:kind ORDER BY id LIMIT 65"),dict(kind=TYPE)).mappings()
    result=[dict(id=str(row["id"]),payload_sha256=hashlib.sha256(row["body"].encode()).hexdigest()) for row in rows]
    if len(result)!=count: raise ValueError("archived preservation count mismatch")
    return dict(snapshot_type=TYPE,count=count,rows=result,preservation_only=True)


def collect():
    engine=build_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url,
        connect_timeout_s=5,statement_timeout_ms=5000)
    @event.listens_for(engine,"begin")
    def readonly(connection): connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            print(json.dumps(capture(connection),sort_keys=True))
    finally: engine.dispose()


def main():
    try:
        collect()
        return 0
    except Exception as exc:
        print(json.dumps(dict(rc=2, verdict="UNKNOWN", error_type=type(exc).__name__,
                              preservation_only=True), sort_keys=True))
        return 2


if __name__=="__main__": sys.exit(main())
