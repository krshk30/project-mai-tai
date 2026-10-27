"""Offline copied-table MUST3 reader protocol. Never starts service/provider loops.

Requires a supplied SQLite copy and manifest; cannot manufacture tonight's copy.
All seeding/reconciliation writes target an ephemeral second copy of that file.
This proves selected local DB boot readers only, not PostgreSQL/native startup.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import socket
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, func, inspect, select, text
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, Strategy
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.reconciliation.service import ReconciliationService
from project_mai_tai.runtime_registry import strategy_registration_map
from project_mai_tai.services.control_plane import ControlPlaneRepository
from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from project_mai_tai.settings import Settings


LEGACY = {"momentum_30s", "momentum_60s"}
WARN = {"momentum_30s_warn", "momentum_60s_warn"}


def sha(path):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


class DenyIO:
    def __getattr__(self, name):
        raise AssertionError(f"external/service-loop IO forbidden in copy reader proof: {name}")


class Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.rows = []

    def emit(self, record):
        self.rows.append(record.getMessage())


def strategy_rows(factory):
    with factory() as session:
        return {row.code: {"id": str(row.id), "name": row.name, "execution_mode": row.execution_mode,
                          "is_enabled": row.is_enabled, "metadata_json": row.metadata_json}
                for row in session.scalars(select(Strategy)).all()}


def boot_readers(factory, settings, *, now):
    before = strategy_rows(factory)
    if not LEGACY <= before.keys():
        raise ValueError("both retained legacy rows are required, including their original IDs")
    with factory() as session:
        prior_paper_account = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == "paper:momentum"))
    with patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
        oms = OmsRiskService(settings, DenyIO(), session_factory=factory, broker_adapter=DenyIO())
        seeded = oms.seed_runtime_metadata()
        after = strategy_rows(factory)
        if not WARN <= after.keys() or any(after[code] != before[code] for code in LEGACY):
            raise AssertionError("warning rows missing or retained legacy rows changed")
        with factory() as session:
            paper_account = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == "paper:momentum"))
            if paper_account != prior_paper_account:
                raise AssertionError("runtime seeding changed/created a paper:momentum broker account")
        engine = StrategyEngineService(settings, DenyIO(), session_factory=factory, now_provider=lambda: now)
        errors = Errors()
        logging.getLogger().addHandler(errors)
        try:
            # These methods may catch SQL errors; ERROR logs and returned errors
            # are part of the failure gate, not silently accepted False values.
            engine._reconcile_runtime_state_from_database(log_when_changed=True)
            repository = ControlPlaneRepository(settings, session_factory=factory, redis=DenyIO())
            database = repository._load_database_state()
            if database["errors"] or database["row_conversion_warnings"]:
                raise AssertionError({"control_errors": database["errors"],
                                      "row_conversion_warnings": database["row_conversion_warnings"]})
            views = repository._build_bot_views(strategy_runtime={"bots": {}}, legacy_shadow={"bots": {}},
                recent_intents=database["recent_intents"], recent_orders=database["recent_orders"],
                recent_fills=database["recent_fills"], recent_trade_coach_reviews=database["recent_trade_coach_reviews"],
                recent_bar_decisions=database["recent_bar_decisions"], open_orders=database["open_orders"],
                persisted_snapshots=database["dashboard_snapshots"], virtual_positions=database["virtual_positions"],
                fills_read_status=database["recent_fills_read_status"])
            reconciler = ReconciliationService(settings, DenyIO(), session_factory=factory)
            cycle = reconciler.run_reconciliation_cycle()
            if cycle["status"] != "completed" or errors.rows:
                raise AssertionError({"reconciler": cycle, "reader_error_logs": errors.rows})
        finally:
            logging.getLogger().removeHandler(errors)
    return {"scope": "LOCAL_SQLITE_BOOT_READERS_ONLY", "seed": seeded,
        "legacy_rows_before": {code: before[code] for code in sorted(LEGACY)},
        "legacy_rows_after": {code: after[code] for code in sorted(LEGACY)},
        "warning_rows_after": {code: after[code] for code in sorted(WARN)},
        "registration_codes": sorted(strategy_registration_map(settings)),
        "strategy_runtime_codes": sorted(engine.state.bots),
        "control_bot_codes": sorted(row["strategy_code"] for row in views),
        "reconciliation": cycle,
        "limits": ["No service run loops, Redis restore, LIVE admission or provider reads",
                   "SQLite does not prove PostgreSQL SQL/locking/type semantics",
                   "Existing reconciler findings are preserved in the result, not treated as new strategy failures"]}


def run_copy(copy_path, manifest_path):
    manifest = json.loads(manifest_path.read_text())
    if manifest["kind"] not in {"ACTUAL_TONIGHT_COPY", "SYNTH_FIXTURE"}:
        raise ValueError("explicit actual-versus-synthetic provenance required")
    if manifest["sqlite_sha256"] != sha(copy_path):
        raise ValueError("copied database checksum mismatch")
    now = datetime.fromisoformat(manifest["next_boot_utc"])
    capture = datetime.fromisoformat(manifest["captured_at_utc"])
    if now.tzinfo is None or capture.tzinfo is None or now <= capture:
        raise ValueError("timezone-aware capture and subsequent boot clocks required")
    if manifest["kind"] == "ACTUAL_TONIGHT_COPY":
        captured_et, boot_et = capture.astimezone(ZoneInfo("America/New_York")), now.astimezone(ZoneInfo("America/New_York"))
        if (captured_et.date().isoformat() != "2026-10-21" or captured_et.hour < 16
                or boot_et.date() <= captured_et.date()
                or manifest.get("coverage") != "COMPLETE_ALL_APPLICATION_TABLES"):
            raise ValueError("MUST3 requires a complete October 21 after-close copy and next-day clock")
    with tempfile.TemporaryDirectory(prefix="momold1-copy-boot-") as temporary:
        target = Path(temporary) / "boot-copy.sqlite"
        shutil.copyfile(copy_path, target)
        engine = create_engine(f"sqlite+pysqlite:///{target}")
        try:
            required = set(Base.metadata.tables)
            declared = set(manifest["table_rows"])
            present = set(inspect(engine).get_table_names())
            if required != declared or not required <= present:
                raise ValueError({"missing_declared_tables": sorted(required - declared),
                                  "missing_sqlite_tables": sorted(required - present),
                                  "unexpected_declared_tables": sorted(declared - required)})
            if any(type(value) is not int or value < 0 for value in manifest["table_rows"].values()):
                raise ValueError("table counts must be explicit nonnegative integers")
            with engine.connect() as connection:
                for table in Base.metadata.sorted_tables:
                    count = connection.scalar(select(func.count()).select_from(table))
                    if count != manifest["table_rows"][table.name]:
                        raise ValueError(f"table count mismatch: {table.name}")
                if connection.execute(text("PRAGMA foreign_key_check")).all():
                    raise ValueError("copied table foreign keys are incomplete")
            factory = sessionmaker(bind=engine, expire_on_commit=False)
            # File must contain redacted effective non-secret settings. Runtime
            # environment and .env must not substitute current production values.
            configuration = manifest["effective_settings_redacted"]
            if not {"momentum_paper_enabled", "market_data_momentum_warning_enabled",
                    "momentum_live_enabled"} <= configuration.keys():
                raise ValueError("explicit momentum enablement settings required")
            unsafe = [key for key, value in configuration.items() if value and any(
                word in key.lower() for word in ("secret", "password", "token", "api_key", "database_url", "redis_url"))]
            if unsafe:
                raise ValueError(f"non-redacted external settings refused: {unsafe}")
            with patch.dict(os.environ, {}, clear=True):
                settings = Settings(_env_file=None, **configuration)
                # Read clocks, including code paths that use module utcnow(),
                # are explicitly pinned to the next-boot scenario.
                with patch("project_mai_tai.services.control_plane.utcnow", lambda: now), \
                     patch("project_mai_tai.reconciliation.service.utcnow", lambda: now), \
                     patch("project_mai_tai.services.strategy_engine_app.utcnow", lambda: now):
                    result = boot_readers(factory, settings, now=now)
        finally:
            engine.dispose()
    result.update(input_kind=manifest["kind"], input_sha256=sha(copy_path), manifest_sha256=sha(manifest_path),
                  captured_at_utc=manifest["captured_at_utc"], next_boot_utc=manifest["next_boot_utc"],
                  must3="LOCAL_COPY_READERS_COMPLETED" if manifest["kind"] == "ACTUAL_TONIGHT_COPY"
                  else "SYNTH_ONLY_NOT_TONIGHT_ACCEPTANCE")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_copy(args.copy, args.manifest)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2, default=str)
        handle.write("\n")


if __name__ == "__main__":
    main()
