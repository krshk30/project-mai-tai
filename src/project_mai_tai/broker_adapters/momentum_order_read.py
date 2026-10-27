"""Per-call momentum cancel evidence; unavailable is never terminal proof."""

from dataclasses import dataclass
from typing import Literal

from project_mai_tai.broker_adapters.protocols import ExecutionReport


@dataclass(frozen=True)
class MomentumOrderRead:
    kind: Literal["report", "unavailable", "status_unknown", "invalid", "absent"]
    report: ExecutionReport | None = None
    reason: str = ""


class MomentumReadUnavailable(Exception):
    """A bounded, classified read attempt failed before usable broker evidence."""


@dataclass(frozen=True)
class MomentumEntryLookup:
    """One provider read, bound to the original own entry, never a None inference."""

    kind: Literal["absent", "present", "unknown"]
    account: str
    client_order_id: str
    symbol: str
    provider: str
    sync_pass: int
    observed_ns: int
    reason: str
    report: ExecutionReport | None = None
