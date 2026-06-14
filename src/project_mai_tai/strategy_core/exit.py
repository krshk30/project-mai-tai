"""Re-export shim — `ExitEngine` was relocated to `project_mai_tai.exit_logic`
(Track-2 Phase-1, behavior-neutral). Imports from here resolve to the same object,
so existing call sites and behavior are unchanged.
"""
from __future__ import annotations

from project_mai_tai.exit_logic.engine import ExitEngine

__all__ = ["ExitEngine"]
