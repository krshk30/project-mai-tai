"""⭐ GATE1 watch — does a qualifying #647 Gate 1 shape exist right now?

⛔⭐⭐ THE QUALIFYING SHAPE IS THE DEFECT ITSELF. Gate 1 needs a v2-held SCHWAB long in RTH whose
shares are NOT reserved by an open exit. An ordinary RTH entry can never qualify — it is bracketed
on entry, so its shares are reserved. That is exactly why SUNE failed. The only unreserved Schwab
long in RTH is one entered PRE-MARKET and still held at 09:30 — the hole Part 1 exists to fix.

The tapes below are the REAL positions from 2026-09-09, so a regression is measured against days
that actually happened rather than invented shapes.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def _locate() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "ops" / "health" / "gate1_shape_watch.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("gate1_shape_watch.py not found")


_SPEC = importlib.util.spec_from_file_location("gate1_shape_watch", _locate())
gw = importlib.util.module_from_spec(_SPEC)
sys.modules["gate1_shape_watch"] = gw
_SPEC.loader.exec_module(gw)

ET = ZoneInfo("America/New_York")


def _row(symbol, account, qty, entry_hhmm, reserved):
    h, m = (int(x) for x in entry_hhmm.split(":"))
    return {
        "symbol": symbol, "account": account, "quantity": qty,
        "entry_at": datetime(2026, 9, 9, h, m, tzinfo=ET),
        "reserved": reserved, "unknown_entry": False,
    }


NOW_RTH = datetime(2026, 9, 9, 10, 30, tzinfo=ET)
NOW_PREMARKET = datetime(2026, 9, 9, 9, 10, tzinfo=ET)


def test_the_qualifying_shape_is_recognised():
    """⭐ THE POINT OF THE WATCH: Schwab long, entered pre-market, held into RTH, nothing reserved."""
    q, rej = gw.qualifying_rows([_row("ABCD", "live:schwab_1m_v2", 2, "09:20", 0)], NOW_RTH)
    assert [r["symbol"] for r in q] == ["ABCD"]
    assert rej == []


def test_the_REAL_SUNE_shape_does_NOT_qualify():
    """⛔ KNOWN-BAD TAPE. SUNE entered 09:38 ET — an RTH entry, bracketed on entry, shares reserved.
    codex-2 correctly refused it as a Gate 1 candidate; this pins that refusal."""
    q, rej = gw.qualifying_rows([_row("SUNE", "live:schwab_1m_v2", 2, "09:38", 2)], NOW_RTH)
    assert q == []
    assert "not a pre-market entry" in rej[0]["why"]


def test_the_REAL_YMAT_shape_does_NOT_qualify_it_is_the_WRONG_BROKER():
    """⛔ KNOWN-BAD TAPE. YMAT was entered 09:20 pre-market and held into RTH — the right SHAPE —
    but on WEBULL. Gate 1 needs Schwab; a Webull long proves nothing about the Schwab preview."""
    q, rej = gw.qualifying_rows([_row("YMAT", "live:orb", 1, "09:20", 0)], NOW_RTH)
    assert q == []
    assert "wrong account" in rej[0]["why"]


def test_reserved_shares_disqualify_even_with_the_right_shape():
    """⛔ THE DISCRIMINATOR. Same pre-market Schwab long, but an open exit reserves the shares —
    the preview cannot be taken against it. `reserved` uses the OMS's own OPEN_ORDER_STATUSES."""
    q, rej = gw.qualifying_rows([_row("ABCD", "live:schwab_1m_v2", 2, "09:20", 2)], NOW_RTH)
    assert q == []
    assert "reserved by an open exit" in rej[0]["why"]


def test_before_0930_nothing_qualifies_yet():
    """PINS THE CLOCK. The shape needs REGULAR HOURS; pre-market it is not yet a Gate 1 candidate."""
    q, rej = gw.qualifying_rows([_row("ABCD", "live:schwab_1m_v2", 2, "09:20", 0)], NOW_PREMARKET)
    assert q == []
    assert "have not started" in rej[0]["why"]


def test_every_row_is_classified_and_none_is_dropped():
    """⛔ A DENOMINATOR NOBODY NAMED IS ONE NOBODY CHECKED. Every input row must come back in
    exactly one bucket, or the watch can go quiet by losing rows rather than by finding none."""
    rows = [
        _row("ABCD", "live:schwab_1m_v2", 2, "09:20", 0),   # qualifies
        _row("SUNE", "live:schwab_1m_v2", 2, "09:38", 2),   # RTH entry
        _row("YMAT", "live:orb", 1, "09:20", 0),            # wrong broker
        _row("EFGH", "live:schwab_1m_v2", 2, "09:20", 2),   # reserved
    ]
    q, rej = gw.qualifying_rows(rows, NOW_RTH)
    assert len(q) + len(rej) == len(rows)


def test_the_reserved_status_set_matches_the_OMS_definition():
    """⛔ AUTHORITATIVE FOR A IS NOT FOR B — unless it is the SAME set. If this watch's idea of
    'reserved' drifts from oms/store.py's, it will qualify a position the real gate refuses."""
    from project_mai_tai.oms.store import OmsStore

    assert tuple(gw.OPEN_ORDER_STATUSES) == tuple(OmsStore.OPEN_ORDER_STATUSES)
