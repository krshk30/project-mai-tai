"""SOURCE UNRUN: literal exact1dfa begin oracle and OFF call-shape controls.

Main only after source/roster/private fence review. The OLD function is retained
raw Git source, not a reimplemented expected-state model. Its compile/exec below
is authored for Main's private execution; the worker has NOT executed/imported,
compiled or collected any project/test code.
"""
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import OmsManagedPosition, SystemIncident
from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from project_mai_tai.oms import wbexit2 as owner
from tests.unit.test_wbattach1_riskoff import make_case as make_case
from tests.unit.test_wbexit2_owner import bound
from tests.unit.test_wbexit2_route import owner_case, close_case, closes

ORIGINAL_REASONS = frozenset({"oms_v2_managed_exit:" + tag for tag in
    ("CW_FLIP", "CONFIRMATION_EXIT", "CW_TARGET", "CW_HARD_STOP", "CW_FLOOR")})


@pytest.fixture
def OLD_begin():
    path = Path(__file__).parents[1] / "fixtures/wbexit3/wbexit2_begin_exact_1dfa.py.txt"
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == "89292b281964d642636e52e4503cd9d1f8f0a26758ebc235df46a0d9e811adba"
    assert owner.SOURCE == "wbexit2" and owner.REASONS == ORIGINAL_REASONS
    assert owner.NAMESPACE == UUID("806dd329-8d11-4b42-95e6-146b0240798c")
    namespace = {"deepcopy": deepcopy, "Decimal": Decimal, "timedelta": timedelta,
        "SystemIncident": SystemIncident, "SOURCE": "wbexit2", "REASONS": ORIGINAL_REASONS,
        "timestamp": owner.timestamp, "positive_quantity": owner.positive_quantity,
        "read": owner.read, "address": owner.address}
    # Literal original begin body; these pure helpers are separately pinned as
    # identical to1dfa in the external source packet. No selector is overridden.
    exec(compile(raw, str(path), "exec"), namespace)
    return namespace["begin"]


@pytest.fixture
def OLD_quote_handler():
    path = Path(__file__).parents[1] / "fixtures/wbexit3/quote_handler_exact_1dfa.py.txt"
    raw = path.read_bytes()
    assert sha256(raw).hexdigest() == "a9a0a55ae9e09095a5c9f90cd852aa8c75d2bb3f573212b5bc7b13df666b4c34"
    namespace = {"QuoteTickEvent": QuoteTickEvent}
    # Exact old class-method body with class indentation removed, only loaded
    # by Main's future private test; no helper policy is replaced.
    exec(compile(raw, str(path), "exec"), namespace)
    return namespace["_handle_quote_tick_event"]


def inputs(case, reason, tree="proven", quantity="10"):
    originals = [{"base": "CONTROLLED-original", "quantity": quantity, "children": [
        {"coid": "CONTROLLED-T", "native_id": "CONTROLLED-native-T", "price": "10.20"},
        {"coid": "CONTROLLED-S", "native_id": "CONTROLLED-native-S", "price": "9.50"}]}]
    if tree == "bare":
        originals = []
    elif tree == "unknown":
        originals[0]["children"] = [{"coid": coid, "native_id": None, "price": None}
                                   for coid in ("CONTROLLED-T", "CONTROLLED-S")]
    return dict(quantity=Decimal(quantity), originals=originals, reason=reason, now=case[4][0],
        bare_proven=tree == "bare", originals_proven=tree != "unknown",
        native_account_id="CONTROLLED-native-account", entry_anchor=case[4][0] - timedelta(minutes=1),
        attempt_started=case[4][0] - timedelta(seconds=2), decision={"CONTROLLED": {"preserve": ["all", "fields"]}})


def persisted(case, binding):
    with case[1]() as db:
        row = db.get(SystemIncident, owner.address(binding))
        return {column.name: deepcopy(getattr(row, column.name)) for column in SystemIncident.__table__.columns}


@pytest.mark.parametrize("off_input", [{}, {"wbexit3_enabled": False}], ids=["default_OFF", "explicit_OFF"])
@pytest.mark.parametrize("reason,tree,quantity", [
    *((reason, "proven", "10") for reason in sorted(ORIGINAL_REASONS)),
    ("oms_v2_managed_exit:CW_FLIP", "bare", "1E+1"),
    ("oms_v2_managed_exit:CW_FLIP", "unknown", "1E+1"),
])
def test_OLD_and_OFF_return_AND_entire_persisted_incident_are_exactly_identical(
        make_case, OLD_begin, off_input, reason, tree, quantity):
    case = make_case()
    assert case[0].settings.oms_webull_exit3_enabled is False
    with case[1].begin() as db:
        binding = bound(db, case)
        expected = OLD_begin(db, binding, **inputs(case, reason, tree, quantity))
    old_row = persisted(case, binding)
    # Reset only this private differential incident; retain the SAME actual
    # entry/managed/account IDs and clock, so no fields are normalized away.
    with case[1].begin() as db:
        db.delete(db.get(SystemIncident, owner.address(binding)))
    with case[1].begin() as db:
        actual = owner.begin(db, binding, **inputs(case, reason, tree, quantity), **off_input)
    off_row = persisted(case, binding)
    assert actual == expected and off_row == old_row
    assert json.dumps(actual, sort_keys=True, separators=(",", ":")) == json.dumps(expected, sort_keys=True, separators=(",", ":"))
    assert "prior_software_filled" not in actual and "prior_software_filled" not in off_row["payload"]
    assert actual["remaining"] == quantity  # Includes the original Decimal exponent spelling.


@pytest.mark.parametrize("off_input", [{}, {"wbexit3_enabled": False}], ids=["default_OFF", "explicit_OFF"])
@pytest.mark.parametrize("reason", ["oms_v2_managed_exit:HARD_STOP", "oms_v2_managed_exit:FLOOR_BREACH",
    "V2_OVERNIGHT_FLATTEN", "V2_EOD_CANCEL_REEXIT", "WEBULL_ATTACH_RISK_OFF",
    "oms_v2_managed_exit:SCALE_PCT2", "CONTROLLED-unknown"])
def test_OLD_and_OFF_reject_same_extra_reason_before_creating_durable_state(make_case, OLD_begin, off_input, reason):
    case = make_case()
    with case[1].begin() as db:
        binding = bound(db, case)
        for begin, extra in ((OLD_begin, {}), (owner.begin, off_input)):
            with pytest.raises(ValueError) as refused:
                begin(db, binding, **inputs(case, reason), **extra)
            assert str(refused.value) == "wbexit2_exit_reason_not_covered"
            assert db.get(SystemIncident, owner.address(binding)) is None


def test_new_reason_and_provenance_require_EXPLICIT_True_input(make_case):
    case = make_case()
    with case[1].begin() as db:
        binding = bound(db, case)
        with pytest.raises(ValueError, match="exit_reason_not_covered"):
            owner.begin(db, binding, **inputs(case, "oms_v2_managed_exit:HARD_STOP"), wbexit3_enabled=1)
        with pytest.raises(ValueError, match="requires_enabled_input"):
            owner.begin(db, binding, **inputs(case, "oms_v2_managed_exit:CW_FLIP"), prior_software_filled=Decimal(3))
        assert db.get(SystemIncident, owner.address(binding)) is None
        state = owner.begin(db, binding, **inputs(case, "oms_v2_managed_exit:HARD_STOP"),
            wbexit3_enabled=True, prior_software_filled=Decimal(3))
        assert state["original_quantity"] == "10" and state["remaining"] == "7"
        assert state["prior_software_filled"] == "3"


@pytest.mark.asyncio
async def test_OFF_real_load_caller_keeps_original_strict_begin_kwargs(make_case, monkeypatch):
    case = await owner_case(make_case)
    assert case[0].settings.oms_webull_exit3_enabled is False
    actual_begin, observed = owner.begin, []
    def strict_original_signature(db, binding, *, quantity, originals, reason, now, bare_proven=False,
            decision=None, attempt_started=None, originals_proven=True, native_account_id=None, entry_anchor=None):
        kwargs = dict(quantity=quantity, originals=originals, reason=reason, now=now, bare_proven=bare_proven,
            decision=decision, attempt_started=attempt_started, originals_proven=originals_proven,
            native_account_id=native_account_id, entry_anchor=entry_anchor)
        state = actual_begin(db, binding, **kwargs)
        observed.append(deepcopy(state))
        return state  # ACTUAL durable reducer, never a proof-success stub.
    monkeypatch.setattr(owner, "begin", strict_original_signature)
    await close_case(case, "CW_HARD_STOP")
    assert len(observed) == 1 and "prior_software_filled" not in observed[0]
    assert len(closes(case[2])) == 1


@pytest.mark.asyncio
async def test_OFF_scale_keeps_original_strict_managed_sell_kwargs(make_case, monkeypatch):
    service, factory, port, _, _ = make_case()
    assert service.settings.oms_webull_exit3_enabled is False
    service.__dict__.pop("_v2_eod_handover_ready", None)
    service.__dict__.pop("_fetch_quote_for_order", None)
    actual, seen = service._emit_v2_managed_sell, []
    async def strict_original_signature(db, row, *, intent_type, quantity, reference_price, reason, bid=None, decided_at=None):
        kwargs = dict(intent_type=intent_type, quantity=quantity, reference_price=reference_price,
            reason=reason, bid=bid, decided_at=decided_at)
        seen.append(dict(kwargs))
        return await actual(db, row, **kwargs)  # Original policy/proof path remains real.
    monkeypatch.setattr(service, "_emit_v2_managed_sell", strict_original_signature)
    with factory() as db:
        row = db.scalar(select(OmsManagedPosition))
        position, row_id = service._hydrate_v2_position(row), str(row.id)
    await service._emit_v2_exit_on_loop_claimed(port.account, "ABC", position, 10., kind="SCALE",
        reference_price=10.2, reason="oms_v2_managed_exit:SCALE_PCT2", bid=10.2, close_on_fill=True,
        sell_qty=3, level="PCT2", expected_managed_row_id=row_id,
        confirmation_context={"CONTROLLED-present-context": "must-not-add-OFF-kwarg"})
    assert len(seen) == 1 and seen[0]["intent_type"] == "scale" and seen[0]["quantity"] == 3


@pytest.mark.asyncio
async def test_OLD_OFF_closed_quote_preserves_exact_account_calls_and_state(make_case, monkeypatch, OLD_quote_handler):
    service, _, port, _, clock = make_case()
    assert service.settings.oms_webull_exit3_enabled is False
    clock[0] = clock[0].replace(hour=1)  # Actual weekday21:00 ET, outside7–20.
    assert service._market_is_fillable() is False
    actual_accounts, actual_capture = service._v2_accounts, service._capture_cw_target_decision
    counts = {"accounts": 0, "capture": 0}
    def accounts():
        counts["accounts"] += 1
        return actual_accounts()
    def capture(*args, **kwargs):
        counts["capture"] += 1
        return actual_capture(*args, **kwargs)
    monkeypatch.setattr(service, "_v2_accounts", accounts)
    monkeypatch.setattr(service, "_capture_cw_target_decision", capture)
    service._managed_v2_symbols.add((port.account, "ABC"))
    event = QuoteTickEvent(source_service="CONTROLLED-OFF", produced_at=clock[0],
        payload=QuoteTickPayload(symbol="ABC", bid_price=Decimal(10), ask_price=Decimal("10.01")))
    quotes_before = deepcopy(service._latest_quotes_by_symbol)
    target_before = deepcopy(service._cw_target_decisions)
    await OLD_quote_handler(service, event)
    old_counts = dict(counts)
    old_quotes = deepcopy(service._latest_quotes_by_symbol)
    assert old_counts["capture"] == 0 and service._cw_target_decisions == target_before
    assert not service.__dict__.get("_symbol_tick_work") and not port.requests
    service._latest_quotes_by_symbol = quotes_before
    counts.update(accounts=0, capture=0)
    await service._handle_quote_tick_event(event)
    assert counts == old_counts and service._latest_quotes_by_symbol == old_quotes
    assert service._cw_target_decisions == target_before
    assert not service.__dict__.get("_symbol_tick_work") and not port.requests
