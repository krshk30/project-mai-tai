"""Every [V2-OCO-EMIT] line must name its broker account.

⛔⭐ WHY THIS IS LOAD-BEARING, not cosmetic. The fan-out puts BOTH real-money accounts on the SAME
symbol at the SAME moment (`live:schwab_1m_v2` direct + `live:orb` Webull leg). This marker was the
only one in the exit-protection picture carrying no account, so any attempt to attribute a bracket
to an account had to match on symbol alone — which silently lets a Schwab bracket be counted as
covering a Webull fill.

Measured cost, 2026-08-17: the fill-denominator table had to print `NATIVE_OCO?` with a question
mark on every RTH row, and the headline finding ("the attach never succeeds") could not be stated
per account at all. One missing field blocked the largest item on the board.

⛔ Do not "simplify" the account back out of these log lines.
"""
from __future__ import annotations

import inspect
import re

from project_mai_tai.oms import service as svc


def _emit_source() -> str:
    return inspect.getsource(svc.OmsRiskService._apply_v2_oco_bracket_entry)


def test_every_V2_OCO_EMIT_line_carries_the_account() -> None:
    """All three emit sites — SKIPPED, no-entry-reference, and the real bracket."""
    src = _emit_source()
    sites = [m for m in re.finditer(r'"\[V2-OCO-EMIT\][^"]*"', src)]
    assert len(sites) >= 3, f"expected 3 emit sites, found {len(sites)}"
    for site in sites:
        # the format string is followed by its args up to the closing paren of the log call
        tail = src[site.end(): site.end() + 400]
        assert "payload.broker_account_name" in tail, (
            f"[V2-OCO-EMIT] site is missing the account: {site.group(0)[:70]}"
        )


def test_the_account_is_not_dropped_from_the_bracket_site_specifically() -> None:
    """The RTH bracket line is the one every RTH cell of the denominator rests on."""
    src = _emit_source()
    assert re.search(
        r'"\[V2-OCO-EMIT\] %s %s bracket entry=.*?payload\.symbol, payload\.broker_account_name',
        src, re.S,
    ), "the real-bracket emit must log symbol AND account, in that order"


def test_the_RTH_only_gate_is_still_there() -> None:
    """⛔ The gate that produces the whole PRE->SKIPPED / RTH->bracket correspondence. Removing it
    would silently start emitting pre-market brackets, which the design says would queue to 09:30
    or firm-reject."""
    src = _emit_source()
    assert "_is_regular_market_session()" in src
    assert "SKIPPED (outside regular hours)" in src
