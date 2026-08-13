"""Two day-one defects in the RTH fan-out cap (#684), both found by watching it run 2026-08-13.

⛔⭐⭐ DEFECT 1 — THE CAP WAS DECORATIVE ON BRACKETED ORDERS.
`webull.py::_build_combo_payload` types the combo MASTER off `bracket_entry_type`, NOT off
`order_type`. The strategy stamps that `MARKET`, so a BRACKETED fan-out leg went out as a MARKET
master and the limit was silently ignored — on exactly the orders the cap was written for (181 of
215 RTH fan-out entries over 14 days carry a bracket).
PROVEN LIVE: XHG 2026-08-13 11:43:29 — we sent `limit_price 3.87` and it **filled at 3.8873**,
45 bps ABOVE our own ceiling, with `bracket_entry_type: MARKET` in the same payload.

⛔⭐⭐ DEFECT 2 — THE BAND MEASURED FROM WHERE WE NOTICED, NOT FROM WHERE WE DECIDED.
`_fanout_rth_resting_cross` sets `entry_price` to the price at which SOFTWARE detected the cross.
Live FGI 08-13: resting level **8.3015**, cross detected at **8.6461** — so the band permitted the
whole 4.15% run-up and capped only half a percent beyond THAT. The abandon that day cleared by
0.0007 and was luck, not design.

⛔ AND THE OBVIOUS FIX WOULD HAVE BEEN WORSE. Re-pointing `entry_price` at the resting level also
re-anchors the OCO bracket (`_apply_v2_oco_bracket_entry`): FGI's target would have been 8.4675
while the leg filled near 8.69 — a target BELOW the fill, an instant loss exit. So the anchor is a
SEPARATE field and `entry_price` keeps meaning "where we expect to fill".
"""
from __future__ import annotations

import inspect

from project_mai_tai.oms import service as svc
from project_mai_tai.strategy_core import schwab_1m_v2 as strat


# --------------------------------------------------------------- DEFECT 1: the bracket master type
def test_the_cap_sets_bracket_entry_type_or_it_is_decorative() -> None:
    """THE REGRESSION. Without this the combo master stays MARKET and the limit is ignored."""
    src = inspect.getsource(svc.OmsRiskService._apply_v2_rth_fanout_limit)
    assert 'md["bracket_entry_type"] = "LIMIT"' in src


def test_the_combo_builder_really_does_key_off_bracket_entry_type() -> None:
    """Pins WHY defect 1 existed. If the builder ever reads `order_type` instead, the fix above
    becomes the wrong one — and this test says so rather than leaving it silently redundant."""
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    src = inspect.getsource(WebullBrokerAdapter._build_combo_payload)
    assert 'request.metadata.get("bracket_entry_type"' in src


def test_a_LIMIT_master_is_accepted_by_the_guard() -> None:
    """⭐ The shape we now ask for must survive the client-side guard — and Probe W proved the
    broker accepts it too (shape A, HTTP 200, placed live 2026-08-12)."""
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    src = inspect.getsource(WebullBrokerAdapter._build_combo_payload)
    assert 'entry_type not in {"LIMIT", "MARKET"}' in src


# ------------------------------------------------------------------- DEFECT 2: the band anchor
def test_rth_resting_passes_the_RESTING_LEVEL_as_the_band_anchor() -> None:
    """THE REGRESSION: the band must measure from the level, not from where we noticed."""
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    assert "band_anchor=state.resting_level" in src


def test_entry_px_is_NOT_repointed_at_the_level() -> None:
    """⛔ THE FIX THAT WOULD HAVE BEEN WORSE. `entry_price` also anchors the OCO bracket; pointing it
    at the resting level puts the target BELOW the fill on any late cross (FGI: target 8.4675 vs a
    ~8.69 fill). It must stay the cross price."""
    src = inspect.getsource(strat.SchwabV2Strategy._fanout_rth_resting_cross)
    assert "entry_px=px" in src
    assert "entry_px=state.resting_level" not in src


def test_the_anchor_is_a_separate_metadata_key() -> None:
    src = inspect.getsource(strat.SchwabV2Strategy._build_webull_fanout_draft)
    assert 'md["resting_band_anchor"]' in src


def test_only_rth_resting_supplies_an_anchor() -> None:
    """`reactive`'s entry_px IS its decision price and `eh_resting` already passes the level, so
    both must leave this unset and fall back to `entry_price`."""
    reactive = inspect.getsource(strat.SchwabV2Strategy._cw_v2_quote)
    assert "band_anchor" not in reactive
    eh = inspect.getsource(strat.SchwabV2Strategy._eh_resting_cross_check)
    assert "band_anchor" not in eh


def test_the_oms_prefers_the_anchor_and_falls_back_to_entry_price() -> None:
    src = inspect.getsource(svc.OmsRiskService._apply_v2_rth_fanout_limit)
    assert 'md["resting_band_anchor"]' in src
    assert "anchor = level" in src, "must fall back to entry_price when no anchor is supplied"
    assert "level=anchor" in src, "the PRICER must receive the anchor, not the entry price"


def test_the_abandon_log_states_BOTH_numbers() -> None:
    """⚠️ Anchor and entry_price now differ. A log that prints one of them makes the next
    investigation guess which — the exact ambiguity that cost a day here."""
    src = inspect.getsource(svc.OmsRiskService._apply_v2_rth_fanout_limit)
    assert "anchor=%.4f, entry_price=%.4f" in src
