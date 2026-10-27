"""Read-only proof of a jointly empty closed interval, never entry permission."""

from project_mai_tai.market_data.keepwatch_history import history_coverage, history_has_candidate
from project_mai_tai.strategy_core.session_line_restore import is_keepwatch_coverage


MINUTE_MS = 60_000


def current_history_response(history, *, symbol, anchor_ms, through_ms, now_ms):
    """Millisecond-clock admission; provider responses cannot come from later ms."""
    if (history is None or history.symbol != symbol or history.anchor_ms != anchor_ms
            or type(history.through_ms) is not int or history.through_ms < through_ms
            or history.through_ms + MINUTE_MS > now_ms
            or now_ms - history.through_ms > 180_000
            or type(history.started_ns) is not int or type(history.completed_ns) is not int
            or not 0 < history.started_ns <= history.completed_ns
            or history.completed_ns // 1_000_000 > now_ms
            or history.through_ms + MINUTE_MS > history.started_ns // 1_000_000):
        return False
    return all(isinstance(value, str) and len(value) == 64
               and all(char in "0123456789abcdef" for char in value)
               for value in (history.massive_sha256, history.schwab_sha256))


def attested_empty_interval(ledger, history, *, symbol, anchor_ms, previous_ms,
                            through_ms, now_ms, resumed):
    """Require positive covered emptiness, matching values and no tape conflict.

    A resumed interval excludes its final real candle; an empty tail includes its
    final queried minute. Service owner/epoch/source CAS remains caller-owned.
    """
    stamps = (anchor_ms, previous_ms, through_ms, now_ms)
    if (any(type(value) is not int for value in stamps) or type(resumed) is not bool
            or previous_ms % MINUTE_MS or through_ms % MINUTE_MS
            or not anchor_ms <= previous_ms < through_ms
            or through_ms + MINUTE_MS > now_ms or now_ms - through_ms > 180_000
            or ledger.symbol != symbol or ledger.anchor_ms != anchor_ms
            or not current_history_response(history, symbol=symbol, anchor_ms=anchor_ms,
                                             through_ms=through_ms, now_ms=now_ms)):
        return False
    proof = ledger._coverage
    if (not is_keepwatch_coverage(proof)
            or proof.complete is not True or previous_ms not in proof.closed_ids):
        return False
    try:
        admitted = tuple(ledger._bars[stamp] for stamp in proof.closed_ids)
    except KeyError:
        return False
    if history_coverage(history, admitted, canonical=ledger.producer_pins is not None) != proof or ledger.prepare() is None:
        return False
    if resumed and (through_ms not in proof.closed_ids
                    or ledger.current_bar_ms != through_ms):
        return False
    end = through_ms if resumed else through_ms + MINUTE_MS
    empty = set(range(previous_ms + MINUTE_MS, end, MINUTE_MS))
    if (not empty or not empty <= set(history.empty_minutes)
            or not empty <= set(proof.empty_minutes) or empty.intersection(ledger._bars)):
        return False
    # This synthetic right bound checks an empty tail without inventing a bar.
    return not ledger.has_unrecovered_traded_gap(((previous_ms, end),))


def attested_repaired_interval(ledger, history, *, symbol, anchor_ms, previous_ms,
                               through_ms, now_ms):
    """Actual provider candles repair a data gap without an EMPTY/TR exception.

    Tape in a recovered candle is not an empty-interval conflict. The entire
    repaired run must exist with matching admitted values in the parsed provider
    result; cached/deque/inbox-only rows cannot stand in for that result.
    """
    if (any(type(value) is not int for value in (anchor_ms, previous_ms, through_ms, now_ms))
            or previous_ms % MINUTE_MS or through_ms % MINUTE_MS
            or not anchor_ms <= previous_ms < through_ms
            or ledger.symbol != symbol or ledger.anchor_ms != anchor_ms
            or ledger.current_bar_ms != through_ms
            or not current_history_response(history, symbol=symbol, anchor_ms=anchor_ms,
                                             through_ms=through_ms, now_ms=now_ms)):
        return False
    proof = ledger._coverage
    if (not is_keepwatch_coverage(proof)
            or proof.complete is not True or previous_ms not in proof.closed_ids):
        return False
    stamps = tuple(range(previous_ms + MINUTE_MS, through_ms + MINUTE_MS, MINUTE_MS))
    if (set(stamps).intersection(history.empty_minutes)
            or set(stamps).intersection(proof.empty_minutes)):
        return False
    try:
        admitted = tuple(ledger._bars[stamp] for stamp in proof.closed_ids)
        actual = tuple(ledger._bars[stamp] for stamp in stamps)
    except KeyError:
        return False
    return (history_coverage(history, admitted, canonical=ledger.producer_pins is not None) == proof
            and set(stamps) <= set(proof.closed_ids)
            and all(history_has_candidate(history, bar) for bar in actual)
            and ledger.prepare() is not None)
