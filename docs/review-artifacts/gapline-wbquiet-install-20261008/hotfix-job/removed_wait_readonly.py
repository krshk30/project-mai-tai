"""Measure the real restore algorithm in a bounded read-only transaction."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def inspect_store(connection, store_factory, session_factory):
    from sqlalchemy import text
    connection.exec_driver_sql('SET TRANSACTION READ ONLY')
    connection.exec_driver_sql("SET LOCAL statement_timeout='5000ms'")
    connection.exec_driver_sql("SET LOCAL lock_timeout='500ms'")
    revisions = list(connection.execute(text('SELECT version_num FROM alembic_version')).scalars())
    if revisions != ['20261008_0023']:
        raise ValueError('installed schema must be exactly 0023; no upgrade authorized')
    restored = store_factory(session_factory).restore()
    identities = sorted((name, request.token) for name, request in restored.items())
    return dict(observed_at_utc=datetime.now(timezone.utc).isoformat(), revision=revisions,
                restored_count=len(restored), restored_symbols=sorted(restored),
                identity_sha256=hashlib.sha256(json.dumps(identities).encode()).hexdigest(),
                snapshot_scope='exact current v2_removed_wait type; archived types untouched/excluded by source',
                observation='read-only replay of deployed RemovedWaitStore.restore, not direct process-memory access',
                direct_boot_count='UNMEASURED: deployed boot path has no count marker', writes_performed=False)


def collect():
    from sqlalchemy.orm import sessionmaker
    from project_mai_tai.db.session import build_engine
    from project_mai_tai.settings import Settings
    from project_mai_tai.v2_removed_wait import RemovedWaitStore
    settings = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
    engine = build_engine(settings.database_url, connect_timeout_s=5, statement_timeout_ms=5000)
    try:
        with engine.connect() as connection:
            factory = sessionmaker(bind=connection, join_transaction_mode='rollback_only')
            return inspect_store(connection, RemovedWaitStore, factory)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = collect()
    with args.output.open('x') as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write('\n')
    args.output.chmod(0o600)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
