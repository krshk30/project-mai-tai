"""SOURCE UNRUN: C1 01:32 narrow cut; all transport operations record and refuse.

This creates a private schema/model projection, not a production boot or native
snapshot. Missing exported columns use disclosed CONTROLLED plumbing values.
No startup metadata/control seed, unrelated hold restoration, or provider proof.
"""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from uuid import UUID, NAMESPACE_URL, uuid5

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, DashboardSnapshot, Fill,
    OmsArmedStop, OmsManagedPosition, Strategy, SystemIncident, TradeIntent,
    VirtualPosition,
)
from project_mai_tai.oms import service as oms, wbexit2, wbexit3
from project_mai_tai.oms.order_read_batch import OrderReadBatch
from project_mai_tai.settings import Settings

DATA = Path(__file__).parents[1] / "fixtures" / "wbexit3" / "narrow_boot"
MODELS = (BrokerAccount, Strategy, SystemIncident, BrokerOrder, TradeIntent,
          Fill, VirtualPosition, AccountPosition, OmsManagedPosition,
          OmsArmedStop, DashboardSnapshot)
REASON = "oms_v2_managed_exit:CW_HARD_STOP"


class RefusedTransport(RuntimeError):
    pass


class RecordingRefuser:
    """Truthy constructor dependency: no successful []/flat/terminal response."""
    def __init__(self, name):
        self.name, self.calls = name, []

    def __bool__(self):
        return True

    def __getattr__(self, method):
        if method.startswith("__"):
            raise AttributeError(method)
        async def refused(*args, **kwargs):
            self.calls.append({"method": method, "args": repr(args), "kwargs": repr(kwargs)})
            raise RefusedTransport(self.name + ":" + method)
        return refused


def canonical(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def load_inputs():
    binding = json.loads((DATA / "binding.json").read_text())
    # UNBOUND never becomes a skip/PASS. Main reviews and seals actual operands
    # before authorizing collection/execution; worker never queries the source.
    assert binding["status"] == "SEALED", "narrow boot operands UNBOUND; no execution-ready claim"
    for record in binding["authority_receipts"]:
        assert hashlib.sha256((DATA / record["file"]).read_bytes()).hexdigest() == record["sha256"]
    raw = (DATA / "real_close.psv.txt").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == binding["raw_export_sha256"]
    records = [json.loads(line) for line in raw.decode().splitlines() if line.startswith("{")]
    assert len(records) == 1
    export = records[0]
    assert export["isolation"] == "repeatable read" and export["read_only"] == "on"
    for key in ("system_incidents_wbexit2_any_status", "system_incidents_wbexit3_any_status",
                "oms_managed_positions_open_all", "broker_orders_open_states", "oms_armed_stops"):
        assert export["counts"][key] == 0
    assert export["virtual_positions_nonzero"] is None and export["account_positions_nonzero"] is None
    assert binding["nonterminal_trade_intents_count"] == 0
    # This exact query predicate can select historical terminal orders. A zero
    # OPEN-order count alone is insufficient evidence of an empty ORB work list.
    assert binding["orb_protection_work"]["status"] == "SEALED"
    orb_raw = (DATA / "orb_protection.psv.txt").read_bytes()
    assert hashlib.sha256(orb_raw).hexdigest() == binding["orb_protection_work"]["raw_sha256"]
    assert binding["orb_protection_work"]["expected_work_count"] == 0
    assert binding["orb_protection_work"]["candidate_count"] == 1
    candidate = binding["orb_protection_work"]["candidate"]
    native_rows = [line.split("|", 6) for line in orb_raw.decode().splitlines()
                   if line.startswith(candidate["id"] + "|") and line.count("|") >= 6]
    assert len(native_rows) == 1
    values = native_rows[0]
    assert dict(zip(("id", "symbol", "status", "quantity", "submitted_at", "route"), values[:6])) == {
        key: candidate[key] for key in ("id", "symbol", "status", "quantity", "submitted_at", "route")}
    assert json.loads(values[6]) == candidate["payload"]
    assert binding["settings"]["orb_webull_account_name"]
    return export, binding


def build_case(tmp_path, *, on):
    export, binding = load_inputs()
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "narrow-boot.sqlite"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    captured = datetime.fromisoformat(export["captured_at"])
    with factory.begin() as db:
        for row in export["broker_accounts"]:
            # Only these five fields were exported. environment/native identity
            # and clocks below are CONTROLLED plumbing, never production facts.
            db.add(BrokerAccount(id=UUID(row["id"]), name=row["name"], provider=row["provider"],
                is_active=row["is_active"], environment="CONTROLLED_PRIVATE_PROJECTION",
                external_account_id=None, created_at=captured, updated_at=captured))
        for row in export["strategies"]:
            db.add(Strategy(id=UUID(row["id"]), code=row["code"], name="CONTROLLED " + row["code"],
                execution_mode="live", is_enabled=True, metadata_json={},
                created_at=captured, updated_at=captured))
        db.flush()
        # Real historical terminal candidate; a missing VP is preserved as
        # absence, not manufactured zero. The actual selector must reject work
        # despite its positive missing-VP guard. Omitted columns are disclosed.
        orb = binding["orb_protection_work"]["candidate"]
        account = db.scalar(select(BrokerAccount).where(BrokerAccount.name == "live:orb"))
        strategy = db.scalar(select(Strategy).where(Strategy.code == "orb_schwab"))
        db.add(BrokerOrder(id=UUID(orb["id"]), intent_id=None,
            strategy_id=strategy.id, broker_account_id=account.id,
            client_order_id="CONTROLLED-OMITTED-STAK-CLIENT-COLUMN", broker_order_id=None,
            symbol=orb["symbol"], side="buy", order_type="CONTROLLED_OMITTED_COLUMN",
            time_in_force="day", quantity=Decimal(orb["quantity"]), status=orb["status"],
            payload=deepcopy(orb["payload"]), submitted_at=datetime.fromisoformat(orb["submitted_at"]),
            updated_at=captured))
    options = dict(binding["settings"])
    options["oms_webull_exit3_enabled"] = on
    # Hold WB2 fixed in both arms: OFF means WB3 OFF, not disabling WB2 ownership.
    options["strategy_schwab_1m_v2_wbexit2_enabled"] = True
    for key, value in options.items():
        assert key in Settings.model_fields, "unknown sealed setting: " + key
        annotation = Settings.model_fields[key].annotation
        assert annotation in (bool, str), "unreviewed sealed setting type: " + key
        assert type(value) is annotation, "sealed setting value/type mismatch: " + key
    settings = Settings(_env_file=None, **options)
    assert all(getattr(settings, key) == value for key, value in options.items())
    broker, redis = RecordingRefuser("provider"), RecordingRefuser("Redis")
    service = oms.OmsRiskService(settings=settings, redis_client=redis,
        session_factory=factory, broker_adapter=broker)
    assert service.redis is redis
    assert service.settings.orb_webull_account_name == binding["settings"]["orb_webull_account_name"]
    return service, factory, broker, redis, engine, captured, binding


def rows(factory):
    with factory() as db:
        return {model.__tablename__: sorted((
            canonical({prop.key: getattr(row, prop.key) for prop in model.__mapper__.column_attrs})
            for row in db.scalars(select(model))), key=str) for model in MODELS}


def seed_controlled_aged_duties(case):
    """CONTROLLED AIOK-shaped 27-share preimage, NOT the empty real export.

    Only hydration is qualified: a durable unknown original tree and physically
    unjoined prior nonce are explicit inputs, not native release/fill evidence.
    """
    service, factory, _, _, _, captured, binding = case
    opened = captured - timedelta(days=1, minutes=15)
    entry_time = opened - timedelta(minutes=1)
    account_name = binding["settings"]["strategy_schwab_1m_v2_webull_account_name"]
    assert account_name
    def identity(label):
        return uuid5(NAMESPACE_URL, "CONTROLLED-WB3-NARROW-BOOT:" + label)
    with factory.begin() as db:
        account = db.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
        strategy = db.scalar(select(Strategy).where(Strategy.code == "schwab_1m_v2"))
        assert account is not None and account.provider == "webull" and account.is_active
        assert strategy is not None
        intent = TradeIntent(id=identity("entry-parent"), strategy_id=strategy.id,
            broker_account_id=account.id, symbol="AIOK", side="buy", intent_type="open",
            quantity=Decimal(27), reason="CONTROLLED prior-day fixture BUY", status="filled",
            payload={"fixture": "CONTROLLED_NOT_NATIVE"}, created_at=entry_time, updated_at=entry_time)
        db.add(intent)
        entry = BrokerOrder(id=identity("entry"), intent_id=intent.id, strategy_id=strategy.id,
            broker_account_id=account.id, client_order_id="CONTROLLED-BOOT-AIOK-ENTRY",
            broker_order_id="CONTROLLED-BOOT-AIOK-NATIVE", symbol="AIOK", side="buy",
            order_type="limit", time_in_force="day", quantity=Decimal(27), status="filled",
            payload={}, submitted_at=entry_time, updated_at=entry_time)
        db.add(entry)
        managed = OmsManagedPosition(id=identity("managed"), strategy_code=strategy.code,
            broker_account_name=account.name, symbol="AIOK", entry_order_id=entry.id,
            entry_client_order_id=entry.client_order_id, entry_price=Decimal("10.91"),
            original_quantity=27, current_quantity=27, entry_time=entry_time, status="open")
        db.add(managed)
        db.add(Fill(id=identity("buy-fill"), order_id=entry.id, strategy_id=strategy.id,
            broker_account_id=account.id, broker_fill_id="CONTROLLED-BOOT-BUY-FILL",
            symbol="AIOK", side="buy", quantity=Decimal(27), price=Decimal("10.91"),
            filled_at=entry_time, payload={"source": "CONTROLLED_NOT_NATIVE"}))
        db.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
            symbol="AIOK", quantity=Decimal(27), average_price=Decimal("10.91")))
        db.flush()
        wb2_binding = wbexit2.identity(entry, managed, account, strategy)
        state2 = wbexit2.begin(db, wb2_binding, quantity=Decimal(27), reason=REASON,
            originals=[], originals_proven=False, now=opened, entry_anchor=entry_time)
        state2 = wbexit2.transition(db, wb2_binding, state2["generation"], operation="acquire",
            now=opened, invocation="CONTROLLED-PRIOR-PROCESS-WB2-UNKNOWN")
        entry.payload = {"wbattach1": {"entry_id": str(entry.id), "managed_id": str(managed.id),
            "attempts": [], "wbexit2_owner_id": state2["owner_id"]}}
        # Full binding mirrors the existing persisted shape; it grants no call
        # context, terminal proof, provider receipt or current invocation.
        binding3 = {"managed_id": str(managed.id), "entry_id": str(entry.id),
            "entry_coid": entry.client_order_id, "entry_native_id": entry.broker_order_id,
            "strategy_id": str(strategy.id), "account_id": str(account.id),
            "account": account.name, "symbol": "AIOK", "entry_anchor_at": entry_time.isoformat()}
        owner3 = wbexit3._address(binding3, REASON, "CLOSE", "")
        state3 = {"source": "wbexit3", "schema": 1, "owner_id": str(owner3), "binding": binding3,
            "reason": REASON, "kind": "CLOSE", "level": "", "requested": "27",
            "reference_price": "10.91", "context": {"fixture": "CONTROLLED_NOT_NATIVE"},
            "attempts": 1, "initial_episode_current": True, "revision": 1,
            "opened_at": opened.isoformat(), "next_retry_at": opened.isoformat(), "phase": "OPEN",
            "active_token": "CONTROLLED-PRIOR-PROCESS-WB3-UNKNOWN", "last_result": "UNKNOWN",
            "filled_highwater": "0", "native_protection": "UNKNOWN", "last_guard": "original_terminal_UNKNOWN"}
        db.add(SystemIncident(id=owner3, service_name="oms-risk", severity="critical", status="open",
            title="CONTROLLED prior-day WB3 duty", opened_at=opened, payload=state3))
    return deepcopy(state2), deepcopy(state3)


async def run_narrow_cut(case):
    service, _, _, _, _, _, _ = case
    assert service.__dict__.get("_orbroute1_reconciliation_task") is None
    assert service.__dict__.get("_webull_identity_writer") is None
    service._wb3_narrow_private_cut_entered = True
    primary = None
    async def actual_cut():
        result2 = await wbexit2.hydrate_at_boot(service)
        await wbexit3.hydrate_at_boot(service)
        summary = await service._sync_broker_orders_pass(
            account_names=None, diagnostic_batch=OrderReadBatch())
        return result2, summary, service.__dict__.get("_orbroute1_reconciliation_task") is not None
    try:
        # Retain the original helper/pass waiter too. Cancelling this fixture
        # must not orphan an existing _run_db thread before ORB cleanup begins.
        return await _physical_join(actual_cut())
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            await drain_owned_cut_work(service)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note("narrow boot owned cleanup: " + repr(cleanup))
            service.logger.error("[CONTROLLED-BOOT-CLEANUP] secondary=%r primary=%r", cleanup, primary)


async def _physical_join(work):
    """Fixture-only: retain this exact task until physically finished on cancel."""
    task = work if isinstance(work, asyncio.Future) else asyncio.create_task(work)
    cancelled = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except BaseException:
            if not task.done():
                raise
    try:
        result = task.result()
    except BaseException as failure:
        if cancelled is not None:
            cancelled.add_note("owned task terminal failure: " + repr(failure))
            raise cancelled from failure
        raise
    if cancelled is not None:
        raise cancelled
    return result


async def drain_owned_cut_work(service):
    if not service.__dict__.get("_wb3_narrow_private_cut_entered"):
        return
    # Only this new private service's cut-created tracked task/writer: no
    # asyncio.all_tasks(), global adoption, native process or baseline rewrite.
    failure = None
    orb_task = service.__dict__.get("_orbroute1_reconciliation_task")
    try:
        if orb_task is not None:
            await _physical_join(orb_task)
    except BaseException as exc:
        failure = exc
    try:
        await _physical_join(service._drain_webull_order_identities())
    except BaseException as exc:
        if failure is None:
            failure = exc
        else:
            failure.add_note("identity drain terminal failure: " + repr(exc))
    pending = [task for task in service.__dict__.get("_webull_protect_tasks", ()) if not task.done()]
    if pending:
        # No unowned task is silently adopted just to pass a cleanup assertion.
        error = AssertionError("unexpected unjoined private protection work")
        if failure is None:
            failure = error
        else:
            failure.add_note(str(error))
    if failure is not None:
        raise failure


async def finish_case(case):
    """Every test exit joins owned work before disposal, retaining primary error."""
    primary = sys.exc_info()[1]
    try:
        await drain_owned_cut_work(case[0])
    except BaseException as cleanup:
        if primary is None:
            raise
        primary.add_note("narrow boot final cleanup: " + repr(cleanup))
        case[0].logger.error("[CONTROLLED-BOOT-CLEANUP] secondary=%r primary=%r", cleanup, primary)
    finally:
        case[4].dispose()


def receipt(case, *, real, on, before, after, result2, summary, orb_spawned, inventory):
    service, _, broker, redis, _, captured, binding = case
    return {"label": "REAL_EMPTY_WB_OWNED_PROJECTION" if real else "CONTROLLED_AGED_NOT_REAL_CLOSE",
        "cut": "WB2hydrate+WB3hydrate+ONE_sync_broker_orders_pass+tracked_ORB_drain",
        "WB3": on, "WB2": True, "captured_at": captured.isoformat(),
        "settings": binding["settings"], "constructor_unexported_columns": "CONTROLLED_PRIVATE_PLUMBING",
        "other_bootstrap_helpers": "UNKNOWN_NOT_EXECUTED", "prerequisite_counts": binding["prerequisite_counts"],
        "WB2_result": result2, "order_summary": summary, "ORB_task_spawned": orb_spawned,
        "provider_attempts": broker.calls, "Redis_attempts": redis.calls, "surviving_duties": inventory,
        "tables_unchanged": before == after, "native_protection": "UNKNOWN",
        "global_entry_GO": "NOT_CLAIMED", "full_initialize_or_run": "NOT_COVERED",
        "control_seed_called": False, "control_hydrated": False,
        "control_runtime_present": service._bot_control_runtime is not None}
