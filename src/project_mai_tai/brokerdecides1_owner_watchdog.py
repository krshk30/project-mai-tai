"""L12 owner-lock detector/dedup. No phone imports, DB, provider or entry grant.

Production phone adapter must be an injected async callable returning EXACT
True for confirmed publisher acceptance, not device delivery. Existing health page/push functions return
None: that is UNKNOWN, not confirmation and not permission to consume an event.
"""

from dataclasses import dataclass
from hashlib import sha256
import logging

from project_mai_tai.brokerdecides1_sync import account_names, enabled

LOGGER = logging.getLogger(__name__)
HOLD_MS = 5 * 60_000  # C1 2026-10-23 10:35:49, strictly MORE than five minutes.
RETRY_MS = 60_000


@dataclass
class OwnerHold:
    opportunity: int
    since_ms: int
    logged: bool = False
    delivered: bool = False
    last_attempt_ms: int | None = None
    inflight: bool = False


def note_owner_refusal(strategy, state, *, reason, allowed):
    if not enabled(getattr(strategy, "settings", None)):
        return
    hold = getattr(state, "_brokerdecides1_owner_hold", None)
    identity = int(state.flip_owner_opportunity_id or state.fanout_segment_id or 0)
    now = strategy._now_ms()
    if allowed or state.flip_owner_phase != "unknown" or identity <= 0:
        state._brokerdecides1_owner_hold = None
    elif reason == "owner_phase_unknown" and (hold is None or hold.opportunity != identity or now < hold.since_ms):
        state._brokerdecides1_owner_hold = OwnerHold(identity, now)


def _native_flat_now(strategy, state, now):
    memory = getattr(strategy, "_brokerdecides1_memory", None)
    snapshot = memory.snapshot if memory is not None else None
    accounts = account_names(strategy.settings)
    age = getattr(strategy.settings, "strategy_schwab_1m_v2_brokerdecides1_max_age_ms", 0)
    if (snapshot is None or type(age) is not int or not 0 < age <= 20_000
            or len(set(accounts)) != 2 or len(snapshot.books) != 2
            or {book.account for book in snapshot.books} != set(accounts)
            or {book.provider for book in snapshot.books} != {"schwab", "webull"}
            or not 0 < snapshot.started_ms <= snapshot.completed_ms <= now
            or any(not book.complete or not snapshot.started_ms <= book.wire_started_ms <= book.wire_completed_ms <= snapshot.completed_ms
                or not 0 <= now - book.wire_started_ms < age
                or state.symbol in book.positions or state.symbol in book.working for book in snapshot.books)):
        return None
    return snapshot


async def owner_watchdog_pass(service):
    """Background poll only. One alert per hold; pending phone is never discarded.

    Local uncertainty can coexist with native-flat books: alert it, NEVER clear
    it here. A new owner/resolved phase resets dedup. No per-tick allocation map.
    """
    strategy = service.strategy
    if not enabled(getattr(strategy, "settings", None)):
        return
    now = strategy._now_ms()
    for state in tuple(getattr(strategy, "_symbol_states", {}).values()):
        hold = getattr(state, "_brokerdecides1_owner_hold", None)
        if hold is None:
            continue
        identity = int(state.flip_owner_opportunity_id or state.fanout_segment_id or 0)
        if state.flip_owner_phase != "unknown" or identity != hold.opportunity:
            state._brokerdecides1_owner_hold = None
            continue
        if now < hold.since_ms:
            hold.since_ms = now
            continue
        snapshot = _native_flat_now(strategy, state, now)
        if now - hold.since_ms <= HOLD_MS or snapshot is None:
            continue
        event_id = sha256(f"{state.symbol}|{identity}|{hold.since_ms}".encode()).hexdigest()
        if not hold.logged:
            LOGGER.error("[BROKERDECIDES1-OWNER-LOCK] symbol=%s opportunity=%d age_ms=%d event_id=%s "
                "both_native_books_flat=1 permission_granted=0 local_complete=%s own_inflight=%s",
                state.symbol, identity, now - hold.since_ms, event_id,
                snapshot.local_complete, state.symbol in snapshot.inflight)
            hold.logged = True
        if hold.delivered or hold.inflight or hold.last_attempt_ms is not None and now - hold.last_attempt_ms < RETRY_MS:
            continue
        from project_mai_tai.brokerdecides1_alert_transport import schedule_alert

        def current_owner(observations, state=state, hold=hold, identity=identity):
            current = strategy._now_ms()
            return (enabled(strategy.settings)
                    and getattr(state, "_brokerdecides1_owner_hold", None) is hold
                    and state.flip_owner_phase == "unknown"
                    and int(state.flip_owner_opportunity_id or state.fanout_segment_id or 0) == identity
                    and current - hold.since_ms > HOLD_MS
                    and _native_flat_now(strategy, state, current) is not None)

        schedule_alert(service, hold, now_ms=now, title="RED BROKERDECIDES1 owner lock", body=(
                f"symbol={state.symbol} opportunity={identity} age_ms={now - hold.since_ms} "
                f"both_native_books_flat=1 event_id={event_id}; admission NOT granted"), event_id=event_id,
                still_valid=current_owner)
