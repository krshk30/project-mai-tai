"""SOURCE UNRUN. Existing application/typed private port; no native replay claim.

Main must review/compose the exact external shared proposal and private SQLite
fixture fence before executing. Historical labels bind causes, not missing native
geometry, book completeness, propagation latency or physical elapsed timers.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import AccountPosition, BrokerOrder, Fill, OmsManagedPosition, SystemIncident, TradeIntent, VirtualPosition
from project_mai_tai.oms import service as oms, wbexit2, wbexit3
from project_mai_tai.oms import webull_attach_riskoff as wb
from project_mai_tai.settings import Settings
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes, Redis
from tests.unit.test_wbexit2_route import ControlledOwnerBroker, close_case, owner_case


def log_case(key):
    packet = json.loads((Path(__file__).parents[1] / "fixtures/wbexit3/log_reconstructed_cases.json").read_text())
    return packet, next(row for row in packet["rows"] if row["id"] == key)


def obligations(case):
    with case[1]() as db:
        return [dict(row.payload) for row in db.scalars(select(SystemIncident).where(
            SystemIncident.payload["source"].as_string() == "wbexit3"))]


async def enable(case, on=True):
    object.__setattr__(case[0].settings, "oms_webull_exit3_enabled", on)
    await wbexit3.hydrate_at_boot(case[0])
    return case


def restarted(case):
    old, factory, broker, entry_id, clock = case
    service = oms.OmsRiskService(settings=old.settings, redis_client=Redis(),
        session_factory=factory, broker_adapter=broker)
    broker.service = service
    return service, factory, broker, entry_id, clock


def test_code_default_OFF_and_no_Schwab_or_manual_selector():
    assert Settings.model_fields["oms_webull_exit3_enabled"].default is False
    service = SimpleNamespace(settings=SimpleNamespace(oms_webull_exit3_enabled=True,
        provider_for_account=lambda account: "webull" if account == "live:orb" else "schwab"))
    now = datetime(2026, 10, 26, 11, 53, tzinfo=UTC)
    for account in ("live:schwab_1m_v2", "paper:orb", "manual"):
        assert not wbexit3.selected(service, "oms_v2_managed_exit:CW_FLIP", account, now=now)


@pytest.mark.parametrize("stamp,expected", [
    ("2026-10-26T07:59:59+00:00", "CLOSED"), ("2026-10-26T08:00:00+00:00", "PM"),
    ("2026-10-26T13:29:59+00:00", "PM"), ("2026-10-26T13:30:00+00:00", "RTH"),
    ("2026-10-26T20:00:00+00:00", "AH"), ("2026-10-27T00:00:00+00:00", "CLOSED"),
    ("2026-10-31T13:30:00+00:00", "CLOSED"), ("2026-12-25T15:30:00+00:00", "CLOSED"),
])
def test_exit_only_calendar_boundary(stamp, expected):
    assert wbexit3.session(datetime.fromisoformat(stamp)) == expected


@pytest.mark.asyncio
async def test_flag_OFF_wrapper_is_byte_path_no_DB_or_provider():
    service = SimpleNamespace(settings=SimpleNamespace(oms_webull_exit3_enabled=False))
    sentinel = object()
    async def original(actual, *args, **kwargs):
        assert actual is service
        return sentinel
    assert await wbexit3.obligated_exit(original)(service) is sentinel
    assert not hasattr(service, "_wbexit3_pending")


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["OLD-WB2", "NEW-WB3"])
@pytest.mark.parametrize("row,tag,cause", [
    ("AIXI-1020", "CW_HARD_STOP", "submitting"),
    ("AIOK-1023-1448", "CONFIRMATION_EXIT", "positions_unknown"),
    ("AIOK-1023-1504", "CW_TARGET", "ledger_zero"),
    ("ELPW-1028-0931", "CW_FLIP", "terminal_lag"),
])
async def test_retained_four_RTH_causes_OLD_NEW(make_case, record_property, on, row, tag, cause):
    packet, operands = log_case(row)
    case = await owner_case(make_case, symbol=operands["symbol"], quantity=operands["quantity"],
        entry=operands["entry_price"], at_clock=datetime.fromisoformat(operands["clock_utc"]))
    await enable(case, on)
    if cause == "submitting":
        with case[1].begin() as db:
            entry = db.get(BrokerOrder, case[3])
            journal = dict(entry.payload[wb.KEY])
            journal["attempts"] = [{**a, "phase": "submitting"} for a in journal["attempts"]]
            entry.payload = {**entry.payload, wb.KEY: journal}
    elif cause == "positions_unknown":
        case[2].positions_unknown = True
    elif cause == "ledger_zero":
        with case[1].begin() as db:
            db.scalar(select(VirtualPosition)).quantity = Decimal(0)  # Controlled adverse preimage; no new fill.
    else:
        case[2].lag = True
    first = await close_case(case, tag)
    ending = first
    receipts = obligations(case)
    if not on:
        assert receipts == []
    else:
        assert len(receipts) == 1 and receipts[0]["active_token"] is None
        assert Decimal(receipts[0]["requested"]) == case[2].owned
    if cause in {"positions_unknown", "ledger_zero", "terminal_lag"}:
        assert not closes(case[2])
        with case[1]() as db:
            assert db.scalar(select(OmsManagedPosition)).status == "open"
    if on and cause == "submitting":
        assert len(closes(case[2])) == 1 and receipts[0]["phase"] == "RESOLVED"
    if on and cause == "ledger_zero":
        assert case[2].positions_reads > 0
        for _ in range(2):
            case[4][0] += timedelta(seconds=61)
            await close_case(case, tag)
        assert obligations(case)[0]["phase"] == "PAGED"
        with case[1]() as db:
            assert db.scalar(select(VirtualPosition.quantity)) == 0
            assert len(list(db.scalars(select(Fill)))) == 1  # Original BUY only; never invent ownership fills.
        ending = "PAGED_ROW_OPEN_PROOF_UNAVAILABLE"
    if cause in {"positions_unknown", "terminal_lag"}:
        case[2].positions_unknown, case[2].lag = False, False
        case[4][0] += timedelta(seconds=11)
        quote = {"received_at": case[4][0], "bid": case[2].bid}
        await (wbexit3.resume_on_quote(case[0], case[2].account, case[2].symbol, quote) if on
               else wbexit2.resume_on_quote(case[0], case[2].account, case[2].symbol, quote))
        assert len(closes(case[2])) == 1 and case[2].total == 1000
        ending = "SOLD_AFTER_POSITIVE_FRESH_READ"
        if on:
            assert obligations(case)[0]["phase"] == "RESOLVED"
    record_property("WBEXIT3_original_case", json.dumps({"row": row, "cause": cause, "flag": on,
        "first_outcome": first, "ending": ending, "wire_sells": len(closes(case[2])), "responsibility": obligations(case),
        "log_operands": operands, "label": packet["label"],
        "historical_native_inputs": "UNKNOWN_CONTROLLED_PORT_ONLY", "physical_10s_verdict": "UNMEASURED"}, default=str))


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["OLD-WB2", "NEW-WB3"])
async def test_SPRC_PM_bare_owned_cause_has_durable_retry_and_EH_limit(make_case, monkeypatch, record_property, on):
    # The retained SPRC packet lacks native geometry. This uses an explicitly
    # CONTROLLED bare fill, not a fictional pre-market native stop.
    case = make_case()
    service, factory, old, entry_id, clock = case
    clock[0] = datetime(2026, 10, 26, 11, 53, tzinfo=UTC)
    monkeypatch.setattr(oms, "_extended_hours_session", lambda now=None: "AM")
    broker = ControlledOwnerBroker(service, factory, old.account)
    broker.symbol = "SPRC"
    service.broker_adapter = broker
    case = service, factory, broker, entry_id, clock
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_wbexit2_enabled", True)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", True)
    object.__setattr__(service.settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", True)
    service.__dict__.pop("_v2_eod_handover_ready", None)
    service.__dict__.pop("_fetch_quote_for_order", None)
    with factory.begin() as db:
        entry = db.get(BrokerOrder, entry_id)
        entry.symbol = "SPRC"
        entry.created_at = entry.submitted_at = clock[0]
        intent = db.get(TradeIntent, entry.intent_id)
        intent.created_at, intent.symbol = clock[0], "SPRC"
        fill = db.scalar(select(Fill))
        fill.filled_at, fill.symbol = clock[0], "SPRC"
        managed = db.scalar(select(OmsManagedPosition))
        managed.symbol, managed.entry_time = "SPRC", clock[0]
        db.scalar(select(VirtualPosition)).symbol = "SPRC"
        db.scalar(select(AccountPosition)).symbol = "SPRC"
    await wbexit2.hydrate_at_boot(service)
    await enable(case, on)
    broker.positions_unknown = True
    await close_case(case, "CW_FLIP")
    assert not closes(broker)
    if on:
        assert obligations(case)[0]["phase"] == "RETRY"
    broker.positions_unknown = False
    clock[0] += timedelta(seconds=11)
    await close_case(case, "CW_FLIP")
    for order in closes(broker):
        assert order.order_type == "limit" and order.metadata["session"] == "AM"
        assert order.quantity <= Decimal(10)
    assert broker.total >= Decimal(1000)
    if on:
        assert len(closes(broker)) == 1 and obligations(case)[0]["phase"] == "RESOLVED"
    packet, operands = log_case("SPRC-1026-0753")
    record_property("SPRC_0753", json.dumps({"label": packet["label"], "operands": operands, "flag": on,
        "wire_sells": len(closes(broker)), "ending": "SOLD" if closes(broker) else "PAGED_OR_OWNED_WAIT",
        "assumption": "CONTROLLED_bare_quantity10;actual_native_chronology_UNKNOWN"}))


@pytest.mark.asyncio
async def test_next_day_boot_keeps_original_obligation_and_orphan_nonce_UNKNOWN(make_case):
    case = await owner_case(make_case)
    await enable(case)
    case[2].positions_unknown = True
    await close_case(case, "CW_HARD_STOP")
    before = obligations(case)[0]
    after_case = restarted(case)
    case[4][0] += timedelta(days=1)
    await wbexit2.hydrate_at_boot(after_case[0])
    await wbexit3.hydrate_at_boot(after_case[0])
    assert after_case[0]._wbexit3_pending[before["owner_id"]]["opened_at"] == before["opened_at"]
    after_case[2].positions_unknown = False
    await close_case(after_case, "CW_HARD_STOP")
    await wbexit3.refresh_pending(after_case[0])
    assert obligations(after_case)[0]["owner_id"] == before["owner_id"]
    assert obligations(after_case)[0]["phase"] == "RESOLVED"
    assert len(closes(after_case[2])) == 1
    assert after_case[2].total >= Decimal(1000)


@pytest.mark.asyncio
async def test_missing_journal_reconstructs_only_positive_original_children(make_case):
    case = await owner_case(make_case)
    await enable(case)
    with case[1].begin() as db:
        entry = db.get(BrokerOrder, case[3])
        entry.payload = {k: v for k, v in entry.payload.items() if k != wb.KEY}
    await close_case(case, "CW_HARD_STOP")
    assert len(closes(case[2])) <= 1
    with case[1]() as db:
        entry = db.get(BrokerOrder, case[3])
        assert entry.payload[wb.KEY]["history_proven"] is True
        assert len(entry.payload[wb.KEY]["attempts"]) == 1
    assert case[2].total >= Decimal(1000)


@pytest.mark.asyncio
async def test_actual_existing_position_sync_restores_owned_ledger_before_retry(make_case):
    case = await owner_case(make_case)
    await enable(case)
    with case[1].begin() as db:
        db.scalar(select(VirtualPosition)).quantity = Decimal(0)
    await close_case(case, "CW_HARD_STOP")
    assert not closes(case[2])
    # Run the real existing own-managed restoration, not a permission override or
    # a test-created SELL/BUY fill. The complete private position port is external.
    await case[0].sync_broker_positions(account_names=[case[2].account])
    with case[1]() as db:
        assert db.scalar(select(VirtualPosition.quantity)) == 10
        assert len(list(db.scalars(select(Fill)))) == 1
    case[4][0] += timedelta(seconds=11)
    await close_case(case, "CW_HARD_STOP")
    assert len(closes(case[2])) == 1 and case[2].total == Decimal(1000)
    assert obligations(case)[0]["phase"] == "RESOLVED"


@pytest.mark.asyncio
async def test_next_day_orphan_nonce_is_reported_without_adoption_or_wire(make_case):
    case = await owner_case(make_case)
    await enable(case)
    case[2].positions_unknown = True
    await close_case(case, "CW_HARD_STOP")
    before = obligations(case)[0]
    # Controlled crash preimage: prior physical quiescence is unproved.
    with case[1].begin() as db:
        row = db.get(SystemIncident, UUID(before["owner_id"]))
        row.payload = {**row.payload, "active_token": "CONTROLLED-prior-physical-invocation"}
    after = restarted(case)
    case[4][0] += timedelta(days=1)
    await wbexit2.hydrate_at_boot(after[0])
    await wbexit3.hydrate_at_boot(after[0])
    reads = case[2].positions_reads
    requests = len(case[2].requests)
    outcome = await close_case(after, "CW_HARD_STOP")
    assert outcome == "wbexit3_prior_physical_invocation_UNKNOWN"
    assert case[2].positions_reads == reads and len(case[2].requests) == requests
    assert obligations(after)[0]["active_token"] == "CONTROLLED-prior-physical-invocation"
    assert after[0]._wbexit3_pending[before["owner_id"]]["opened_at"] == before["opened_at"]


@pytest.mark.asyncio
async def test_no_quote_still_pages_retained_duty_on_existing_sync_scan(make_case):
    case = await owner_case(make_case)
    await enable(case)
    case[2].positions_unknown = True
    await close_case(case, "CW_HARD_STOP")
    state = obligations(case)[0]
    case[4][0] += timedelta(seconds=31)
    reads, requests = case[2].positions_reads, len(case[2].requests)
    wbexit3.report_pending(case[0])
    await wbexit3.refresh_pending(case[0])
    assert obligations(case)[0]["phase"] == "PAGED"
    assert case[2].positions_reads == reads and len(case[2].requests) == requests
    assert obligations(case)[0]["opened_at"] == state["opened_at"]
    with case[1]() as db:
        assert db.scalar(select(OmsManagedPosition.status)) == "open"


@pytest.mark.asyncio
async def test_full_original_child_execution_resolves_duty_from_real_fills(make_case):
    case = await owner_case(make_case)
    await enable(case)
    case[2].child_filled = Decimal(10)
    await close_case(case, "CW_HARD_STOP")
    assert not closes(case[2]) and case[2].total == Decimal(1000)
    assert obligations(case)[0]["phase"] == "RESOLVED"
    with case[1]() as db:
        assert db.scalar(select(OmsManagedPosition.status)) == "closed"
        assert db.scalar(select(VirtualPosition.quantity)) == 0
        assert sum((f.quantity for f in db.scalars(select(Fill).where(Fill.side == "sell"))), Decimal(0)) == 10


@pytest.mark.asyncio
async def test_changed_original_native_identity_HOLD_PAGE_never_linked_completion(make_case, monkeypatch, record_property):
    from dataclasses import replace
    case = await owner_case(make_case)
    await enable(case)
    case[2].lag = True
    await close_case(case, "CW_HARD_STOP")
    assert len(case[2].cancelled) == 1 and not closes(case[2])
    actual_read = case[2].confirm_exit_pair_from_today_list
    async def changed(**binding):
        result = await actual_read(**binding)
        return replace(result, reports=tuple(replace(r, broker_order_id="CONTROLLED-changed-" + r.broker_order_id)
            for r in result.reports))
    monkeypatch.setattr(case[2], "confirm_exit_pair_from_today_list", changed)
    case[2].lag = False
    for _ in range(2):
        case[4][0] += timedelta(seconds=61)
        await close_case(case, "CW_HARD_STOP")
    assert obligations(case)[0]["phase"] == "PAGED" and not closes(case[2])
    assert len(case[2].cancelled) == 1
    with case[1]() as db:
        assert db.scalar(select(OmsManagedPosition.status)) == "open"
        assert db.scalar(select(VirtualPosition.quantity)) == 10
    record_property("replacement", "NOT COVERED (no adapter link traversal); bounded HOLD/PAGE; row OPEN; software ladder retained")


@pytest.mark.asyncio
@pytest.mark.parametrize("episode", ["GIPR-170707", "GIPR-180510", "IMCC-174604"])
async def test_retained_double_cancel_and_429_answers_stay_UNKNOWN_until_read(make_case, episode, record_property):
    from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
    from tests.webull_confirmation_exit_fixtures import (
        gipr_170707_order_cannot_cancel, gipr_170707_detail_rate_limited,
        gipr_180510_order_cannot_cancel, gipr_180511_detail_rate_limited,
        imcc_174604_cancelled_and_rate_limited,
    )
    class RetainedAnswers(ControlledOwnerBroker):
        unreadable = True

        async def cancel_exit_pair(self, **binding):
            await super().cancel_exit_pair(**binding)
            fn = gipr_170707_order_cannot_cancel if episode == "GIPR-170707" else gipr_180510_order_cannot_cancel
            return [fn(binding["base_client_order_id"], suffix, symbol=self.symbol) for suffix in ("T", "S")]

        async def confirm_exit_pair_from_today_list(self, **binding):
            if self.cancelled and self.unreadable:
                base = binding["base_client_order_id"]
                if episode == "IMCC-174604":
                    reports = imcc_174604_cancelled_and_rate_limited(base, symbol=self.symbol)
                else:
                    fn = gipr_170707_detail_rate_limited if episode == "GIPR-170707" else gipr_180511_detail_rate_limited
                    reports = tuple(fn(base, suffix, symbol=self.symbol) for suffix in ("T", "S"))
                return ExitPairReleaseResult(outcome="unanswerable", reports=reports)
            return await super().confirm_exit_pair_from_today_list(**binding)
    case = await owner_case(make_case, port_type=RetainedAnswers)
    await enable(case)
    await close_case(case, "CW_HARD_STOP")
    assert not closes(case[2]) and obligations(case)[0]["phase"] == "RETRY"
    case[2].unreadable = False
    case[4][0] += timedelta(seconds=11)
    await close_case(case, "CW_HARD_STOP")
    assert len(case[2].cancelled) == 1 and len(closes(case[2])) == 1
    assert case[2].total == Decimal(1000) and obligations(case)[0]["phase"] == "RESOLVED"
    record_property("retained_answer_case", episode + ": controlled native census; positive terminal read gates SELL")
