"""The protective pair's trading-session tag — the root cause of every pre-market refusal.

⛔⭐⭐ THE MECHANISM (root-caused 2026-08-17 from a captured payload). Webull validates a
`CORE`-tagged order against the CORE reference — the PRIOR CLOSE — not the live extended-hours
tape. Pre-market on a gapper that is fatal:

    live IVF 2026-08-17 08:26 ET: bought 2.5300, stop 2.40, IVF prior close 0.9716
    -> 5x 417 STOP_LOSS_PRICE_LT_MARKETPRICE
       "The stop price of the stop-loss order should be lower than the current market price."

Our stop WAS below our entry. It was not below the prior close, and pre-market that is what Webull
compared it to. 100% of refusals were pre-market; every RTH fill got its bracket.

⛔ BROKER-VERIFIED ENUM, on the v3 COMBO endpoint specifically (2026-08-17 preview):
    CORE 200 · ALL_DAY 200 · ALL 417 · NIGHT 417 · Y 417
`ALL` ("include extended trading hours") is valid for a SINGLE-LEG order and REFUSED on the combo.
That asymmetry is why a first six-value probe concluded "CORE is the only value" — the combo
endpoint restricts the documented enum. **Do not "correct" ALL_DAY to ALL.**

⛔⛔ CI PROVES THE MAPPING, NOT THE FIX. Probe X established that `preview_order` validates
parameters and NOT position backing, so a 200 does not prove Webull stops comparing the stop to a
core reference. The only acceptance is a live pre-market fill producing `[WEBULL-PROTECT-ATTACHED]`
— which has never once been observed.
"""
from __future__ import annotations

import inspect
from decimal import Decimal
from types import SimpleNamespace

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.oms import service as svc


def _adapter() -> WebullBrokerAdapter:
    return object.__new__(WebullBrokerAdapter)


def _req(session_hint: str | None):
    meta = {"bracket_target_price": "5.10", "bracket_stop_price": "4.75",
            "webull_exit_only_pair": "true"}
    if session_hint is not None:
        meta["market_session"] = session_hint
    return SimpleNamespace(
        client_order_id="coid-1", symbol="TEST", side="sell", quantity=Decimal("2"),
        broker_account_name="live:orb", metadata=meta, time_in_force="day",
    )


def _sessions(legs) -> set[str]:
    return {leg["support_trading_session"] for leg in legs}


# --------------------------------------------------------------------- the mapping, both ways
def test_RTH_keeps_CORE() -> None:
    """⛔ NOT a blanket swap. RTH is PROVEN working — live SLE 2026-08-17 got its bracket 1.8s
    after the fill — and CORE carries none of ALL_DAY's overnight breadth."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req("RTH"))
    assert _sessions(legs) == {"CORE"}


def test_EXTENDED_sends_ALL_DAY() -> None:
    """The one value the COMBO endpoint accepts that spans pre-market."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req("EXTENDED"))
    assert _sessions(legs) == {"ALL_DAY"}


def test_BOTH_legs_carry_the_same_session() -> None:
    """A pair split across two sessions is not a pair. Both legs or neither."""
    for hint, expected in (("RTH", "CORE"), ("EXTENDED", "ALL_DAY")):
        legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req(hint))
        assert len(legs) == 2
        assert _sessions(legs) == {expected}


def test_ABSENT_metadata_defaults_to_CORE() -> None:
    """⛔ Every pre-existing caller must be byte-identical. A caller that never learned about the
    session must not silently start sending ALL_DAY."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req(None))
    assert _sessions(legs) == {"CORE"}


def test_an_UNKNOWN_hint_falls_back_to_CORE_not_to_extended() -> None:
    """Fail toward the narrow, proven value. An unrecognised hint must never widen the session."""
    legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req("WEEKEND"))
    assert _sessions(legs) == {"CORE"}


def test_ALL_is_NOT_used_it_is_417_on_the_combo_endpoint() -> None:
    """⛔ The trap. `ALL` reads like the obvious choice and is valid for a single-leg order, but the
    COMBO endpoint 417s it. Broker-verified 2026-08-17."""
    for hint in ("RTH", "EXTENDED"):
        legs = WebullBrokerAdapter._build_exit_only_pair_payload(_adapter(), _req(hint))
        assert "ALL" not in _sessions(legs), "ALL is refused by the combo endpoint"


# ------------------------------------------------------------- the OMS decides, the adapter maps
def _attach_body() -> str:
    """Source of `_attach_webull_protection` with its leading docstring removed.

    ⛔ The docstring names these very markers and the ALL_DAY value, so searching the raw source
    matches PROSE instead of CODE.

    ⛔ Bound the split to the FIRST docstring only. The function also contains a NESTED helper
    (`_retry_delay`) with its own docstring, so the source holds FOUR triple-quotes; an unbounded
    split yields 5 parts and `parts[2]` lands on the code BETWEEN the two docstrings, which holds
    none of the log calls.
    """
    src = inspect.getsource(svc.OmsRiskService._attach_webull_protection)
    parts = src.split('"""', 2)
    return parts[2] if len(parts) > 2 else src


def test_the_OMS_attach_is_rth_only_and_sends_the_rth_hint() -> None:
    """The broker-proven-impossible pre-market combo is refused before request construction."""
    body = _attach_body()
    assert "if not _is_regular_market_session():" in body
    assert 'session_hint = "RTH"' in body
    assert '"market_session": session_hint' in body
    assert '"EXTENDED"' not in body


def test_the_session_is_LOGGED_on_every_outcome_line() -> None:
    """⛔ If tomorrow's pre-market attach fails, the FIRST question is which string went out.
    That must never have to be inferred.

    ⛔ EVERY occurrence, not the first. `[WEBULL-PROTECT-RETRY]` has TWO sites — the `refused`
    branch and the `raised` branch — and checking only the first let a mutant that stripped the
    session from the refused line SURVIVE.

    ⛔ Each check is bounded to its OWN `self.logger(...)` call. A fixed-size window bleeds into
    the NEXT log statement, and since that one also carries `session=%s` the same mutant survived
    a second time. Split on the call boundary, not on a character count.
    """
    body = _attach_body()
    calls = body.split("self.logger")
    markers = ("[WEBULL-PROTECT-ATTACHED]", "[WEBULL-PROTECT-RETRY]", "[WEBULL-PROTECT-FAILED]")
    seen = {m: 0 for m in markers}
    for call in calls:
        for m in markers:
            if m in call:
                seen[m] += 1
                assert "session=%s" in call, f"{m} logs without the session actually sent: {call[:120]}"
    assert seen["[WEBULL-PROTECT-RETRY]"] >= 2, (
        f"expected BOTH retry sites (refused + raised), found {seen['[WEBULL-PROTECT-RETRY]']}"
    )
    for m in markers:
        assert seen[m] >= 1, f"{m} not found in any logger call"


def test_the_RTH_BRACKET_payload_is_untouched() -> None:
    """⛔ `_build_combo_payload` (the native RTH bracket) is a DIFFERENT builder that also sends
    CORE. It works. This change must not have touched it."""
    src = inspect.getsource(WebullBrokerAdapter._build_combo_payload)
    assert '"support_trading_session": "CORE"' in src, "the RTH bracket builder must stay hardcoded CORE"
