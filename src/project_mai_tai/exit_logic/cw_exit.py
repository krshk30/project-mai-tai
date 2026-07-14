"""Shared CW-v2 exit decision — the SINGLE source of truth for the OMS (tick-by-tick,
``_evaluate_v2_managed_exit``) and the backtest harness (``atr_cw_v2_variants.run_exit``), so
live and backtest are the same build (operator parity requirement, 2026-07-14).

Two modes, selected by ``floor_enabled``:
  * OFF (current live): full close at **+target%** (hard) OR **-stop%** OR a bar-close flip.
  * ON  (floor exit):   at **+target%** ARM a floor at **+floor_pct%** and RIDE; exit when the bid
    falls back to that floor, else -stop% (only reachable before arming) or a bar-close flip.
    Backtest 07-09..07-14: floor@+2% + 1-bar reclaim gap + keep -5% was the best config.

``armed`` is a plain bool (the floor is fixed at entry*(1+floor_pct/100), so it re-arms
identically after an OMS restart — no durable state needed). Pure + deterministic.
"""
from __future__ import annotations


def cw_exit_decision(
    entry: float,
    bid: float,
    armed: bool,
    *,
    target_pct: float,
    stop_pct: float,
    floor_pct: float,
    floor_enabled: bool,
    flip_pending: bool,
) -> tuple[str, bool]:
    """Return ``(action, armed_out)``.

    action ∈ {"hold", "target", "floor", "stop", "flip", "arm"}:
      target/floor/stop/flip -> exit now; arm -> set the floor and keep holding; hold -> do nothing.
    Precedence mirrors the current OMS block: target/arm > hard-stop > flip.
    """
    tp = entry * (1.0 + target_pct / 100.0)
    hs = entry * (1.0 - stop_pct / 100.0)

    if not floor_enabled:
        if bid >= tp:
            return "target", armed
        if bid <= hs:
            return "stop", armed
        if flip_pending:
            return "flip", armed
        return "hold", armed

    # floor mode
    floor = entry * (1.0 + floor_pct / 100.0)
    if not armed:
        if bid >= tp:
            return "arm", True          # reached the target -> lock the floor, keep riding
        if bid <= hs:
            return "stop", False
        if flip_pending:
            return "flip", False
        return "hold", False
    # armed: ride until the bid falls back to the floor (or a bar-close flip)
    if bid <= floor:
        return "floor", False
    if flip_pending:
        return "flip", False
    return "hold", True
