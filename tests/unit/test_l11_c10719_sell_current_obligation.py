"""CONTROLLED SELL ownership, not CLWT historical/native position proof."""

from dataclasses import replace

import pytest

from project_mai_tai.brokerdecides1 import AdmissionMemory, AdmissionSnapshot, NativeAccountBook
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook

NOW = 1792765145000
SELL = 1792765140000
IDENTITY = 1792758487033
ACCOUNTS = ("live:schwab_1m_v2", "live:orb")


def probe(*, keepwatch=True, card=True, symbol="CLWT"):
    strategy = SchwabV2Strategy(Settings(
        strategy_schwab_1m_v2_keepwatch1_enabled=keepwatch,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=False,
        strategy_schwab_1m_v2_account_name=ACCOUNTS[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNTS[1]))
    strategy._now_ms = lambda: NOW
    writes = []
    strategy.configure_fanout_identity_persistence(
        lambda symbol, identity, active, reason: writes.append(("identity", identity, active)))
    strategy.configure_flip_entry_ownership(
        lambda record, active, reason: writes.append(("owner", record.opportunity_id, active)),
        restore_readable=True)
    state = strategy.watchlist_state(symbol)
    state.atr_state = "short"
    state.atr_short_flip_bar_ts = SELL
    state.bars = [OHLCVBar(SELL, 2.3, 2.4, 2.2, 2.3, 1000)]
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    state.flip_owner_opportunity_id = state.fanout_segment_id = IDENTITY
    if card:
        strategy._brokerdecides1_memory = AdmissionMemory()
        strategy._brokerdecides1_memory.apply(AdmissionSnapshot(
            "CONTROLLED", 1, NOW - 200, NOW - 10,
            tuple(NativeAccountBook(account, provider, NOW - 100, NOW - 20,
                frozenset(), frozenset(), True, "CONTROLLED_NOT_NATIVE")
                for account, provider in zip(ACCOUNTS, ("schwab", "webull"))),
            frozenset(), frozenset(), True, "CONTROLLED"))
    return strategy, state, writes


def retired(state, writes):
    assert state.flip_owner_phase == "idle"
    assert state.fanout_segment_id == state.flip_owner_opportunity_id == 0
    assert state.flip_owner_sell_reset_pending is None
    assert [(kind, identity) for kind, identity, active in writes if not active] == [
        ("identity", IDENTITY), ("owner", IDENTITY)]


@pytest.mark.parametrize("card", [False, True])
def test_never_sent_probe_ends_at_sell_without_l12_permission(card):
    strategy, state, writes = probe(card=card)
    strategy._end_flip_owner_on_sell(state)
    retired(state, writes)


@pytest.mark.parametrize("terminal", ["cancelled", "expired", "closed_fill"])
def test_terminal_prior_history_is_not_a_current_obligation(terminal):
    strategy, state, writes = probe()
    state.flip_owner_first_rest_placed = True
    state.fanout_claim_outcome = "released"
    if terminal == "closed_fill":
        state.flip_owner_fill_accounts.add(ACCOUNTS[0])
        state.flip_owner_position_ids[ACCOUNTS[0]] = "CONTROLLED_CLOSED_ROW"
    strategy._end_flip_owner_on_sell(state)
    retired(state, writes)


@pytest.mark.parametrize("blocker", [
    "position", "working_schwab", "working_webull", "unanswered", "reservation", "restart"])
def test_current_real_obligation_types_never_retire_at_sell(blocker):
    strategy, state, writes = probe()
    memory = strategy._brokerdecides1_memory
    snapshot = memory.snapshot
    if blocker == "position":
        state.position_qty = 1
    elif blocker.startswith("working"):
        provider = blocker.split("_")[1]
        memory.apply(replace(snapshot, generation=2, books=tuple(
            replace(book, working=frozenset({state.symbol})) if book.provider == provider
            else book for book in snapshot.books)))
    elif blocker == "unanswered":
        memory.apply(replace(snapshot, generation=2, inflight=frozenset({state.symbol})))
    elif blocker == "reservation":
        memory.reserve(state.symbol, "CONTROLLED_UNANSWERED_SUBMIT")
    else:
        state.restart_rest_evidence_pending = True
        state.restart_rest_cancel_fields = {"resting_active": False}
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_opportunity_id == state.fanout_segment_id == IDENTITY
    assert not any(not active for _, _, active in writes)


def test_working_order_ends_then_actual_position_callback_retires_exact_owner():
    strategy, state, writes = probe()
    memory = strategy._brokerdecides1_memory
    clear = memory.snapshot
    memory.apply(replace(clear, generation=2, books=(
        replace(clear.books[0], working=frozenset({state.symbol})), clear.books[1])))
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_sell_reset_pending == (IDENTITY, SELL)
    memory.apply(replace(clear, generation=3))
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    retired(state, writes)


def test_pending_retirement_cannot_clear_replacement_identity():
    strategy, state, writes = probe()
    state.flip_owner_sell_reset_pending = (IDENTITY, SELL)
    state.flip_owner_opportunity_id = state.fanout_segment_id = IDENTITY + 1
    strategy.apply_flip_position_book(FlipPositionBook(NOW, True, {}))
    assert state.flip_owner_opportunity_id == IDENTITY + 1
    assert not any(not active for _, _, active in writes)


@pytest.mark.parametrize("unknown", ["position_read", "restore", "missing_card_after_send"])
def test_unknown_economic_evidence_is_not_flatness(unknown):
    strategy, state, writes = probe(card=False)
    if unknown == "position_read":
        state.flip_owner_evidence_readable = False
    elif unknown == "restore":
        strategy._flip_owner_restore_readable = False
    else:
        state.flip_owner_first_rest_placed = True
        state.cw_v2_emit_claimed = True
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not any(not active for _, _, active in writes)


@pytest.mark.parametrize("store", ["identity", "owner"])
def test_store_failure_does_not_release_identity(store):
    strategy, state, writes = probe()
    def fail(*args):
        raise OSError("CONTROLLED durable retirement failure")
    if store == "identity":
        strategy._fanout_identity_persist = fail
    else:
        strategy._flip_owner_persist = fail
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == IDENTITY


def test_keepwatch_off_retains_exact_legacy_idle_contradiction():
    strategy, state, writes = probe(keepwatch=False, card=False)
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_phase == "unknown"
    assert state.flip_owner_opportunity_id == IDENTITY
    assert state.flip_owner_sell_reset_pending is None
    assert not any(not active for _, _, active in writes)


def test_sprc_premarket_fill_then_buy_confirmation_still_holds_unknown(caplog):
    strategy, state, writes = probe(symbol="SPRC")
    state.flip_owner_phase = "resting"
    state.flip_owner_first_rest_placed = True
    state.position_qty = 196  # CONTROLLED positive holding, not a flat seed.
    strategy._note_flip_owner_fill(state, account_name=ACCOUNTS[1],
                                  reason="CONTROLLED_PREMARKET_FILL")
    assert state.flip_owner_phase == "provisional"
    # The broker fill preceded confirmation. No matching managed-row binding
    # arrived in the controlled interval: this must not become a fresh entry.
    strategy._now_ms = lambda: NOW + 120000
    strategy._bind_flip_owner_on_buy_flip(state, flip_bar_ts=SELL + 60000)
    assert state.flip_owner_phase == "unknown"
    assert state.position_qty == 196
    assert state.flip_owner_opportunity_id == state.fanout_segment_id == IDENTITY
    assert state.flip_owner_fill_accounts == {ACCOUNTS[1]}
    assert state.flip_owner_sell_reset_pending is None
    assert "reason=buy_flip_owner_not_proven" in caplog.text
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    assert not any(not active for _, _, active in writes)


@pytest.mark.parametrize("field", ["positions", "working"])
def test_foreign_account_same_symbol_is_not_a_bot_account_obligation(field):
    strategy, state, writes = probe()
    memory = strategy._brokerdecides1_memory
    original = memory.snapshot
    foreign = replace(original.books[0], account="live:momentum_warn_live",
                      **{field: frozenset({state.symbol})})
    memory.apply(replace(original, generation=2, books=(foreign, original.books[1])))
    strategy._end_flip_owner_on_sell(state)
    retired(state, writes)


@pytest.mark.parametrize("card", ["missing", "stale"])
@pytest.mark.parametrize("terminal_kind", ["cancelled", "expired"])
def test_proven_terminal_unfilled_owner_ends_without_current_l12_card(card, terminal_kind):
    strategy, state, writes = probe(card=card != "missing")
    state.flip_owner_first_rest_placed = True
    state.fanout_claim_outcome = "released"
    now = NOW + 20000 if card == "stale" else NOW
    strategy._now_ms = lambda: now
    # This is the explicit CONTROLLED output contract of the existing real
    # terminal-order census, not a local release, cancel draft or native claim.
    assert terminal_kind in {"cancelled", "expired"}
    strategy.apply_flip_position_book(FlipPositionBook(now, True, {},
        terminal_unfilled_opportunities_by_symbol={state.symbol: frozenset({IDENTITY})}))
    assert state.flip_owner_terminal_unfilled_proof == (IDENTITY, now)
    strategy._end_flip_owner_on_sell(state)
    retired(state, writes)


@pytest.mark.parametrize("fault", ["wrong_identity", "unreadable", "stale", "send_reserved"])
def test_terminal_proof_cannot_settle_foreign_stale_or_unanswered_send(fault):
    strategy, state, writes = probe(card=fault == "send_reserved")
    state.flip_owner_first_rest_placed = True
    state.fanout_claim_outcome = "released"
    identity = IDENTITY + 1 if fault == "wrong_identity" else IDENTITY
    strategy.apply_flip_position_book(FlipPositionBook(NOW, fault != "unreadable", {},
        terminal_unfilled_opportunities_by_symbol={state.symbol: frozenset({identity})}))
    if fault == "stale":
        strategy._now_ms = lambda: NOW + 16000
    elif fault == "send_reserved":
        strategy._brokerdecides1_memory.reserve(state.symbol, "CONTROLLED_UNANSWERED_SEND")
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not any(not active for _, _, active in writes)


def test_local_release_alone_does_not_attest_cancel_or_expiry():
    strategy, state, writes = probe(card=False)
    state.flip_owner_first_rest_placed = True
    strategy._release_fanout_webull_claim(state, reason="grace_backstop_not_a_broker_answer")
    assert state.fanout_claim_outcome == "released"
    assert state.flip_owner_terminal_unfilled_proof is None
    strategy._end_flip_owner_on_sell(state)
    assert state.flip_owner_opportunity_id == IDENTITY
    assert not any(not active for _, _, active in writes)
