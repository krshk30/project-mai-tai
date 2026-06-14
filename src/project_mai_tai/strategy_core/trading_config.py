"""Re-export shim — `TradingConfig` (and its `make_*_variant` methods) was relocated
to `project_mai_tai.exit_logic.config` (Track-2 Phase-1, behavior-neutral). Imports
from here resolve to the same class, so existing call sites and behavior are unchanged.
"""
from __future__ import annotations

from project_mai_tai.exit_logic.config import TradingConfig

__all__ = ["TradingConfig"]
