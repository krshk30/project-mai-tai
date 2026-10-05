"""Run actual pinned old-main code against the actual additive migration, offline."""
from __future__ import annotations

import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

BASE = "7e10baf0319da796b84934fe38994f6db4fcfc0b"
ROOT = Path(__file__).resolve().parents[3]
PROGRAM = r'''
import importlib.util
from decimal import Decimal
from pathlib import Path
import sys
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from project_mai_tai.db.models import Base, OmsManagedPosition
from project_mai_tai.oms.store import OmsStore
assert "entry_order_id" not in OmsManagedPosition.__table__.columns
engine = create_engine("sqlite://")
Base.metadata.create_all(engine)
spec = importlib.util.spec_from_file_location("actual_0022", sys.argv[1])
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)
with engine.begin() as c:
    migration.op = Operations(MigrationContext.configure(c))
    migration.upgrade()
store = OmsStore()
with Session(engine) as s:
    for account in ("live:schwab_1m_v2", "live:orb"):
        row = store.create_managed_position(s, strategy_code="schwab_1m_v2", broker_account_name=account, symbol="ROLLBACK", entry_price=Decimal("5.07"), quantity=1)
        s.commit()
        assert store.get_open_managed_position(s, broker_account_name=account, symbol="ROLLBACK").id == row.id
        assert s.execute(text("SELECT entry_order_id,entry_client_order_id FROM oms_managed_positions WHERE id=:id"), {"id":row.id.hex}).one() == (None,None)
        store.close_managed_position(s, row)
        s.commit()
        assert store.get_open_managed_position(s, broker_account_name=account, symbol="ROLLBACK") is None
        print(account + " old_main_additive_schema_create_read_close=PASS")
print("old_model_has_no_binding_columns=PASS actual_migration_0022=PASS")
'''


def main():
    archive = subprocess.check_output(["git", "archive", BASE, "src"], cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix=".ownmix1-old-schema-", dir=ROOT) as directory:
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:
            source.extractall(directory, filter="data")
        env = {**os.environ, "PYTHONPATH": "src", "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run(
            [sys.executable, "-c", PROGRAM,
             str(ROOT / "sql/migrations/versions/20261005_0022_managed_entry_binding.py")],
            cwd=directory, env=env, capture_output=True, text=True,
        )
        print("old_main_commit=" + BASE)
        print(result.stdout, end="")
        if result.returncode:
            print(result.stderr, file=sys.stderr)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
