"""Behavior-neutral proof for the Track-2 Phase-1 `exit_logic` extraction.

THE PROOF GATE. `tests/unit/golden/exit_logic_golden.json` was captured from CLEAN
`origin/main` (BEFORE the relocation) by running `run_scenarios()`. After the pure
move (ExitEngine + Position math + TradingConfig → `project_mai_tai.exit_logic`, with
`strategy_core` re-export shims), this test re-runs the SAME scenarios through the
re-exported symbols and asserts **byte-identical** output — the by-name regression vs
main. Plus the structural-identity check (`strategy_core.X is exit_logic.X`) and the
import-graph leaf guard. If any of these fail, the extraction is not behavior-neutral
and Phase 2 does not start.

Imports go through the `strategy_core` paths deliberately — those paths must resolve
to the same code before AND after the move (that is what the re-export shims guarantee),
so the identical scenario function exercises both states.
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest

from project_mai_tai.strategy_core.exit import ExitEngine
from project_mai_tai.strategy_core.position_tracker import Position
from project_mai_tai.strategy_core.trading_config import TradingConfig

GOLDEN = pathlib.Path(__file__).parent / "golden" / "exit_logic_golden.json"
ENTRY_TIME = "2026-01-01 10:00:00"  # fixed → no wall-clock in Position.to_dict()
ENTRY = 10.0


def _pos(*, qty: int = 100, profile: str = "NORMAL", floor_params: dict | None = None) -> Position:
    fp = floor_params or {}
    return Position(
        ticker="TEST", entry_price=ENTRY, quantity=qty,
        entry_time=ENTRY_TIME, path="P1", scale_profile=profile, **fp,
    )


def _price_for_pct(pct: float) -> float:
    return ENTRY * (1 + pct / 100)


def run_scenarios() -> dict:
    """Deterministic battery exercising every exit-decision surface. The returned
    dict is the golden vector — captured on main, asserted after the move."""
    out: dict = {}
    base = TradingConfig()
    engine = ExitEngine(base)

    # --- 1. Position.update_price: peak / tier / floor ratchet across a price path ---
    # Rise through every floor band then pull back (floor must ratchet, never drop).
    path = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0, 4.5, 3.0, 2.0, 1.0, -0.5]
    p = _pos()
    series = []
    for pct in path:
        p.update_price(_price_for_pct(pct))
        series.append({"in_pct": pct, **p.to_dict()})
    out["update_price_series_NORMAL"] = series

    # reclaim-style floor params (0.25 / 0.75) — the make_30s_reclaim_variant path
    p2 = _pos(floor_params={"floor_lock_at_1pct_peak_pct": 0.25, "floor_lock_at_2pct_peak_pct": 0.75})
    series2 = []
    for pct in [1.0, 2.0, 3.0, 4.0, 2.0]:
        p2.update_price(_price_for_pct(pct))
        series2.append({"in_pct": pct, **p2.to_dict()})
    out["update_price_series_reclaim_floor"] = series2

    # --- 2. get_scale_action: every tier, NORMAL + DEGRADED, with/without scales_done ---
    scale_cases = []
    for profile in ("NORMAL", "DEGRADED"):
        for profit_pct in [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0]:
            for done in ([], ["PCT2"], ["FAST4"], ["PCT1"], ["PCT2", "PCT4_AFTER2"]):
                pp = _pos(profile=profile)
                pp.scales_done = list(done)
                pp.update_price(_price_for_pct(profit_pct))
                scale_cases.append({
                    "profile": profile, "profit_pct": profit_pct, "scales_done": done,
                    "action": pp.get_scale_action(base),
                })
    out["get_scale_action_cases"] = scale_cases

    # --- 3. is_floor_breached ---
    breach = []
    for peak_pct, now_pct in [(2.0, 2.0), (2.0, 0.4), (2.0, 0.6), (4.0, 2.6), (4.0, 2.4), (0.5, 0.0)]:
        pp = _pos()
        pp.update_price(_price_for_pct(peak_pct))   # set peak + floor
        pp.update_price(_price_for_pct(now_pct))     # then current
        breach.append({"peak_pct": peak_pct, "now_pct": now_pct,
                       "floor_pct": pp.floor_pct, "floor_price": pp.floor_price,
                       "breached": pp.is_floor_breached()})
    out["is_floor_breached_cases"] = breach

    # --- 4. apply_scale ---
    pp = _pos(qty=100)
    pp.update_price(_price_for_pct(2.0))
    pp.apply_scale("PCT2", 50, exit_price=_price_for_pct(2.0))
    out["apply_scale_after_PCT2"] = {**pp.to_dict(), "scale_pnl": pp.scale_pnl}

    # --- 5. ExitEngine.check_hard_stop (stop_loss_pct=1.5 → stop=9.85) ---
    hard = []
    for px in [9.86, 9.85, 9.84, 10.0]:
        pp = _pos()
        pp.update_price(px)
        hard.append({"price": px, "signal": engine.check_hard_stop(pp, px)})
    out["check_hard_stop_cases"] = hard

    # --- 6. ExitEngine.check_intrabar_exit (floor-breach vs scale vs none) ---
    intrabar = []
    # floor breach
    pb = _pos()
    pb.update_price(_price_for_pct(2.0))
    pb.update_price(_price_for_pct(0.4))
    intrabar.append({"case": "floor_breach", "signal": engine.check_intrabar_exit(pb)})
    # scale due
    ps = _pos()
    ps.update_price(_price_for_pct(2.0))
    intrabar.append({"case": "scale_PCT2", "signal": engine.check_intrabar_exit(ps)})
    # nothing
    pn = _pos()
    pn.update_price(_price_for_pct(0.5))
    intrabar.append({"case": "none", "signal": engine.check_intrabar_exit(pn)})
    out["check_intrabar_exit_cases"] = intrabar

    # --- 7. ExitEngine.check_exit: tier MACD/stoch exits ---
    # base config has exit_stoch_health_filter_enabled=False; also test the 30s variant (True)
    engine_health = ExitEngine(TradingConfig().make_30s_variant())
    tier_cases = []
    indicator_sets = [
        {"stoch_k_below_exit": True, "stoch_k_falling": True},
        {"macd_cross_below": True},
        {"stoch_k_below_exit": True, "stoch_k_falling": True, "price_above_ema9": True},
        {"stoch_k_below_exit": True, "stoch_k_falling": True, "price_above_ema9": False},
        {"stoch_k_below_exit": False, "stoch_k_falling": True},
        {"stoch_k_below_exit": True, "stoch_k_falling": True, "stoch_k": 50, "stoch_k_prev": 40,
         "stoch_k_prev2": 30, "stoch_d": 35},  # "healthy" momentum (health filter path)
        {},
    ]
    for tier in (1, 2, 3):
        for eng_name, eng in (("base", engine), ("30s_health", engine_health)):
            for ind in indicator_sets:
                pp = _pos()
                # drive peak to set the tier deterministically
                peak = {1: 0.5, 2: 1.5, 3: 3.5}[tier]
                pp.update_price(_price_for_pct(peak))
                pp.update_price(_price_for_pct(0.7))  # back to a non-breaching, non-scaling price
                # force exact tier (update_price only upgrades; ensure we test the intended tier)
                pp.tier = tier
                tier_cases.append({
                    "tier": tier, "engine": eng_name, "indicators": ind,
                    "signal": eng.check_exit(pp, ind),
                })
    out["check_exit_cases"] = tier_cases

    return out


# --------------------------------------------------------------------------- (A)

def test_golden_parity() -> None:
    """By-name regression vs main: the relocated code reproduces the golden
    vectors captured on clean main, byte-for-byte."""
    assert GOLDEN.exists(), "golden vectors missing — generate on clean main first"
    expected = json.loads(GOLDEN.read_text())
    actual = json.loads(json.dumps(run_scenarios()))  # normalize via JSON round-trip
    assert actual == expected


# --------------------------------------------------------------------------- (B)

def test_structural_identity_after_move() -> None:
    """The strategy_core re-exports resolve to the relocated exit_logic objects —
    so every existing import site gets the same code (skips pre-move)."""
    try:
        from project_mai_tai.exit_logic.config import TradingConfig as ELConfig
        from project_mai_tai.exit_logic.engine import ExitEngine as ELEngine
        from project_mai_tai.exit_logic.position import Position as ELPosition
    except ModuleNotFoundError:
        pytest.skip("exit_logic not present yet (pre-move golden-capture phase)")
    from project_mai_tai.strategy_core.exit import ExitEngine as SCEngine
    from project_mai_tai.strategy_core.position_tracker import Position as SCPosition
    from project_mai_tai.strategy_core.trading_config import TradingConfig as SCConfig
    assert SCEngine is ELEngine
    assert SCPosition is ELPosition
    assert SCConfig is ELConfig


# --------------------------------------------------------------------------- (C)

def test_import_graph_leaf_guard() -> None:
    """exit_logic must stay a LEAF — no imports of the engine/streamer/gateway/DB —
    so the OMS (Phase 2) can import it without dragging v2-isolation-breaking deps."""
    pkg = pathlib.Path(__file__).resolve().parents[2] / "src" / "project_mai_tai" / "exit_logic"
    if not pkg.exists():
        pytest.skip("exit_logic not present yet (pre-move golden-capture phase)")
    # `strategy_core` is forbidden too: exit_logic must be a PURE LEAF. The back-edge
    # exit_logic.position → strategy_core.time_utils caused a circular import
    # (strategy_core/__init__ → position_tracker → exit_logic.position); now stdlib-only.
    forbidden = ("strategy_engine_app", "schwab_streamer", "schwab_native_30s",
                 "bar_builder", "polygon_30s", "market_data", "services.", ".db.", "db.models",
                 "gateway", "strategy_core")
    offenders = []
    for py in pkg.glob("*.py"):
        tree = ast.parse(py.read_text())
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
            elif isinstance(node, ast.Import):
                mod = ",".join(a.name for a in node.names)
            if mod and any(f in mod for f in forbidden):
                offenders.append(f"{py.name}: {mod}")
    assert not offenders, f"exit_logic leaked non-leaf imports: {offenders}"
