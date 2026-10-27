"""Pure ATR simulation contracts; no application, broker, or storage imports.

Results describe hypothetical executions, never permission to trade. Storage,
workers, and replay input adapters live outside this package's engine contract.
"""

from .types import EntryMode, FillModel, LiveReason, PositionReceipt, SimExitReason

__all__ = ["EntryMode", "FillModel", "LiveReason", "PositionReceipt", "SimExitReason"]
