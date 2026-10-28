"""L12 in-memory strategy and final-publication fences, additive to old gates."""

import logging
from datetime import UTC, datetime

from project_mai_tai.brokerdecides1 import AdmissionMemory, Decision
from project_mai_tai.brokerdecides1_sync import account_names, enabled
from project_mai_tai.fanout_segment_store import current_session_anchor

LOGGER = logging.getLogger(__name__)


def line_segment(state):
    # The confirmed SHORT boundary, not a synthetic fanout opportunity minted
    # during restoration. The fresh BUY exception explicitly carries that boundary.
    if (state.slotclear_fresh_buy_bar_ms and state.bars
            and state.slotclear_fresh_buy_bar_ms == state.bars[-1].timestamp_ms
            and state.atr_state == "long"):
        return int(state.slotclear_fresh_buy_segment_id or 0)
    return int(state.atr_short_flip_bar_ts or 0)


def exception_scope(strategy, state, *, at_sell=False):
    """Pure OLD blocker/provenance check. Never inspect L12 memory here.

    Only the current OLD blocker counts. A completed retirement does not leave
    a segment-wide obligation on later ordinary entries.
    """
    if not enabled(getattr(strategy, "settings", None)) or state is None:
        return False
    segment = line_segment(state)
    cap = (getattr(state, "cw_seed_cap_watch_start_ms", 0) > 0
        and getattr(state, "cw_resting_taken", False) and getattr(state, "cw_reclaim_taken", False))
    owner = (getattr(strategy, "_flip_owned_first_entry_enabled", False)
        and getattr(state, "flip_owner_phase", None) == "unknown"
        and int(state.flip_owner_opportunity_id or state.fanout_segment_id or 0) > 0)
    # OLD's SELL path rejects an idle record carrying a contradictory identity.
    # Do not generalize that SELL-only condition into an ordinary BUY gate.
    idle_sell = (at_sell and getattr(strategy, "_flip_owned_first_entry_enabled", False)
        and getattr(state, "flip_owner_phase", None) == "idle"
        and 0 < int(state.flip_owner_opportunity_id or state.fanout_segment_id or 0) < segment)
    return bool(cap or owner or idle_sell)


def entry_uses_exception(strategy, symbol):
    state = getattr(strategy, "_symbol_states", {}).get(str(symbol).upper())
    return exception_scope(strategy, state)


def draft_uses_exception(strategy, draft):
    """An already journaled exception stays scoped to THAT draft, not a symbol.

    A real fresh-journal capability is process-local, identity-checked and
    single-use at PublicationBoundary. Plain metadata/restored rows cannot mint
    it. This preserves final native recheck if the OLD note clears during L11's
    await, without imposing L12 memory on a later unrelated draft.
    """
    if (not enabled(getattr(strategy, "settings", None))
            or draft.side != "buy" or draft.intent_type != "open"):
        return False
    if entry_uses_exception(strategy, draft.symbol):
        return True
    from project_mai_tai.brokerdecides1_prewire_authority import FreshJournalCapability, SIDECAR

    return type(getattr(draft, SIDECAR, None)) is FreshJournalCapability


def entry_decision(strategy, symbol, *, at_sell=False):
    settings = getattr(strategy, "settings", None)
    if not enabled(settings):
        return Decision(True, "flag_off", None)
    state = strategy._symbol_states.get(symbol.upper())
    if not exception_scope(strategy, state, at_sell=at_sell):
        return Decision(True, "outside_L12_exception", None)
    return _exception_decision(strategy, symbol)


def _exception_decision(strategy, symbol):
    """Actual exception assessment, including an exact already-bound draft."""
    settings = strategy.settings
    state = strategy._symbol_states.get(symbol.upper())
    if state is None:
        LOGGER.warning("[BROKERDECIDES1-ADMISSION] symbol=%s allowed=False "
                       "reason=line_segment_missing oldest_wire_age_ms=UNKNOWN", symbol)
        return Decision(False, "line_segment_missing", None)
    memory = getattr(strategy, "_brokerdecides1_memory", None)
    if memory is None:
        decision = Decision(False, "snapshot_missing", None)
    else:
        age_bound = getattr(settings, "strategy_schwab_1m_v2_brokerdecides1_max_age_ms", 0)
        decision = memory.assess(accounts=account_names(settings), symbol=state.symbol,
            segment=line_segment(state), now_ms=strategy._now_ms(),
            max_age_ms=age_bound if type(age_bound) is int and 0 < age_bound <= 20_000 else 0)
    bar = strategy._now_ms() // 60000
    # One bounded per-symbol log key in existing tracked state, not per-tick growth.
    if getattr(state, "_brokerdecides1_log_key", None) != (bar, decision.reason):
        state._brokerdecides1_log_key = (bar, decision.reason)
        LOGGER.info("[BROKERDECIDES1-ADMISSION] symbol=%s allowed=%s reason=%s "
            "oldest_wire_age_ms=%s segment=%s", state.symbol, decision.allowed,
            decision.reason, decision.oldest_age_ms, line_segment(state))
    return decision


def card_fill_segment_definite(strategy, state):
    """The existing publisher's COMPLETE fill census covers this actual SHORT.

    local_evidence reads existing v2 BUY fills since the current 04:00 ET anchor;
    legacy/unmapped fills become in-flight UNKNOWN. Do not extend that negative
    evidence to an older day or across a publication/session rollover. This is
    a card decision, NOT reconstruction of a retry counter or owner history.
    """
    if not exception_scope(strategy, state) or not entry_decision(strategy, state.symbol).allowed:
        return False
    segment = line_segment(state)
    now = strategy._now_ms()
    if type(segment) is not int or segment <= 0 or segment % 60_000 or segment > now:
        return False
    snapshot = strategy._brokerdecides1_memory.snapshot

    def anchor(stamp):
        return int(current_session_anchor(datetime.fromtimestamp(stamp / 1000, UTC)).timestamp() * 1000)

    current = anchor(now)
    return (anchor(snapshot.started_ms) == anchor(snapshot.completed_ms) == current
        and segment >= current)


def reconstructed_first_admitted(strategy, state, *, slot="first"):
    """Exception to a cap-only note, never to economic ownership.

    The binding survives opportunity allocation, not a fill/emit/owner transition.
    Every call rechecks current native evidence; the binding is NOT a cached GO.
    Original cap/owner/retry notes are neither cleared nor rewritten here.
    """
    if (not enabled(getattr(strategy, "settings", None)) or slot != "first"
            or not getattr(state, "cw_seed_cap_watch_start_ms", 0)
            or not state.cw_resting_taken or not state.cw_reclaim_taken
            or state.flip_owner_phase != "idle"
            or state.flip_owner_first_rest_placed or state.flip_owner_fill_accounts
            or state.flip_owner_position_ids or state.flip_owner_open_positions
            or state.position_qty or state.position_qty_held
            or state.resting_active or state.webull_resting_active
            or state.cw_v2_emit_claimed or state.fanout_webull_claimed
            or state.fanout_claim_outcome == "filled"
            or strategy._falseflip_entry_blocked(state)
            or strategy._removed_wait_gate_closed(state.symbol)
            or not entry_decision(strategy, state.symbol).allowed):
        return False
    segment = line_segment(state)
    # Claude18:21:33: the card may answer the missing-segment permission question;
    # it must NOT manufacture a retry budget or discard unreadable/spent ownership.
    if strategy._flip_owned_first_entry_enabled:
        if (not strategy._flip_owner_restore_readable
                or not strategy._flip_owner_evidence_fresh(state)
                or (strategy._removed_wait_enabled and strategy._removed_wait_has_owner(state))):
            return False
        if strategy._retry_one_enabled:
            if (not state.retry_one_budget_readable
                    or strategy._falseflip_effective_closes(state, state.retry_one_closes_in_segment)
                        >= 1 + strategy._retry_one_max_retries):
                return False
            if state.retry_one_segment_id <= 0 or segment != state.retry_one_segment_id:
                if (state.retry_one_segment_id != 0 or state.retry_one_closes_in_segment != 0
                        or not card_fill_segment_definite(strategy, state)):
                    return False
    opportunity = (int(state.fanout_segment_id or 0), int(state.flip_owner_opportunity_id or 0))
    provenance = (segment, state.cw_seed_cap_watch_start_ms)
    binding = getattr(state, "_brokerdecides1_cap_binding", None)
    if binding is None:
        if opportunity != (0, 0):
            return False  # A pre-existing identity is never relabelled cap-only.
        state._brokerdecides1_cap_binding = (provenance, opportunity)
    elif binding != (provenance, opportunity):
        return False
    return True


def bind_reconstructed_opportunity(state, candidate):
    """Called only after successful creation of THIS new durable opportunity."""
    binding = getattr(state, "_brokerdecides1_cap_binding", None)
    if (binding is not None
            and binding == ((line_segment(state), state.cw_seed_cap_watch_start_ms), (0, 0))
            and candidate > 0 and state.flip_owner_phase == "idle"
            and state.fanout_segment_id == state.flip_owner_opportunity_id == candidate):
        state._brokerdecides1_cap_binding = (binding[0], (candidate, candidate))


def unused_owner_at_sell_definite(strategy, state):
    """Current SHORT, including a missed SELL or restored prior-day identity."""
    return _unused_owner_at_segment_definite(strategy, state, line_segment(state), sell=True)


def _restored_empty_owner_note(state):
    """A legacy restore sets placed=True even on an empty UNKNOWN note.

    This does NOT prove historical non-dispatch or flatness. It only identifies
    that restore-derived bit; the COMPLETE current broker/local card must still
    answer this later segment, including old unresolved sends. All other claims
    and original retry/line guards remain authoritative.
    """
    record = getattr(state, "_brokerdecides1_restored_empty_owner", None)
    return bool(record is not None and record.phase == state.flip_owner_phase == "unknown"
        and record.symbol == state.symbol
        and record.opportunity_id == state.flip_owner_opportunity_id == state.fanout_segment_id
        and record.retry_segment_id == state.flip_owner_retry_segment_id
        and record.retry_closes_at_place == state.flip_owner_retry_closes_at_place
        and not record.fill_accounts and not record.position_ids and not record.position_entry_ms
        and not record.flip_bar_ts and not record.provisional_started_ms
        and not state.flip_owner_flip_bar_ts and not state.flip_owner_provisional_started_ms)


def _unused_owner_at_segment_definite(strategy, state, segment, *, sell):
    required = ("flip_owner_phase", "atr_state", "bars", "flip_owner_first_rest_placed",
        "flip_owner_fill_accounts", "flip_owner_position_ids", "flip_owner_position_entry_ms",
        "flip_owner_open_positions", "position_qty", "position_qty_held", "resting_active",
        "webull_resting_active", "cw_v2_emit_claimed", "fanout_webull_claimed", "fanout_claim_slot_id",
        "fanout_claim_attempt_id", "fanout_claim_outcome", "restart_rest_evidence_pending",
        "restart_rest_recover_accounts", "restart_rest_cancel_fields", "flip_owner_opportunity_id", "fanout_segment_id")
    if not all(hasattr(state, name) for name in required):
        return False
    if (not enabled(getattr(strategy, "settings", None))
            or not getattr(strategy, "_flip_owner_restore_readable", False)
            or state.flip_owner_phase not in {"idle", "unknown"}
            or state.atr_state != ("short" if sell else "long") or not state.bars
            or type(segment) is not int or segment <= 0 or segment % 60_000
            or state.bars[-1].timestamp_ms < segment
            or not strategy._fanout_identity_bar_is_live(state)
            or (state.flip_owner_first_rest_placed and not _restored_empty_owner_note(state))
            or state.flip_owner_fill_accounts
            or state.flip_owner_position_ids or state.flip_owner_position_entry_ms
            or state.flip_owner_open_positions or state.position_qty or state.position_qty_held
            or state.resting_active or state.webull_resting_active
            or state.cw_v2_emit_claimed or state.fanout_webull_claimed
            or state.fanout_claim_slot_id or state.fanout_claim_attempt_id
            or state.fanout_claim_outcome not in {"", "released"}
            or state.restart_rest_evidence_pending or state.restart_rest_recover_accounts
            or state.restart_rest_cancel_fields
            or strategy._falseflip_entry_blocked(state)
            or state.symbol in getattr(strategy, "_removed_wait_requests", {})):
        return False
    opportunity = state.flip_owner_opportunity_id
    if (type(opportunity) is not int or not 0 < opportunity < segment
            or state.fanout_segment_id != opportunity):
        return False
    if not exception_scope(strategy, state, at_sell=sell):
        return False
    memory = getattr(strategy, "_brokerdecides1_memory", None)
    if memory is None:
        return False
    snapshot = memory.snapshot
    now = strategy._now_ms()
    age = getattr(strategy.settings, "strategy_schwab_1m_v2_brokerdecides1_max_age_ms", 0)
    if type(age) is not int or not 0 < age <= 20_000:
        return False
    decision = memory.assess(accounts=account_names(strategy.settings), symbol=state.symbol,
        segment=segment, now_ms=now, max_age_ms=age)
    if not decision.allowed or memory.snapshot is not snapshot:
        return False
    anchor = int(current_session_anchor(datetime.fromtimestamp(now / 1000, UTC)).timestamp() * 1000)
    # Older identities may retire; negative FILL coverage may NOT cross 04:00.
    # Prior unresolved sends still fence via the complete, crossday local census.
    return (snapshot.started_ms >= anchor and snapshot.completed_ms >= snapshot.started_ms
        and anchor <= segment <= now)


def recover_unused_current_short(strategy, state):
    """Background actor only. Normal ordered retirement, never an entry grant."""
    if (not enabled(getattr(strategy, "settings", None))
            or getattr(strategy, "_entries_held", True)
            or not strategy.line_buy_ready(state.symbol) or strategy.gap_hold_active(state.symbol)
            or not unused_owner_at_sell_definite(strategy, state)):
        return False
    return strategy._retire_flip_owner_opportunity(state, reason="brokerdecides_unused_identity_current_short")


def recover_unused_at_buy(strategy, state, signal):
    """Use only the actual live ATR callback's causal preceding SHORT identity."""
    segment = signal.get("decision_short_segment_id")
    if (not enabled(getattr(strategy, "settings", None))
            or signal.get("flip") != "BUY" or signal.get("observation_phase", "live") != "live"
            or strategy._bar_observation_phase != "live" or strategy._entries_held
            or strategy.gap_hold_active(state.symbol) or not strategy.line_buy_ready(state.symbol)
            or not _unused_owner_at_segment_definite(strategy, state, segment, sell=False)):
        return False
    if not strategy._retire_flip_owner_opportunity(state, reason="brokerdecides_unused_identity_new_buy_segment"):
        return False
    # Retirement clears old claims. Retain this callback's real SHORT for the
    # existing card; do not manufacture or reset a durable retry budget.
    state.slotclear_fresh_buy_bar_ms = int(state.bars[-1].timestamp_ms)
    state.slotclear_fresh_buy_segment_id = segment
    return True


def prepare_send(strategy, draft, account):
    """Synchronous last gate before XADD: assess, stamp, reserve with no await.

    The pre-existing attempt journal must already be committed by the service's
    tracked dispatcher. Unknown publication retains the reservation and journal.
    No new DB call is added here or to a bar/tick handler.
    """
    if (not enabled(getattr(strategy, "settings", None))
            or draft.side != "buy" or draft.intent_type != "open"):
        return
    if not draft_uses_exception(strategy, draft):
        return
    decision = _exception_decision(strategy, draft.symbol)
    if not decision.allowed:
        raise ValueError(f"brokerdecides1_hold:{decision.reason}")
    if account not in account_names(strategy.settings):
        raise ValueError("brokerdecides1_account_UNKNOWN")
    token = draft.metadata.get("clearwait_dispatch_token")
    if (not isinstance(token, str) or not token or len(token) > 128
            or getattr(strategy, "_removed_wait_dispatch_persist", None) is None):
        raise ValueError("brokerdecides1_dispatch_journal_UNKNOWN")
    state = strategy._symbol_states[draft.symbol.upper()]
    from project_mai_tai.atr_country_route import selection_from_metadata

    # The approved contract is one selected broker, not a two-leg waiver. A
    # legacy/unsealed fanout cannot send its first leg then discover the second
    # is reserved. It needs an explicit separately reviewed compatibility path.
    if not strategy._country_single_entry(state):
        raise ValueError("brokerdecides1_legacy_fanout_UNREVIEWED")
    selected = selection_from_metadata(draft.metadata, strategy.settings)
    current = strategy._country_existing_selection(state)
    if (selected != current or selected.account != account
            or selected.symbol != state.symbol
            or str(selected.segment_id) != str(draft.metadata.get("fanout_segment_id"))):
        raise ValueError("brokerdecides1_selected_account_UNKNOWN")
    draft.metadata["brokerdecides_line_segment_ms"] = str(line_segment(state))
    draft.metadata["brokerdecides_reservation_token"] = token
    memory = strategy._brokerdecides1_memory
    memory.reserve(state.symbol, token)


def initialize(strategy):
    if enabled(getattr(strategy, "settings", None)):
        strategy._brokerdecides1_memory = AdmissionMemory()
