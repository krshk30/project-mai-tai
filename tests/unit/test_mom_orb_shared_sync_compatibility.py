"""CONTROLLED shared-OMS read impact, not original market/native acceptance."""
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.brokerdecides1 import NativeAccountBook, decode
from project_mai_tai.brokerdecides1_sync import account_names, channel, local_evidence
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy, TradeIntent, VirtualPosition
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.runtime_seed import seed_runtime_metadata
from project_mai_tai.settings import Settings
from tests.unit.test_oms_risk_service import FakeRedis
from tests.unit.mom_orb_sync_fixture import await_native_publication, owned_database, owned_service


@pytest.fixture
def memory_session_factory(tmp_path):
    # Retain the existing fixture API/selectors, but no shared in-memory connection.
    # Main must claim ONLY this fresh private SQLite basename in its file fence.
    with owned_database(tmp_path) as factory:
        yield factory


class ControlledExternalBooks:
    """Explicit non-flat/current external read fixtures; no decision-port mocks."""
    def __init__(self, mode):
        self.mode, self.native_reads, self.submits = mode, [], []

    async def list_account_positions(self, account):
        return [BrokerPositionSnapshot(account, 'CONTROL', Decimal(10), Decimal(2),
                                       as_of=datetime.now(UTC))]

    async def fetch_order_update(self, request):
        return None  # No new report; not terminal/flat evidence.

    async def fetch_oco_exit_fill(self, *args, **kwargs):
        return None  # UNKNOWN exit read must not create a SELL Fill.

    async def submit_order(self, request):
        self.submits.append(request)
        raise AssertionError('read-only compatibility fixture must never dispatch')

    async def read_native_entry_book(self, account):
        self.native_reads.append(account)
        if self.mode == 'exception':
            raise RuntimeError('CONTROLLED_READ_FAILURE_NOT_NATIVE_FLAT')
        at = int(datetime.now(UTC).timestamp() * 1000)
        return NativeAccountBook(account, 'webull' if account == 'live:orb' else 'schwab',
            at, at, frozenset(), frozenset({'CONTROL'}), self.mode == 'complete_nonflat',
            'CONTROLLED_NONFLAT' if self.mode == 'complete_nonflat' else 'CONTROLLED_INCOMPLETE')


def economic_rows(factory):
    with factory() as session:
        orders = [(str(row.id), row.client_order_id, row.symbol, row.side, str(row.quantity),
                   row.status, row.payload, str(row.intent_id), str(row.broker_account_id))
                  for row in session.scalars(select(BrokerOrder).order_by(BrokerOrder.id))]
        fills = [(str(row.id), str(row.order_id), row.symbol, row.side, str(row.quantity),
                  str(row.price), row.filled_at, row.broker_fill_id)
                 for row in session.scalars(select(Fill).order_by(Fill.id))]
        owned = [(str(row.strategy_id), str(row.broker_account_id), row.symbol,
                  str(row.quantity), str(row.average_price), str(row.realized_pnl))
                 for row in session.scalars(select(VirtualPosition).order_by(VirtualPosition.id))]
    return orders, fills, owned


@pytest.mark.asyncio(loop_scope='function')
@pytest.mark.parametrize('strategy_code', ['momentum_warn_live', 'orb_schwab'])
@pytest.mark.parametrize('provider', ['schwab', 'webull'])
@pytest.mark.parametrize('mode', ['complete_nonflat', 'incomplete', 'exception'])
async def test_actual_full_sync_l12_reads_do_not_rewrite_mom_orb_economics(
        strategy_code, provider, mode, memory_session_factory):
    factory = memory_session_factory
    settings = Settings(_env_file=None, oms_adapter='paper', redis_stream_prefix='CONTROLLED_MOM_ORB',
        strategy_schwab_1m_v2_enabled=True, orb_enabled=True,
        strategy_schwab_1m_v2_account_name='live:schwab_1m_v2',
        strategy_schwab_1m_v2_broker_provider='schwab',
        strategy_schwab_1m_v2_webull_account_name='live:orb',
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        strategy_schwab_1m_v2_brokerdecides1_enabled=True,
        orb_live_schwab_orders_enabled=True)
    redis, broker = FakeRedis(), ControlledExternalBooks(mode)
    service = OmsRiskService(settings, redis, session_factory=factory, broker_adapter=broker)
    async with owned_service(factory, service):
        service.seed_runtime_metadata()
        account_name = 'live:orb' if provider == 'webull' else 'live:schwab_1m_v2'
        with factory.begin() as session:
            # MOM's real persistence.claim registers this owner through ensure_strategy;
            # here it is explicit fixture data, not an actor claim or admission proof.
            if strategy_code == 'momentum_warn_live':
                service.store.ensure_strategy(session, strategy_code, name='Momentum LIVE',
                    execution_mode='live', metadata_json={'account_name': account_name})
            strategy = session.scalar(select(Strategy).where(Strategy.code == strategy_code))
            account = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account_name))
            assert strategy is not None and account is not None and account.provider == provider
            intent = TradeIntent(strategy_id=strategy.id, broker_account_id=account.id,
                symbol='CONTROL', side='buy', quantity=Decimal(10), intent_type='open',
                reason='CONTROLLED_EXISTING_FILL', status='filled', payload={})
            session.add(intent)
            session.flush()
            order = BrokerOrder(intent_id=intent.id, strategy_id=strategy.id, broker_account_id=account.id,
                client_order_id='controlled-' + strategy_code + '-' + provider, broker_order_id='controlled-native',
                symbol='CONTROL', side='buy', quantity=Decimal(10), status='filled',
                order_type='limit', time_in_force='day', payload={'limit_price': '2.00'},
                submitted_at=datetime.now(UTC))
            session.add(order)
            session.flush()
            session.add(Fill(order_id=order.id, strategy_id=strategy.id, broker_account_id=account.id,
                symbol='CONTROL', side='buy', quantity=Decimal(10), price=Decimal(2),
                filled_at=datetime.now(UTC), broker_fill_id='CONTROLLED_EXISTING_RECEIPT', payload={}))
            session.add(VirtualPosition(strategy_id=strategy.id, broker_account_id=account.id,
                symbol='CONTROL', quantity=Decimal(10), average_price=Decimal(2), realized_pnl=Decimal(0)))
        before = economic_rows(factory)
        # Real sync returns before native refresh by design. Observe actual completion
        # before snapshot/economic readback; no production sync await is introduced.
        result = await service.sync_broker_state()
        await await_native_publication(service)
        after = economic_rows(factory)
        snapshot = decode(redis.values[channel(settings)])
        assert result['accounts'] >= 2
        assert broker.native_reads == ['live:schwab_1m_v2', 'live:orb']
        assert after == before and len(after[1]) == 1 and broker.submits == []
        if mode == 'complete_nonflat':
            assert snapshot.local_complete and len(snapshot.books) == 2
            assert all(book.positions == frozenset({'CONTROL'}) for book in snapshot.books)
        else:
            assert not snapshot.local_complete


@pytest.mark.parametrize('defect', [
    'webull_registration_disabled', 'configured_name_absent', 'duplicate_configured_name',
])
def test_registered_identity_refuses_incomplete_configured_scope(memory_session_factory, defect):
    settings = Settings(_env_file=None, strategy_schwab_1m_v2_enabled=True,
        strategy_schwab_1m_v2_account_name='live:schwab_1m_v2',
        strategy_schwab_1m_v2_broker_provider='schwab',
        strategy_schwab_1m_v2_webull_account_name='live:orb',
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=defect != 'webull_registration_disabled',
        strategy_schwab_1m_v2_country_route_enabled=False, orb_webull_account_name='')
    seed_runtime_metadata(settings, session_factory=memory_session_factory)
    names = account_names(settings)
    with memory_session_factory() as session:
        registered = {row.name: row.provider for row in session.scalars(select(BrokerAccount))}
        assert registered['live:schwab_1m_v2'] == 'schwab'
        if defect == 'webull_registration_disabled':
            assert 'live:orb' not in registered
        else:
            assert registered['live:orb'] == 'webull'
            names = ((names[0], 'live:CONTROLLED_MISSING') if defect == 'configured_name_absent'
                     else (names[0], names[0]))
        # Real local census must refuse; native-looking inputs cannot create the
        # missing durable account identity. No predicate or publisher is mocked.
        with pytest.raises(ValueError, match='^local_account_identity_UNKNOWN$'):
            local_evidence(session, names)
