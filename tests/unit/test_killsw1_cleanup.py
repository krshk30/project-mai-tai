from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.oms.bot_control_cleanup import OwnedCleanup, plan_cleanup

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 15, 10, 0, tzinfo=ET)
STRATEGY, ACCOUNT = uuid4(), uuid4()


def evidence(**overrides):
    values = dict(
        strategy_id=STRATEGY,
        account_id=ACCOUNT,
        symbol="BBCI",
        binding_key="entry:55e84cc9",
        generation=1,
        provider="webull",
        owner_proven=True,
        own_remaining=Decimal(25),
        broker_available=Decimal(25),
        observed_at=NOW,
        entry_terminal=True,
        protection_terminal=True,
        bid=Decimal("3.879"),
        bid_at=NOW,
    )
    values.update(overrides)
    return OwnedCleanup(**values)


def plan(value, now=NOW):
    return plan_cleanup(value, strategy_id=STRATEGY, account_id=ACCOUNT, generation=1, now=now)


@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize(
    "hour,minute,kind,session",
    [
        (8, 0, "limit", "AM"),
        (9, 29, "limit", "AM"),
        (9, 30, "market", "NORMAL"),
        (15, 59, "market", "NORMAL"),
        (16, 0, "limit", "PM"),
        (19, 59, "limit", "PM"),
    ],
)
def test_exact_owned_exit_each_broker_session(provider, hour, minute, kind, session):
    stamp = NOW.replace(hour=hour, minute=minute)
    value = evidence(provider=provider, observed_at=stamp, bid_at=stamp)
    result = plan(value, stamp)
    assert result.action == "SUBMIT_EXIT" and result.quantity == 25
    assert result.order_type == kind and result.session == session
    assert result.limit_price == (Decimal("3.87") if kind == "limit" else None)


@pytest.mark.parametrize("provider,allowed", [("webull", True), ("schwab", False)])
def test_early_premarket_is_provider_specific_no_reroute(provider, allowed):
    stamp = NOW.replace(hour=5)
    result = plan(evidence(provider=provider, observed_at=stamp, bid_at=stamp), stamp)
    assert (result.action == "SUBMIT_EXIT") is allowed
    if not allowed:
        assert result.reason == "broker_session_unsupported_keep_protection"


@pytest.mark.parametrize(
    "changes",
    [
        {"strategy_id": uuid4()},
        {"account_id": uuid4()},
        {"generation": 2},
        {"owner_proven": False},
        {"binding_key": ""},
        {"symbol": ""},
    ],
)
def test_wrong_owner_never_cancels_or_sells(changes):
    assert plan(evidence(**changes)).action == "BLOCKED"


@pytest.mark.parametrize("quantity", [None, Decimal(-1), Decimal("NaN"), Decimal("Infinity")])
def test_unknown_owned_quantity_never_borrows_net_account(quantity):
    result = plan(evidence(own_remaining=quantity, broker_available=Decimal(1000)))
    assert result.action == "BLOCKED" and result.reason == "owned_quantity_unknown"


def test_exit_is_min_of_proven_own_and_available_not_account_total():
    assert plan(evidence(broker_available=Decimal(1000))).quantity == 25
    assert plan(evidence(broker_available=Decimal(7))).quantity == 7
    assert plan(evidence(broker_available=None)).action == "BLOCKED"
    assert plan(evidence(broker_available=Decimal(0))).action == "BLOCKED"


def test_entry_cancellation_precedes_quantity_or_protection_cleanup():
    value = evidence(entry_terminal=False, entry_order_ids=("own-entry",), own_remaining=None)
    result = plan(value)
    assert result.action == "CANCEL_ENTRY" and result.order_ids == ("own-entry",)
    assert plan(replace(value, entry_order_ids=())).action == "BLOCKED"
    assert plan(replace(value, entry_terminal=None)).action == "BLOCKED"


def test_protection_cancel_ack_is_not_terminal_and_quote_needed_before_retirement():
    stamp = NOW.replace(hour=8)
    value = evidence(
        observed_at=stamp,
        bid_at=stamp,
        protection_terminal=False,
        protection_order_ids=("own-stop", "own-target"),
    )
    assert plan(value, stamp).action == "CANCEL_PROTECTION"
    # Until the terminal read is proven, this decision must never sell.
    assert plan(value, stamp).quantity is None
    terminal = replace(value, protection_terminal=True)
    assert plan(terminal, stamp).action == "SUBMIT_EXIT"
    stale = replace(value, bid_at=stamp - timedelta(seconds=3))
    assert plan(stale, stamp).reason == "executable_bid_not_fresh_keep_protection"


@pytest.mark.parametrize("seconds", [-1, 3])
def test_future_or_stale_owner_evidence_blocks(seconds):
    assert plan(evidence(observed_at=NOW - timedelta(seconds=seconds))).action == "BLOCKED"


def test_existing_uncertain_exit_even_flat_prevents_false_completion():
    result = plan(evidence(own_remaining=Decimal(0), exit_working_or_uncertain=True))
    assert result.action == "WAIT" and result.reason == "existing_exit_requires_reconciliation"


def test_target_fill_zero_never_emits_replacement_sell():
    assert plan(evidence(own_remaining=Decimal(0))).action == "SETTLED"
    result = plan(
        evidence(
            own_remaining=Decimal(0),
            protection_terminal=False,
            protection_order_ids=("remaining-child",),
        )
    )
    assert result.action == "CANCEL_PROTECTION" and result.quantity is None


@pytest.mark.parametrize("hour", [3, 20, 23])
def test_outside_session_keeps_protection(hour):
    stamp = NOW.replace(hour=hour)
    assert plan(evidence(observed_at=stamp, bid_at=stamp), stamp).action == "BLOCKED"


def test_sub_dollar_bid_uses_conservative_tick():
    stamp = NOW.replace(hour=8)
    result = plan(evidence(observed_at=stamp, bid_at=stamp, bid=Decimal("0.98765")), stamp)
    assert result.limit_price == Decimal("0.9876")


@pytest.mark.parametrize("ids", [("",), (" ",), (" own-id",), ("same", "same")])
def test_unaddressable_or_duplicated_child_ids_never_cancel(ids):
    assert plan(evidence(entry_terminal=False, entry_order_ids=ids)).action == "BLOCKED"
    assert plan(evidence(protection_terminal=False, protection_order_ids=ids)).action == "BLOCKED"


def test_quote_expiring_after_protection_release_does_not_authorize_blind_exit():
    stamp = NOW.replace(hour=8)
    value = evidence(
        observed_at=stamp,
        bid_at=stamp,
        protection_terminal=False,
        protection_order_ids=("stop", "target"),
    )
    assert plan(value, stamp).action == "CANCEL_PROTECTION"
    later = stamp + timedelta(seconds=3)
    value = replace(value, observed_at=later, protection_terminal=True)
    result = plan(value, later)
    assert result.action == "BLOCKED" and result.quantity is None


def test_weekend_or_unknown_clock_cannot_retire_protection():
    saturday = NOW.replace(day=17)
    result = plan(
        evidence(
            observed_at=saturday,
            bid_at=saturday,
            protection_terminal=False,
            protection_order_ids=("stop",),
        ),
        saturday,
    )
    assert result.action == "BLOCKED"
    assert plan(evidence(), NOW.replace(tzinfo=None)).reason == "clock_unknown"
