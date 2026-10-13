"""Retained snapshot values with controlled delivery; no native trade/audit claims."""

from decimal import Decimal

import pytest

from project_mai_tai.events import MarketSnapshotPayload
from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from project_mai_tai.momentum_paper import engine as paper
from project_mai_tai.symbol_case import raw_symbol_is_uppercase
from tests.unit.test_tickcase1_live_inputs import NOW, ROWS, PAIRS
from tests.unit.test_momwarn1_gateway import aggregate
from tests.unit.test_momwarn1_gateway import event, service


def test_paper_delegates_only_case_predicate_to_shared_pure_helper(monkeypatch):
    seen = []
    def observe(symbol):
        seen.append(symbol)
        return raw_symbol_is_uppercase(symbol)
    monkeypatch.setattr(paper, 'raw_symbol_is_uppercase', observe)
    assert paper.symbol_is_eligible('TPC')
    assert not paper.symbol_is_eligible('TpC')
    assert not paper.symbol_is_eligible('BRK.B')
    assert not paper.symbol_is_eligible(' TPC ')
    assert not paper.symbol_is_eligible('ABCDEF')
    assert not paper.symbol_is_eligible(None)
    assert seen == ['TPC','TpC','BRK.B',' TPC ','ABCDEF',None]


@pytest.mark.parametrize('reverse', [False, True])
def test_native_common_prior_closes_survive_both_collision_orders(reverse):
    rows = list(ROWS.values())
    if reverse:
        rows.reverse()
    evaluator = paper.MomentumPaperEngine(
        prior_closes={row['symbol']:Decimal(row['previous_close']) for row in rows},
        condition_version='CONTROLLED-COMPOSITION', coverage_started_ms=int(NOW.timestamp()*1000)-60_000)
    assert evaluator.prior_closes == {'TPC':Decimal('83.49'),'BCPC':Decimal('168.19')}
    snapshots = [MarketSnapshotPayload.model_validate(row) for row in rows]
    records, capped = evaluator.detect_from_snapshots(snapshots, completed_at=NOW, max_symbols=16)
    assert not records and capped == 0
    assert not set(evaluator._snapshot_history) & {'TpC','BCpC'}
    assert evaluator.prior_closes == {'TPC':Decimal('83.49'),'BCPC':Decimal('168.19')}


@pytest.mark.parametrize('preferred,common', PAIRS)
def test_direct_A_native_snapshot_prices_never_bind_preferred_alias(preferred,common):
    # One-second A envelopes are controlled, not retained original A messages.
    def frame(symbol):
        price = float(ROWS[symbol]['last_trade_price'])
        return aggregate(sym=symbol,o=price,h=price,l=price,c=price)
    assert MassiveTradeStream._normalize_warning_aggregate(frame(preferred),common) is None
    accepted = MassiveTradeStream._normalize_warning_aggregate(frame(common),common)
    assert accepted.symbol == common
    assert accepted.close == float(ROWS[common]['last_trade_price'])


@pytest.mark.asyncio
@pytest.mark.parametrize('symbol', ['BRK.B','BRK.A','BF.B'])
async def test_lowercase_dot_class_is_dropped_without_removing_common_owner(symbol):
    svc, provider, _, _ = service()
    await svc.apply_subscription_event(event('v2',[symbol]))
    await svc.apply_subscription_event(event('v2',[symbol.lower()],'remove'))
    assert svc.active_symbols() == {symbol}
    assert provider._subscriptions == {symbol}
    assert set(provider._ws.added) == {f'T.{symbol}',f'Q.{symbol}'}
    assert not provider._ws.removed
