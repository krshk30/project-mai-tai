"""F1 entry scope: real current publication, explicitly controlled membership.

The original ten-current-bar recovery control constructs the publication. No
entry, publication, or proof predicate is replaced by a favorable return value.
Native/boot admission remains held throughout these service-policy checks.
"""
import pytest

from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.strategy_core.keepwatch import KeepWatchSession
from tests.unit import test_keepwatch_current_bar_recovery as recovery


async def current_publication(monkeypatch):
    captured = []
    original = service.SchwabV2BotService

    class Captured(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            captured.append(self)

    with monkeypatch.context() as patch:
        patch.setattr(service, 'SchwabV2BotService', Captured)
        await recovery.test_real_dispatch_traded_gap_current_scope_does_not_attest_history(
            10, True, 330, False, patch)
    bot, = captured
    proof = bot._keepwatch_current_recoveries['IZM']
    now = proof.last_work.observed_ms
    bot.strategy._now_ms = lambda: now
    # Explicit CONTROLLED selected-scanner membership. It is separate from
    # native/fleet readiness and does not replace an admission predicate.
    bot._keepwatch = KeepWatchSession()
    bot._keepwatch.update(proof.anchor_ms, {'IZM'}, now, {'IZM': proof.state.atr_state})
    bot.settings = bot.settings.model_copy(update={
        'strategy_schwab_1m_v2_keepwatch_sized_trade_gap_enabled': True,
        'strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled': True})
    bot.strategy.settings = bot.settings
    assert bot._keepwatch_current_publication('IZM') is proof
    assert bot.strategy._entries_held and not bot._boot_state_restoration_complete
    return bot, proof


@pytest.mark.asyncio
async def test_ordinary_history_pending_unknown_keeps_real_current_publication_entry(monkeypatch):
    bot, proof = await current_publication(monkeypatch)
    for reason in ('closed_minute_history_pending', 'bar_history_read_pending'):
        bot._keepwatch_mark_unknown('IZM', reason)
        assert 'IZM' in bot._keepwatch_unknown
        assert not bot._keepwatch_rest_hold.deferring('IZM')
        assert bot._keepwatch_current_publication('IZM') is proof
        assert bot._keepwatch_entry_allowed('IZM')
        assert not bot._entry_membership_allowed('IZM')  # Boot/native unreadable.


@pytest.mark.asyncio
async def test_active_F1_deferral_blocks_new_entry_even_with_current_publication(monkeypatch):
    bot, recovery_proof = await current_publication(monkeypatch)
    symbol, ledger, state = 'IZM', recovery_proof.ledger, recovery_proof.state
    assert bot._keepwatch_entry_allowed(symbol)
    state.resting_active = state.resting_is_broker_order = True
    state.resting_schwab_generation = 'CONTROLLED-existing-native-generation'
    state.resting_schwab_quantity = 10
    ledger.incomplete_reason = 'stale_rebuild'  # CONTROLLED failed prefix preimage.
    operation = object()
    scope = (symbol, ledger.anchor_ms, ledger.epoch)
    bot._keepwatch_history_context.set((operation, scope))
    bot._keepwatch_history_scopes[operation] = scope
    through = recovery_proof.last_work.bar.timestamp_ms
    bot._keepwatch_hold_unknown(symbol, 'prefix_publication_unproven', transient_scope=dict(
        ledger=ledger, epoch=ledger.epoch, state=state,
        source_identity=bot._line_source_identity[symbol], expected_work=None,
        trade_through=through, trade_token=ledger.trade_evidence_token(through), anchor=ledger.anchor_ms))
    assert bot._keepwatch_rest_hold.deferring(symbol)
    assert bot._keepwatch_current_publication(symbol) is recovery_proof
    assert not bot._keepwatch_entry_allowed(symbol)
    assert state.resting_active and not bot.strategy.drain_pending_intents()
    assert not bot.strategy.drain_webull_direct_intents()
    bot._keepwatch_rest_hold.cancel_required(symbol)  # Hard reason exhausts grace.
    assert not bot._keepwatch_rest_hold.deferring(symbol)
    bot._keepwatch_rest_hold.clear_proven(symbol)
    assert not bot._keepwatch_rest_hold.deferring(symbol)
