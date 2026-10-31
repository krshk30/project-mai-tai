"""Observe one synchronous entry evaluation without evaluating a gate twice."""
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class _Trace:
    branch: str = "unknown"
    first_false_predicate: str = "none"


@dataclass(frozen=True)
class LineEntryReceipt:
    allowed: bool
    branch: str
    first_false_predicate: str


_trace: ContextVar[_Trace | None] = ContextVar("line_entry_receipt", default=None)


def branch(name: str) -> None:
    trace = _trace.get()
    if trace is not None:
        trace.branch = name


def predicate(value, reason: str):
    """Return the already computed boolean operand, retaining short circuiting."""
    trace = _trace.get()
    if trace is not None and not value and trace.first_false_predicate == "none":
        trace.first_false_predicate = reason
    return value


def capture(evaluate, symbol: str) -> LineEntryReceipt:
    trace = _Trace()
    token = _trace.set(trace)
    try:
        allowed = evaluate(symbol)
        return LineEntryReceipt(
            bool(allowed), trace.branch,
            "none" if allowed else (
                trace.first_false_predicate if trace.first_false_predicate != "none"
                else "unattributed"),
        )
    finally:
        _trace.reset(token)
