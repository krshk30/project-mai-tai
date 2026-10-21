"""Per-call momentum cancel evidence; unavailable is never terminal proof."""

from dataclasses import dataclass
from typing import Literal

from project_mai_tai.broker_adapters.protocols import ExecutionReport


@dataclass(frozen=True)
class MomentumOrderRead:
    kind: Literal["report", "unavailable", "status_unknown", "invalid"]
    report: ExecutionReport | None = None
    reason: str = ""


class MomentumReadUnavailable(Exception):
    """A bounded, classified read attempt failed before usable broker evidence."""
