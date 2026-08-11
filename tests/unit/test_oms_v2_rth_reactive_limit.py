"""RTH REACTIVE entry — a band-capped marketable LIMIT instead of an uncapped MARKET.

⭐⭐ THE MEASUREMENT THAT MOTIVATES IT (21 days, `live:schwab_1m_v2`, same universe, same window):

    reactive MARKET      n=66   SD 58.6 bps   worst adverse +351.7 bps
    reactive LIMIT       n=5    SD 28.0 bps
    resting STOP_LIMIT   n=63   SD 25.9 bps   worst adverse  +60.2 bps

⛔ The n=5 is NOT the evidence and must not be quoted as a result — it is consistent-with. **The
evidence is the mechanism: a price cap caps the price.** The ≥200 bps entries are UNBOUNDED-PRICE
events, not late-arrival events; chasing costs the spread and the drift (tens of bps), having no
ceiling is what produces 352.

⛔ THIS DOES NOT FIX THE CHASING. The trigger and timing are unchanged — the bot still waits for the
print. Resting at the known level is a separate change, blocked by `_resting_entry_already_open`
(one resting order per symbol; a second slot is the #580/EGG-POLA orphan surface).

⚠️ THE NEW FAILURE MODE: **a market order always fills; this one will sometimes not.** Both the
placement and the abandon are logged so the frequency reads off the tape rather than needing another
reconstruction — the fifth missing-negative of the week, avoided in advance rather than found later.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

import project_mai_tai.oms.service as oms_service

from test_oms_v2_eh_resting_entry import _oms, _v2_open  # noqa: E402


MARKER = "[OMS-V2-RTH-REACTIVE-LIMIT]"


class _Cap:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg, *a) -> None:
        self.lines.append(msg % a if a else msg)

    warning = exception = debug = error = info


def _reactive_meta(**extra) -> dict:
    """Exactly what the strategy emits for an RTH reactive entry: no order_type key at all, so the
    OMS defaults it to MARKET. That default is what this change replaces."""
    md = {
        "path": "ATR Flip",
        "atr_variant": "CW-v2",
        "entry_price": "10.0000",
        "reference_price": "10.0000",
        "cw_trigger": "10.0000",
        "cw_flip_level": "9.0000",
        "source": "schwab_1m_v2",
    }
    md.update(extra)
    return md


def _fresh_quote(svc, symbol="FOO", ask=10.02):
    svc._latest_quotes_by_symbol[symbol] = {
        "ask": ask, "bid": ask - 0.02, "received_at": datetime.now(UTC),
    }


def _rth(monkeypatch, is_rth=True):
    monkeypatch.setattr(oms_service, "_is_regular_market_session", lambda *a, **k: is_rth)


# ── the change ───────────────────────────────────────────────────────────────────────────────

def test_an_rth_reactive_open_becomes_a_band_capped_limit(monkeypatch) -> None:
    """⭐ THE POINT. No order_type on the intent means the OMS would default to MARKET — uncapped.
    After this it carries a limit at min(ask, level*(1+band))."""
    _rth(monkeypatch)
    svc = _oms()
    svc.logger = _Cap()
    _fresh_quote(svc, ask=10.02)
    ev = _v2_open(_reactive_meta())

    assert svc._apply_v2_rth_reactive_limit(event=ev, intent=None) is None   # PROCEED
    md = ev.payload.metadata
    assert md["order_type"] == "limit"
    assert md["limit_price"] == "10.02"          # ask <= cap(10.05) -> the ask
    assert md["oms_v2_rth_reactive_limit"] == "true"
    assert any(MARKER in ln and "PLACED" in ln for ln in svc.logger.lines)


def test_the_limit_is_capped_by_the_band_not_by_the_ask(monkeypatch) -> None:
    """⛔ THE CEILING IS THE WHOLE CHANGE. If the ask sits inside the band we take the ask; the cap
    is what stops a 352 bps fill, so it must bind when the ask runs away — see the abandon test."""
    _rth(monkeypatch)
    svc = _oms()
    svc.logger = _Cap()
    _fresh_quote(svc, ask=10.04)
    ev = _v2_open(_reactive_meta())
    svc._apply_v2_rth_reactive_limit(event=ev, intent=None)
    assert ev.payload.metadata["limit_price"] == "10.04"
    cap = float(ev.payload.metadata["oms_v2_rth_reactive_limit_cap"])
    assert cap == pytest.approx(10.05)
    assert float(ev.payload.metadata["limit_price"]) <= cap


def test_reference_price_is_NOT_overwritten(monkeypatch) -> None:
    """⛔⭐⭐ THE ONE DELIBERATE RTH/EH DIVERGENCE, and it is load-bearing.

    The EH pricer sets `reference_price` to its own limit. If this path did the same, the recorded
    decision price would become the fill price and EVERY slippage study measuring fill-vs-decision
    would silently start measuring fill-vs-fill and report ~0 — destroying the exact measurement
    that justified this change."""
    _rth(monkeypatch)
    svc = _oms()
    svc.logger = _Cap()
    _fresh_quote(svc, ask=10.02)
    ev = _v2_open(_reactive_meta())
    svc._apply_v2_rth_reactive_limit(event=ev, intent=None)
    assert ev.payload.metadata["reference_price"] == "10.0000", (
        "the strategy's trigger price must remain the recorded reference"
    )


# ── the new failure mode: a limit that does not fill ─────────────────────────────────────────

def test_ask_past_the_band_abandons_and_says_so(monkeypatch) -> None:
    """⚠️ A MARKET ALWAYS FILLS; THIS WILL SOMETIMES NOT. When the ask has gapped past the band we
    prefer no fill to a chase — and the abandon must be on the tape, or tomorrow's non-fill rate
    needs another reconstruction."""
    _rth(monkeypatch)
    svc = _oms()
    svc.logger = _Cap()
    _fresh_quote(svc, ask=11.50)                    # way past cap 10.05
    ev = _v2_open(_reactive_meta())
    monkeypatch.setattr(svc, "_abandon_v2_eh_entry", lambda **kw: ("ABANDONED", kw))

    out = svc._apply_v2_rth_reactive_limit(event=ev, intent=None)

    assert out is not None and out[0] == "ABANDONED"
    assert out[1]["reason_code"] == "ASK_PAST_BAND"
    assert any(MARKER in ln and "ABANDONED" in ln and "ASK_PAST_BAND" in ln for ln in svc.logger.lines)


def test_no_fresh_quote_abandons_rather_than_ordering_blind(monkeypatch) -> None:
    _rth(monkeypatch)
    svc = _oms()
    svc.logger = _Cap()                              # no quote seeded at all
    ev = _v2_open(_reactive_meta())
    monkeypatch.setattr(svc, "_abandon_v2_eh_entry", lambda **kw: ("ABANDONED", kw))

    out = svc._apply_v2_rth_reactive_limit(event=ev, intent=None)

    assert out is not None and out[1]["reason_code"] == "NO_FRESH_QUOTE"
    assert any(MARKER in ln and "NO_FRESH_QUOTE" in ln for ln in svc.logger.lines)


# ── everything else must be byte-identical ───────────────────────────────────────────────────

@pytest.mark.parametrize("meta,why", [
    ({"resting_entry": "true"}, "the RTH resting path owns its own broker STOP_LIMIT"),
    ({"eh_resting": "true"}, "the EH pricer owns this one"),
    ({"fanout_leg": "webull"}, "the Webull fan-out leg is deliberately untouched here"),
    ({"atr_variant": "CW-v2-resting"}, "not the reactive variant"),
])
def test_other_v2_paths_are_untouched(monkeypatch, meta, why) -> None:
    _rth(monkeypatch)
    svc = _oms()
    svc.logger = _Cap()
    _fresh_quote(svc, ask=10.02)
    ev = _v2_open(_reactive_meta(**meta))
    before = dict(ev.payload.metadata)

    assert svc._apply_v2_rth_reactive_limit(event=ev, intent=None) is None
    assert ev.payload.metadata == before, why
    assert svc.logger.lines == [], why


def test_extended_hours_is_untouched(monkeypatch) -> None:
    """⛔ RTH-ONLY. In EH this must be inert — the EH pricer runs instead, and running both would
    double-price the same order."""
    _rth(monkeypatch, is_rth=False)
    svc = _oms()
    svc.logger = _Cap()
    _fresh_quote(svc, ask=10.02)
    ev = _v2_open(_reactive_meta())
    before = dict(ev.payload.metadata)

    assert svc._apply_v2_rth_reactive_limit(event=ev, intent=None) is None
    assert ev.payload.metadata == before
    assert svc.logger.lines == []


def test_a_missing_anchor_leaves_todays_market_behaviour_alone(monkeypatch) -> None:
    """⛔ FAIL-SAFE DIRECTION. Without `entry_price` there is nothing to band-cap. Do NOT abandon the
    entry — fall through to today's MARKET. A missing field must never cost a trade."""
    _rth(monkeypatch)
    svc = _oms()
    svc.logger = _Cap()
    _fresh_quote(svc, ask=10.02)
    md = _reactive_meta()
    del md["entry_price"]
    ev = _v2_open(md)

    assert svc._apply_v2_rth_reactive_limit(event=ev, intent=None) is None
    assert "order_type" not in ev.payload.metadata, "must remain a MARKET order"


def test_the_shared_pricer_is_one_implementation() -> None:
    """⛔ REUSE, NOT A SECOND COPY. Both the EH and RTH paths must call the same helper — a duplicate
    is free to drift, and a silent RTH/EH pricing divergence is exactly the class this codebase
    keeps paying for."""
    import inspect

    from project_mai_tai.oms.service import OmsRiskService

    eh = inspect.getsource(OmsRiskService._apply_v2_eh_resting_entry)
    rth = inspect.getsource(OmsRiskService._apply_v2_rth_reactive_limit)
    assert "_band_capped_marketable_limit" in eh
    assert "_band_capped_marketable_limit" in rth
