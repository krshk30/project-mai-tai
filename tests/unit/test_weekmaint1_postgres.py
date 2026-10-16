"""Opt-in real PG16 archive qualification, authored UNRUN; no application DSN.

Requires a FRESH qualification_weekmaint_* DB on the existing /private/tmp
Unix socket. Uses real mapped tables in public, actual commits and actual CSV
files. It does not certify broker gates, migrations, systemd or a whole job.
No schema/database drop, cleanup deletion or automatic fresh-DB retry exists.
"""
import csv
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import io
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ops.weekmaint import archive, policy
from project_mai_tai.db.models import (
    Base, BrokerAccount, BrokerOrder, Fill, OmsManagedPosition, Strategy, TradeIntent, VirtualPosition,
)

DSN_ENV = "WEEKMAINT1_TEST_PG_DSN"
ACK_ENV = "WEEKMAINT1_TEST_PG_WRITE_DATABASE"
NOW = datetime(2026, 10, 17, 8, 30, tzinfo=UTC)
TABLES = (BrokerAccount, Strategy, TradeIntent, BrokerOrder, Fill, VirtualPosition, OmsManagedPosition)
RETIREMENT_TABLES = ("broker_orders", "trade_intents", "oms_managed_positions", "virtual_positions")
PRICE = Decimal("1.23456789")
QUANTITY = Decimal("7.12345678")


def qualification_url(raw, acknowledgement):
    """Pure preconnection guard; never consult Settings or another PG variable."""
    if os.geteuid() == 0:
        raise ValueError("qualification_must_not_run_as_OS_root")
    url = make_url(raw)
    host = url.query.get("host", "")
    if (url.drivername != "postgresql+psycopg"
            or not re.fullmatch(r"qualification_weekmaint_[a-z0-9_]+", url.database or "")
            or acknowledgement != url.database
            or url.username != "killsw1_replay" or url.password is not None
            or url.host not in {None, "localhost"}
            or type(url.port) is not int or not 1024 <= url.port <= 65535 or url.port == 5432
            or set(url.query) != {"host"} or not isinstance(host, str)
            or not host.startswith("/private/tmp/") or ".." in Path(host).parts):
        raise ValueError("explicit_fresh_weekmaint_local_write_lab_required")
    return url


@pytest.fixture(scope="module")
def pg():
    raw = os.environ.get(DSN_ENV)
    if not raw:
        pytest.skip("WEEKMAINT1 real PG proof UNQUALIFIED: explicit opt-in DSN absent")
    url = qualification_url(raw, os.environ.get(ACK_ENV))
    socket = Path(url.query["host"])
    assert socket.is_dir() and not socket.is_symlink()
    assert socket.resolve() == socket and Path("/private/tmp") in socket.parents
    engine = create_engine(url, connect_args={"connect_timeout": 3,
        "options": "-csearch_path=public -cstatement_timeout=5000 -clock_timeout=2000 -ctimezone=UTC"})
    statements = []
    def audit(_connection, _cursor, statement, parameters, context, _many):
        statements.append(statement)
        assert not re.match(r"\s*(?:DELETE|TRUNCATE|DROP)\b", statement, re.IGNORECASE)
        if statement.startswith("INSERT INTO weekmaint_archive"):
            actual_sql = str(context.compiled.statement)
            assert "CAST(:source_json_text AS text)" in actual_sql
            assert "CAST(:original_row_json AS jsonb)" in actual_sql
            assert parameters["source_json_text"] == parameters["original_row_json"]
            assert isinstance(parameters["source_json_text"], str)
    event.listen(engine, "before_cursor_execute", audit)
    try:
        with engine.begin() as connection:
            identity = connection.execute(text("SELECT current_database(),current_user,session_user,"
                "current_setting('port'),inet_server_addr()::text,current_setting('server_version_num'),"
                "current_schema(),current_setting('transaction_read_only')")).one()
            assert identity[:3] == (url.database, url.username, url.username)
            assert int(identity[3]) == url.port and identity[4] is None
            assert 160000 <= int(identity[5]) < 170000 and identity[6:] == ("public", "off")
            assert connection.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname !~ '^pg_' "
                                          "AND nspname NOT IN ('public','information_schema')")) == 0
            assert connection.scalar(text("SELECT count(*) FROM pg_class c JOIN pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname='public'")) == 0
            Base.metadata.create_all(connection, tables=[model.__table__ for model in TABLES])
        sessions = sessionmaker(engine, expire_on_commit=False)
        strategy = uuid4()
        accounts = {provider: uuid4() for provider in ("schwab", "webull")}
        with sessions.begin() as session:
            session.add(Strategy(id=strategy, code="schwab_1m_v2", name="SYNTHETIC archive lab",
                                 execution_mode="live"))
            session.add_all([BrokerAccount(id=account, name=f"live:weekmaint-qualification-{provider}",
                environment="production", provider=provider) for provider, account in accounts.items()])
        yield SimpleNamespace(engine=engine, sessions=sessions, strategy=strategy, accounts=accounts,
                              statements=statements, identity=identity)
    finally:
        event.remove(engine, "before_cursor_execute", audit)
        engine.dispose()  # Keep DB/rows as evidence; never drop or clean by deletion.


def current_rows(pg, plans):
    with pg.engine.connect() as connection:
        return {plan.table: connection.scalar(text(
            f"SELECT to_jsonb(t)::text FROM public.{plan.table} t WHERE id=:id"),
            {"id": UUID(plan.row_id)}) for plan in plans}


def history(pg):
    with pg.engine.connect() as connection:
        return connection.execute(text("SELECT id,to_jsonb(t)::text FROM public.fills t ORDER BY id")).all()


def run_counts(pg, operation):
    with pg.engine.connect() as connection:
        if connection.scalar(text("SELECT to_regclass('public.weekmaint_runs')")) is None:
            return 0, 0
        return tuple(connection.scalar(text(f"SELECT count(*) FROM public.{table} WHERE operation_id=:op"),
            {"op": operation}) for table in ("weekmaint_runs", "weekmaint_archive"))


def seed_plans(pg, provider="schwab"):
    """Synthetic codec/transaction rows, not historical trades or broker-flat proof."""
    intent_id, order_id, managed_id, vp_id = (uuid4() for _ in range(4))
    symbol = "W" + order_id.hex[:8].upper()
    account = f"live:weekmaint-qualification-{provider}"
    at = NOW - timedelta(days=2)
    payload = {"preserve": {"limit": "1.23456789", "quantity": "7.12345678", "unknown_key": [1, "x"]}}
    with pg.sessions.begin() as session:
        session.add(TradeIntent(id=intent_id, strategy_id=pg.strategy, broker_account_id=pg.accounts[provider],
            symbol=symbol, side="buy", intent_type="open", quantity=QUANTITY, reason="SYNTHETIC lab",
            status="pending", payload=payload, created_at=at, updated_at=at))
        session.flush()
        session.add(BrokerOrder(id=order_id, intent_id=intent_id, strategy_id=pg.strategy,
            broker_account_id=pg.accounts[provider], symbol=symbol, side="buy", order_type="limit",
            time_in_force="DAY", quantity=QUANTITY, status="working", payload=payload,
            client_order_id="synthetic-weekmaint-" + order_id.hex, submitted_at=at, updated_at=at))
        session.flush()
        session.add(Fill(strategy_id=pg.strategy, broker_account_id=pg.accounts[provider], order_id=order_id,
            symbol=symbol, side="buy", quantity=Decimal("0.12345678"), price=PRICE, filled_at=at,
            broker_fill_id="synthetic-weekmaint-" + order_id.hex, payload={"must_never_delete": True}))
        session.add(OmsManagedPosition(id=managed_id, strategy_code="schwab_1m_v2", broker_account_name=account,
            symbol=symbol, entry_order_id=order_id, entry_client_order_id="synthetic-weekmaint-" + order_id.hex,
            entry_price=PRICE, original_quantity=7, current_quantity=0, status="open",
            scale_pnl=Decimal("1234567890.12345678"), created_at=at, updated_at=at))
        session.add(VirtualPosition(id=vp_id, strategy_id=pg.strategy, broker_account_id=pg.accounts[provider],
            symbol=symbol, quantity=QUANTITY, average_price=PRICE, realized_pnl=Decimal("-0.00000001"),
            opened_at=at, updated_at=at))
    ids = dict(zip(RETIREMENT_TABLES, (order_id, intent_id, managed_id, vp_id), strict=True))
    plans = []
    with pg.engine.connect() as connection:
        for table, row_id in ids.items():
            raw = connection.scalar(text(f"SELECT to_jsonb(t)::text FROM public.{table} t WHERE id=:id"),
                                    {"id": row_id})
            before = json.loads(raw, parse_float=Decimal)
            changes = {"updated_at": NOW.isoformat()}
            if table in {"broker_orders", "trade_intents"}:
                changes.update(status="aborted" if table == "broker_orders" else "superseded",
                               payload={"weekmaint": {"reason": "weekmaint_stale"}})
            elif table == "oms_managed_positions":
                changes["status"] = "closed"
            else:
                changes["quantity"] = "0"
            plans.append(policy.Retirement(table, str(row_id), account, symbol, before, changes, raw))
    return plans


def assert_csv_and_archive(pg, operation, plans, final, expected_raw=None):
    raw = final.read_bytes()
    if expected_raw is not None:
        assert raw == expected_raw
    parsed = list(csv.DictReader(io.StringIO(raw.decode())))
    assert len(parsed) == 4 and {row["table"] for row in parsed} == set(RETIREMENT_TABLES)
    by_table = {plan.table: plan for plan in plans}
    for row in parsed:
        plan = by_table[row["table"]]
        assert row["operation_id"] == str(operation) and row["row_id"] == plan.row_id
        assert row["source_json_text"] == plan.before_json and row["source_sha256"] == plan.source_hash
        assert row["reason"] == "weekmaint_stale" and row["account"] == plan.account
        assert row["changes_json"] == policy.encoded(plan.changes).decode()
    with pg.engine.connect() as connection:
        run = connection.execute(text("SELECT csv_sha256,row_count FROM public.weekmaint_runs "
            "WHERE operation_id=:op"), {"op": operation}).one()
        assert run == (hashlib.sha256(raw).hexdigest(), 4)
        records = connection.execute(text("SELECT table_name,source_json_text,original_row,source_sha256 "
            "FROM public.weekmaint_archive WHERE operation_id=:op"), {"op": operation}).all()
        assert len(records) == 4
        for table, original, jsonb_row, original_hash in records:
            assert original == by_table[table].before_json
            assert original_hash == hashlib.sha256(original.encode()).hexdigest()
            assert jsonb_row["id"] == by_table[table].row_id
        types = connection.execute(text("SELECT pg_typeof(source_json_text)::text,pg_typeof(original_row)::text "
            "FROM public.weekmaint_archive WHERE operation_id=:op"), {"op": operation}).all()
        assert len(types) == 4 and all(tuple(row) == ("text", "jsonb") for row in types)
        numeric_text = connection.execute(text("SELECT table_name,original_row->>'entry_price',"
            "original_row->>'average_price',original_row->>'quantity',original_row->>'scale_pnl' "
            "FROM public.weekmaint_archive WHERE operation_id=:op"), {"op": operation}).all()
        for table, entry_price, average_price, quantity, scale_pnl in numeric_text:
            if table == "oms_managed_positions":
                assert Decimal(entry_price) == PRICE and Decimal(scale_pnl) == Decimal("1234567890.12345678")
            elif table == "virtual_positions":
                assert Decimal(average_price) == PRICE and Decimal(quantity) == QUANTITY
            else:
                assert Decimal(quantity) == QUANTITY
    assert final.stat().st_mode & 0o077 == 0 and final.parent.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("provider", ["schwab", "webull"])
def test_actual_commit_retains_exact_preimages_prices_csv_and_history(pg, tmp_path, provider):
    plans = seed_plans(pg, provider)
    original, filled_before = current_rows(pg, plans), history(pg)
    operation = uuid4()
    result = archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
    assert run_counts(pg, operation) == (1, 4)
    final = Path(result["csv"])
    assert_csv_and_archive(pg, operation, plans, final)
    assert result["csv_sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    assert history(pg) == filled_before and not list(final.parent.glob("*.staged"))
    current = {table: json.loads(raw, parse_float=Decimal) for table, raw in current_rows(pg, plans).items()}
    assert current["broker_orders"]["status"] == "aborted"
    assert current["trade_intents"]["status"] == "superseded"
    assert current["oms_managed_positions"]["status"] == "closed"
    assert current["virtual_positions"]["quantity"] == 0
    assert current["broker_orders"]["quantity"] == QUANTITY
    assert current["virtual_positions"]["average_price"] == PRICE
    assert current["virtual_positions"]["realized_pnl"] == Decimal("-0.00000001")
    assert current["oms_managed_positions"]["scale_pnl"] == Decimal("1234567890.12345678")
    for table in ("broker_orders", "trade_intents"):
        assert current[table]["payload"]["preserve"] == json.loads(original[table])["payload"]["preserve"]
        assert current[table]["payload"]["weekmaint"] == {"reason": "weekmaint_stale"}
    assert any("FOR UPDATE" in statement for statement in pg.statements)
    assert not any(re.match(r"\s*(?:DELETE|TRUNCATE|DROP)\b", sql, re.I) for sql in pg.statements)


def test_real_changed_second_row_rolls_back_first_update_archive_and_run(pg, tmp_path):
    plans = seed_plans(pg)
    second = plans[1]
    with pg.engine.begin() as connection:
        connection.execute(text("UPDATE public.trade_intents SET reason='real concurrent source change' WHERE id=:id"),
                           {"id": UUID(second.row_id)})
    changed, filled_before = current_rows(pg, plans), history(pg)
    operation = uuid4()
    with pytest.raises(policy.Refused, match="row_changed_after_quiescent_plan"):
        archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
    assert current_rows(pg, plans) == changed and history(pg) == filled_before
    assert run_counts(pg, operation) == (0, 0)
    assert (tmp_path / "audit" / f"{operation}.csv.staged").is_file()
    assert not (tmp_path / "audit" / f"{operation}.csv").exists()


def test_real_transaction_rolls_back_on_last_update_failure_before_commit(pg, tmp_path):
    plans = seed_plans(pg)
    original, filled_before = current_rows(pg, plans), history(pg)
    updates = []
    def fail_last(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("UPDATE public."):
            updates.append(statement)
            if statement.startswith("UPDATE public.virtual_positions "):
                raise RuntimeError("controlled last update failure before real commit")
    event.listen(pg.engine, "before_cursor_execute", fail_last)
    operation = uuid4()
    try:
        with pytest.raises(RuntimeError, match="before real commit"):
            archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
    finally:
        event.remove(pg.engine, "before_cursor_execute", fail_last)
    assert len(updates) == 4
    assert current_rows(pg, plans) == original and history(pg) == filled_before
    assert run_counts(pg, operation) == (0, 0)
    assert not (tmp_path / "audit" / f"{operation}.csv").exists()


def test_CSV_stage_failure_never_enters_real_transaction(pg, tmp_path, monkeypatch):
    plans = seed_plans(pg)
    original, filled_before = current_rows(pg, plans), history(pg)
    operation = uuid4()
    def fail_stage(*_args):
        raise OSError("controlled stage/fsync failure before database transaction")
    with monkeypatch.context() as patch:
        patch.setattr(archive, "stage_file", fail_stage)
        offset = len(pg.statements)
        with pytest.raises(OSError, match="before database transaction"):
            archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
        assert len(pg.statements) == offset
    assert current_rows(pg, plans) == original and history(pg) == filled_before
    assert run_counts(pg, operation) == (0, 0)


def test_real_commit_before_publish_failure_recovers_exact_committed_CSV(pg, tmp_path, monkeypatch):
    plans = seed_plans(pg, "webull")
    operation = uuid4()
    filled_before, staged_bytes, publish_calls = history(pg), [], []
    def fail_after_commit(staged, final, expected):
        # This separate connection cannot observe uncommitted rows: actual
        # visibility here is the commit-boundary proof, not an engine spy.
        assert run_counts(pg, operation) == (1, 4)
        assert json.loads(current_rows(pg, plans)["virtual_positions"])["quantity"] == 0
        staged_bytes.append(staged.read_bytes())
        assert hashlib.sha256(staged_bytes[-1]).hexdigest() == expected and not final.exists()
        publish_calls.append(str(final))
        raise OSError("controlled publish crash AFTER independently visible real commit")
    with monkeypatch.context() as patch:
        patch.setattr(archive, "publish", fail_after_commit)
        with pytest.raises(OSError, match="AFTER independently visible real commit"):
            archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
    assert len(publish_calls) == 1 and run_counts(pg, operation) == (1, 4)
    final = archive.recover_committed_csv(pg.engine, operation, tmp_path / "audit")
    assert_csv_and_archive(pg, operation, plans, final, staged_bytes[0])
    assert history(pg) == filled_before


def test_duplicate_operation_is_real_unique_conflict_no_second_updates_or_history_change(pg, tmp_path):
    plans = seed_plans(pg)
    operation = uuid4()
    archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
    committed, filled_before = current_rows(pg, plans), history(pg)
    final = tmp_path / "audit" / f"{operation}.csv"
    raw = final.read_bytes()
    offset = len(pg.statements)
    with pytest.raises(IntegrityError):
        archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
    assert not any(statement.startswith("UPDATE public.") for statement in pg.statements[offset:])
    assert current_rows(pg, plans) == committed and history(pg) == filled_before
    assert run_counts(pg, operation) == (1, 4) and final.read_bytes() == raw


def test_committed_archive_corruption_refuses_recovery_instead_of_publishing_wrong_digest(pg, tmp_path, monkeypatch):
    plans = seed_plans(pg)
    operation = uuid4()
    def unavailable(*_args):
        raise OSError("controlled unpublished committed operation")
    with monkeypatch.context() as patch:
        patch.setattr(archive, "publish", unavailable)
        with pytest.raises(OSError):
            archive.archive_transaction(pg.engine, operation, plans, tmp_path / "audit")
    with pg.engine.begin() as connection:
        connection.execute(text("UPDATE public.weekmaint_archive SET source_json_text=source_json_text||' ' "
            "WHERE operation_id=:op AND table_name='broker_orders'"), {"op": operation})
    with pytest.raises(policy.Refused, match="CSV_digest_inconsistent"):
        archive.recover_committed_csv(pg.engine, operation, tmp_path / "audit")
    assert not (tmp_path / "audit" / f"{operation}.csv").exists()


@pytest.mark.parametrize("raw,ack", [
    ("postgresql+psycopg://killsw1_replay@localhost:5432/qualification_weekmaint_x?host=/private/tmp/lab", "qualification_weekmaint_x"),
    ("postgresql+psycopg://killsw1_replay@remote:51767/qualification_weekmaint_x?host=/private/tmp/lab", "qualification_weekmaint_x"),
    ("postgresql+psycopg://killsw1_replay@localhost:51767/project_mai_tai?host=/private/tmp/lab", "project_mai_tai"),
    ("postgresql+psycopg://root@localhost:51767/qualification_weekmaint_x?host=/private/tmp/lab", "qualification_weekmaint_x"),
    ("postgresql+psycopg://killsw1_replay@localhost:51767/qualification_weekmaint_x?host=/var/run/postgresql", "qualification_weekmaint_x"),
    ("postgresql+psycopg://killsw1_replay@localhost:51767/qualification_weekmaint_x?host=/private/tmp/lab", "wrong_ack"),
])
def test_unsafe_PG_targets_refused_before_any_engine_or_connection(monkeypatch, raw, ack):
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    with pytest.raises(ValueError, match="local_write_lab_required"):
        qualification_url(raw, ack)


def test_root_execution_refused_before_url_or_engine(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="OS_root"):
        qualification_url("not_even_parsed", None)
