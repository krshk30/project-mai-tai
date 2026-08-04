"""Exit-only OCO pair against an ALREADY-HELD position (#646 Part 1).

⭐ THE HOLE THIS CLOSES. `_build_bracket_payload` is TRIGGER(entry) -> OCO(exits): it can only be
attached to an order we are placing. A position entered PRE-MARKET has no such parent — its entry
filled hours earlier as a plain single-leg order — and nothing in the OMS ever revisits it. So a
07:30 entry still held at 09:30 is never bracketed for its entire life, and rides the software
ladder all day. That ladder is the one that produced KUST: −5.17% on a signal that was right,
nine cancels against a bid that never once fell below the limit.

⛔⭐ THE SESSION RULE IS MEASURED, NOT ASSUMED (Probe P, 2026-08-04, preview-only).
Schwab answers a STOP leg in the extended-hours session with, verbatim:

    "This order type is not available for this session."   (originalSeverity: REJECT)

It fired for a single STOP leg, for TRIGGER->OCO, and for THIS exit-only shape — each against an
`session=NORMAL` control that ACCEPTED, so the reject is an answer and not an artefact. A LIMIT
*is* accepted in AM, but a limit cannot express a protective stop: a sell limit below market
executes immediately instead of waiting for adverse movement. Hence there is no such thing as a
native pre-market protective exit, and 09:30 is the broker's earliest arm point, not our choice.

⛔ STEP-1 PROOF STILL OWED. Probe P did NOT prove this shape is ACCEPTED — with the account flat,
its NORMAL control rejected on the oversold/position check. That establishes the session is fine
and leaves the shape unproven. It must be previewed against a REAL held position before the flag
goes on. These tests pin what we BUILD, not what the broker will answer.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.settings import Settings

BROKER_EH_REJECT = "This order type is not available for this session"


def _adapter() -> SchwabBrokerAdapter:
    return SchwabBrokerAdapter(
        Settings(oms_adapter="schwab", schwab_access_token="t", schwab_account_hash="h")
    )


def _exit_oco_request(**md: object) -> OrderRequest:
    metadata: dict[str, object] = {
        "exit_only_oco": "true",
        "session": "NORMAL",
        "bracket_target_price": "10.20",   # +2% off a 10.00 entry
        "bracket_stop_price": "9.50",      # -5%
    }
    metadata.update(md)
    return OrderRequest(
        client_order_id="schwab_1m_v2-KUST-close-abc123",
        broker_account_name="live:schwab_1m_v2",
        strategy_code="schwab_1m_v2",
        symbol="KUST",
        side="sell",
        intent_type="close",
        quantity=Decimal("2"),
        reason="oms_v2_rth_edge_bracket",
        order_type="limit",
        metadata=metadata,
    )


# --------------------------------------------------------------- the shape

def test_it_is_a_bare_oco_pair_with_no_trigger_parent() -> None:
    """The whole point: there is no entry to hang this off. A TRIGGER parent would try to BUY."""
    payload = _adapter()._build_exit_only_oco_payload(_exit_oco_request())
    assert payload["orderStrategyType"] == "OCO"
    assert "orderLegCollection" not in payload, "an exit-only OCO must have no parent order leg"
    assert len(payload["childOrderStrategies"]) == 2


def test_both_legs_SELL_one_LIMIT_target_one_STOP_protective_at_the_right_prices() -> None:
    """⛔ Pin the VALUES. A pair that is structurally right but priced wrong is a real-money loss,
    and a suite that only asserted 'it built something' would never catch it."""
    legs = _adapter()._build_exit_only_oco_payload(_exit_oco_request())["childOrderStrategies"]
    by_type = {leg["orderType"]: leg for leg in legs}
    assert set(by_type) == {"LIMIT", "STOP"}
    assert by_type["LIMIT"]["price"] == 10.20
    assert by_type["STOP"]["stopPrice"] == 9.50
    for leg in legs:
        assert leg["orderLegCollection"][0]["instruction"] == "SELL"
        assert leg["orderLegCollection"][0]["quantity"] == 2.0


# --------------------------------------------------------------- the measured session rule

@pytest.mark.parametrize("session", ["AM", "PM"])
def test_it_REFUSES_to_build_an_extended_hours_bracket(session: str) -> None:
    """⛔ Measured 2026-08-04. Refuse locally rather than emit an order the broker will certainly
    reject — and carry the broker's own words so the next reader does not have to re-derive them."""
    with pytest.raises(RuntimeError) as exc:
        _adapter()._build_exit_only_oco_payload(_exit_oco_request(session=session))
    assert BROKER_EH_REJECT in str(exc.value)
    assert session in str(exc.value)


def test_the_normal_session_is_the_only_one_that_builds() -> None:
    payload = _adapter()._build_exit_only_oco_payload(_exit_oco_request(session="NORMAL"))
    assert all(leg["session"] == "NORMAL" for leg in payload["childOrderStrategies"])


def test_a_missing_session_defaults_to_NORMAL_rather_than_raising() -> None:
    """Absent metadata must not become an EH order by accident."""
    req = _exit_oco_request()
    req.metadata.pop("session")
    payload = _adapter()._build_exit_only_oco_payload(req)
    assert all(leg["session"] == "NORMAL" for leg in payload["childOrderStrategies"])


# --------------------------------------------------------------- never half a bracket

@pytest.mark.parametrize("missing", ["bracket_target_price", "bracket_stop_price"])
def test_it_refuses_to_emit_half_an_OCO(missing: str) -> None:
    """One leg alone is not a bracket — it is an unpaired sell reserving the shares, which is the
    E5 oversell shape the OCO structure exists to eliminate."""
    req = _exit_oco_request()
    req.metadata.pop(missing)
    with pytest.raises(RuntimeError) as exc:
        _adapter()._build_exit_only_oco_payload(req)
    assert missing in str(exc.value)


# --------------------------------------------------------------- A5: don't perturb what works

def test_the_entry_bracket_legs_are_unchanged_at_NORMAL() -> None:
    """A5. `_bracket_exit_leg` gained a session parameter; its DEFAULT must stay NORMAL so the
    existing entry-bracket payload is byte-identical. A pre-market fix may not disturb the RTH
    flow that already works."""
    leg = _adapter()._bracket_exit_leg(
        _exit_oco_request(), order_type="LIMIT", price=Decimal("10.20")
    )
    assert leg["session"] == "NORMAL"


def test_exit_only_requests_are_recognised_and_plain_ones_are_not() -> None:
    adapter = _adapter()
    assert adapter._is_exit_only_oco_request(_exit_oco_request()) is True
    req = _exit_oco_request()
    req.metadata["exit_only_oco"] = "false"
    assert adapter._is_exit_only_oco_request(req) is False
    req.metadata.pop("exit_only_oco")
    assert adapter._is_exit_only_oco_request(req) is False
