"""Neutral, leaf-level exit-ladder logic shared by `strategy_core` (the momentum
bots) and, from Track-2 Phase-2, the OMS for v2 positions.

Contents are a PURE relocation of the validated ladder — `TradingConfig`,
`Position` (peak/tier/floor/scale math), and `ExitEngine` — with `strategy_core`
keeping thin re-export shims so existing import sites and behavior are unchanged.
This package imports only stdlib + `strategy_core.time_utils` (itself a pure leaf
the OMS already imports); it must never import the strategy engine, streamer,
gateway, or DB (enforced by `tests/unit/test_exit_logic_parity.py`).
"""
from __future__ import annotations

from project_mai_tai.exit_logic.config import TradingConfig
from project_mai_tai.exit_logic.engine import ExitEngine
from project_mai_tai.exit_logic.position import Position

__all__ = ["TradingConfig", "ExitEngine", "Position"]
