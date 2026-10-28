"""Pure Option A entry planning from supplied watched-flip and tape evidence."""

from hashlib import sha256
import json

from .types import BodyObservation, EntryPlan, EpisodeInput, FillModel, FlipIdentity, Quote, SimOrder
from .volume_body import MINUTE_NS, MinuteBody, body_covers, legal_cap
from .execution_eligibility import classify_execution, execution_status, note_unknown


def episode_id(identity: FlipIdentity) -> str:
    payload = (
        identity.trading_day, identity.symbol, identity.broker, identity.account_id,
        identity.strategy, identity.flip_id, identity.segment_id,
    )
    return sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def placement_book(cap, quote: Quote | None) -> tuple[str, int | None]:
    """Exchange-event-clock book model, not original received/as-known proof."""
    if quote is None or any(
        price is None or not price.is_finite() or price <= 0
        for price in (quote.bid, quote.ask)
    ):
        return "UNKNOWN_INVALID_BOOK", None
    if quote.bid > quote.ask:
        return "UNKNOWN_CROSSED_BOOK", None
    if cap >= quote.ask:
        size = quote.ask_size
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return "UNKNOWN_ASK_SIZE", None
        return "MARKETABLE_ASK", 0
    if cap > quote.bid:
        return "NEW_BEST_BID", 0
    if cap == quote.bid:
        size = quote.bid_size
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            return "UNKNOWN_BID_SIZE", None
        return "JOIN_BID", size
    return "UNKNOWN_DEEPER_BOOK", None


def prepare_entry(episode: EpisodeInput, model: FillModel) -> EntryPlan:
    issues: list[str] = []
    if episode.watched is not True or episode.watched_source is None:
        return EntryPlan(None, (), False, ("UNKNOWN_OR_NOT_WATCHED",))
    if episode.trigger is None or episode.quantity is None or episode.sizing_source is None:
        return EntryPlan(None, (), False, ("UNKNOWN_TRIGGER_OR_SIZING",))
    if isinstance(episode.quantity, bool) or not isinstance(episode.quantity, int) or episode.quantity <= 0:
        raise ValueError("positive_integer_quantity_required")
    cap = legal_cap(episode.trigger)
    end_ns = min(
        episode.session_end.available_ns,
        episode.sell_flip.available_ns if episode.sell_flip is not None else episode.session_end.available_ns,
    )
    if end_ns <= episode.buy_flip.available_ns:
        return EntryPlan(None, (), False, ("INVALID_FLIP_BOUNDARY_ORDER",))
    flip = episode.flip_coverage
    if episode.flip_coverage.complete is not True:
        issues.append("UNKNOWN_FLIP_COVERAGE")
    if flip.start_ns > episode.buy_flip.available_ns or flip.end_ns < end_ns:
        issues.append("UNKNOWN_FLIP_WINDOW")
    issues.extend(flip.gaps)
    if issues:
        return EntryPlan(None, (), False, tuple(dict.fromkeys(issues)))
    if episode.tape_coverage.complete is not True:
        issues.append("UNKNOWN_TAPE_COVERAGE")
    if episode.tape_coverage.start_ns > episode.buy_flip.available_ns:
        issues.append("UNKNOWN_BUY_FLIP_PREFIX")
    issues.extend(episode.tape_coverage.gaps)
    body = MinuteBody(episode.identity.symbol)
    touched = False
    observations: list[BodyObservation] = []
    unknowns = {}
    order = None
    for trade in episode.trades:
        if trade.key.event_ns >= end_ns:
            break
        body.add(trade)  # Pre-flip/pre-touch prints remain in this minute's denominator.
        if trade.key.event_ns < episode.buy_flip.available_ns:
            continue
        eligible, _ = execution_status(trade)
        raw_touch = trade.price >= episode.trigger
        if not touched and raw_touch:
            if eligible is True:
                touched = True
            elif eligible is None:
                note_unknown(issues, "UNKNOWN_EXECUTION_TOUCH_ELIGIBILITY")
                unknowns[trade.key.raw_id] = trade.execution or classify_execution(trade, None, None)
        if not touched and not raw_touch:
            continue
        minute_ns = trade.key.event_ns // MINUTE_NS * MINUTE_NS
        prefix_complete = (
            episode.tape_coverage.complete is True
            and episode.tape_coverage.start_ns <= minute_ns
        )
        snapshot = body.snapshot(prefix_complete=prefix_complete)
        ready = body_covers(snapshot, episode.trigger)
        observations.append(BodyObservation(trade.key, snapshot, touched, ready))
        if not ready or not touched:
            continue
        if eligible is not True:
            if eligible is None:
                note_unknown(issues, "UNKNOWN_EXECUTION_GO_ELIGIBILITY")
                unknowns[trade.key.raw_id] = trade.execution or classify_execution(trade, None, None)
            continue
        if not prefix_complete:
            issues.append("UNKNOWN_PRETOUCH_MINUTE_PREFIX")
            continue
        # SIP event ordering is explicit; received_ns is retained, not backdated.
        known_quotes = tuple(
            q for q in episode.quotes
            if q.symbol == trade.symbol
            and (q.key.event_ns, q.key.sequence) <= (trade.key.event_ns, trade.key.sequence)
        )
        quote = max(known_quotes, key=lambda q: (q.key.event_ns, q.key.sequence), default=None)
        branch, queue = placement_book(cap, quote)
        if model == FillModel.QUEUE_AWARE and queue is None:
            issues.append("UNKNOWN_QUEUE_DEPTH_AT_PLACEMENT")
            issues.append(branch)
        order = SimOrder(
            f"ATRSIM1:{episode_id(episode.identity)}:{model.value}", episode.identity,
            model, trade.key, cap, episode.quantity, queue,
            quote.key.source if quote is not None else None,
            branch if model == FillModel.QUEUE_AWARE else "OPTIMISTIC_LATER_PRINT",
            quote,
            end_ns,
        )
        break  # One simulated order per segment, no stacking on later readiness.
    return EntryPlan(order, tuple(observations), touched, tuple(dict.fromkeys(issues)), tuple(unknowns.values()))
