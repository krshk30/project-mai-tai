"""C1 18:28:57 working-set proposal. No I/O, model execution or permissions.

The planner intentionally cannot seal an episode at its SELL timestamp: the
existing kernel exits at the first eligible print at/after the boundary. A caller
must resolve that tail with the unchanged classifier or retain it through asof.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from atrsimlive1_line_route import recompute


ET = ZoneInfo("America/New_York")
NS = 1000000000
MINUTE = 60 * NS


def nanos(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("aware_window_clock_required")
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * NS + delta.microseconds * 1000


@dataclass(frozen=True)
class WorkingWindow:
    symbol: str
    start_ns: int
    boundary_ns: int | None
    asof_end_ns: int
    origin: str
    terminal_pending: bool
    end_key: tuple[int, int] | None = None
    status: str = "UNRESOLVED_TERMINAL"


def plan(symbols, bars, *, start, end, placements=()):
    """Full roster + fast full-bar oracle, before any raw working-tape read.

    placements are explicit original-input anchors, never inferred broker state.
    No-BUY names remain in the manifest and require their full-day census gates.
    """
    if len(set(symbols)) != len(symbols) or not 1 <= len(symbols) <= 256:
        raise ValueError("original_bounded_symbol_roster_required")
    left, right = nanos(start), nanos(end)
    if not left < right:
        raise ValueError("observed_window_bounds_required")
    grouped = {symbol: [] for symbol in symbols}
    for row in bars:
        if row["symbol"] not in grouped:
            raise ValueError("foreign_bar_outside_roster")
        grouped[row["symbol"]].append(row)
    windows, lines = [], {}
    for symbol in symbols:
        computed = recompute(grouped[symbol], nanos=nanos)
        lines[symbol] = computed
        sells = [item["available_ns"] for item in computed if item["oracle"]["flip"] == "SELL"]
        for item in computed:
            if item["oracle"]["flip"] != "BUY":
                continue
            buy = item["available_ns"]
            boundary = next((clock for clock in sells if clock > buy), None)
            if boundary is not None and boundary >= right:
                boundary = None
            # Include the flip minute: watched-prefix/coverage and minute-body
            # inputs must not be rewritten as a favorable post-touch subset.
            windows.append(WorkingWindow(symbol, max(left, item["start_ns"]), boundary,
                right, "RECOMPUTED_BUY:" + str(item["original"]["id"]), boundary is not None,
                status="UNRESOLVED_TERMINAL" if boundary is not None else "ASOF_OPEN"))
    for item in placements:
        symbol, clock = item["symbol"], nanos(item["at"])
        if symbol not in grouped or clock >= right or not item.get("source"):
            raise ValueError("original_placement_source_required")
        sells = [row["available_ns"] for row in lines[symbol] if row["oracle"]["flip"] == "SELL"]
        boundary = next((at for at in sells if max(left, clock) < at < right), None)
        windows.append(WorkingWindow(symbol, max(left, clock // MINUTE * MINUTE), boundary,
            right, "RECORDED_PLACEMENT:" + str(item["source"]) + ':original_ns=' + str(clock), boundary is not None,
            status="UNRESOLVED_TERMINAL" if boundary is not None else "ASOF_OPEN"))
    return dict(symbols=list(symbols), line_rows=lines, windows=tuple(windows),
                no_window_symbols=[symbol for symbol in symbols if not any(w.symbol == symbol for w in windows)],
                source_population="ALL original symbols retained; no-window is not no-fault proof")


def terminal(window, rows, *, policy, source, complete, null_policy=None):
    """Resolve only the unconditional boundary tail, not guessed target timing.

    Probe is advisory selection. Final payload includes ALL rows from the
    working prefix THROUGH the entire eligible timestamp group, including odd
    lots/UNKNOWNs; the original engine still determines every entry and exit.
    """
    from atrsimlive1_null_policy import classify_row_execution, require_null_policy
    from project_mai_tai.atr_simulation.types import EventKey, TradePrint

    require_null_policy(null_policy)
    if not window.terminal_pending:
        return window
    previous = None
    for row in rows:
        clock, sequence = nanos(row["event_ts"]), row["id"]
        key = clock, sequence
        if (row["symbol"] != window.symbol or row.get("provider") != "massive"
                or clock < window.boundary_ns or clock >= window.asof_end_ns
                or previous is not None and key <= previous):
            raise ValueError("exact_causal_terminal_probe_required")
        previous = key
        if type(row["size"]) is not int or row["size"] < 0:
            continue  # Full-day invalid-size census still must preserve the fault.
        trade = TradePrint(EventKey(clock, sequence, "market_capture_trades:" + str(sequence),
                                   source, nanos(row["received_at"])),
                           window.symbol, Decimal(str(row["price"])), row["size"])
        evidence = classify_row_execution(trade, row, policy, null_policy=null_policy)
        if evidence.eligible is True:
            return replace(window, terminal_pending=False, end_key=key,
                           status="BOUNDARY_FIRST_ELIGIBLE_KEY")
    if complete is not True:
        return window  # Caller must continue under original page/row/time caps.
    return replace(window, terminal_pending=False, status="ASOF_NO_ELIGIBLE_TERMINATOR")


def selection(windows):
    """An unresolved tail is never a closed SQL window or an empty success."""
    if any(item.terminal_pending for item in windows):
        raise ValueError("terminal_search_or_asof_proof_required")
    return [dict(symbol=item.symbol, start_ns=item.start_ns,
                 end_exclusive_ns=item.asof_end_ns if item.end_key is None else None,
                 end_inclusive_timestamp_ns=None if item.end_key is None else item.end_key[0],
                 terminal_witness_key=item.end_key, origin=item.origin, status=item.status)
            for item in windows]


# The integrator supplies values with the existing bound cursor; identifiers
# and conditions are literal. This is a source proposal, not an executed query.
TRADE_WINDOW_PREDICATE = """provider = 'massive' AND symbol = %(symbol)s
 AND received_at <= %(asof)s AND EXISTS (
 SELECT 1 FROM unnest(%(starts)s::timestamptz[], %(ends)s::timestamptz[],
                     %(inclusive_ends)s::timestamptz[])
 AS w(lo, hi, last_ts)
 WHERE event_ts >= w.lo AND ((w.hi IS NOT NULL AND event_ts < w.hi)
 OR (w.last_ts IS NOT NULL AND event_ts <= w.last_ts)))"""

QUOTE_CARRY_IN_PREDICATE = """provider = 'massive' AND symbol = %(symbol)s
 AND received_at <= %(asof)s AND event_ts >= %(day_start)s AND event_ts < %(window_start)s
 ORDER BY event_ts DESC, id DESC LIMIT 1"""
