"""Native prices from a retained batch; deliveries/clocks/envelopes are controlled."""
import ast
from dataclasses import replace
from datetime import datetime
import json
from pathlib import Path
import subprocess
from tests.c7_history import historical_source
from types import SimpleNamespace

import pytest

from project_mai_tai.events import MarketSnapshotPayload, StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.market_data.massive_provider import MassiveTradeStream
from project_mai_tai.market_data import massive_provider
from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient, Quote, ChartBar
from project_mai_tai.market_data.schwab_v2_streamer import SchwabV2Streamer
from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.services import scanner_confirmed_capture, strategy_engine_app
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import five_pillars, snapshot_utils, top_gainers
from project_mai_tai.strategy_core.config import MomentumAlertConfig, MomentumConfirmedConfig
from project_mai_tai.strategy_core.models import ReferenceData
from project_mai_tai.strategy_core.momentum_alerts import MomentumAlertEngine
from project_mai_tai.strategy_core.momentum_confirmed import MomentumConfirmedScanner
from project_mai_tai.strategy_core.runner import RunnerStrategyRuntime
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy
from project_mai_tai.symbol_case import raw_symbol_is_uppercase
from tests.unit.test_scanner_confirmed_capture import _FakeSessionFactory
from tests.unit.test_momwarn1_gateway import event, service
from tests.unit.test_scanner_cycle_history_restore import build_test_session_factory, FakeRedis
from project_mai_tai.db.models import DashboardSnapshot
from tests.c7_retained import retained_bytes, retained_manifest

CASE = json.loads((Path(__file__).parents[1]/'fixtures/tickcase1_native_pairs.json').read_text())
ROWS = {row['symbol']: row for row in CASE['pair_snapshots']}
NOW = datetime.fromisoformat(CASE['produced_at'].replace('Z','+00:00'))
PAIRS = [('TpC','TPC'), ('BCpC','BCPC')]


def snapshot(symbol):
    return strategy_engine_app.snapshot_from_payload(MarketSnapshotPayload.model_validate(ROWS[symbol]))


def frozen_namespace(path, module):
    source = subprocess.check_output(['git','show','ec4dc414:'+path],text=True)
    namespace = dict(vars(module))
    exec(compile(source, '<frozen-ec4-controlled-comparison>', 'exec'), namespace)
    return namespace


@pytest.mark.parametrize('preferred,common', PAIRS)
@pytest.mark.parametrize('preferred_last', [True,False])
def test_native_snapshot_quotes_cannot_merge_or_overwrite_common(preferred, common, preferred_last):
    runner = RunnerStrategyRuntime(definition_code='runner', account_name='CONTROLLED',
        default_quantity=1, now_provider=lambda: NOW)
    rows = [snapshot(common),snapshot(preferred)]
    if not preferred_last:
        rows.reverse()
    runner.update_market_snapshots(rows)
    assert runner._latest_quotes == {common:{'bid':float(ROWS[common]['bid_price']),
                                             'ask':float(ROWS[common]['ask_price'])}}
    runner.set_watchlist([preferred,common,'BRK.B'])
    assert runner.watchlist == {common,'BRK.B'}
    runner.update_candidates([{'ticker':preferred},{'ticker':common}])
    assert set(runner._candidates) == {common}


def test_retained13269_rawcase_filter_and_scanner_uppercase_content_parity(monkeypatch):
    raw = retained_bytes('snapshot-market-payload.json')
    assert retained_manifest()['artifacts']['snapshot-market-payload.json']['original_sha256'] == CASE['source_sha256']
    payload = json.loads(raw)['event']['payload']
    rows = payload['snapshots']
    kept = [row for row in rows if raw_symbol_is_uppercase(row['symbol'])]
    dropped = [row for row in rows if not raw_symbol_is_uppercase(row['symbol'])]
    assert len(rows) == 13269 and len(kept) == 12870 and len(dropped) == 399
    assert kept == [row for row in rows if row['symbol'] == row['symbol'].upper()]
    assert {row['symbol'] for row in dropped} >= {'TpC','BCpC'}
    snapshots = [strategy_engine_app.snapshot_from_payload(MarketSnapshotPayload.model_validate(row)) for row in rows]
    uppercase = [s for s in snapshots if raw_symbol_is_uppercase(s.ticker)]
    refs = {row['symbol']:ReferenceData(row['shares_outstanding'],float(row['avg_daily_volume']))
            for row in payload['reference_data'] if raw_symbol_is_uppercase(row['symbol'])}
    old = frozen_namespace('src/project_mai_tai/strategy_core/five_pillars.py', five_pillars)
    config = five_pillars.FivePillarsConfig()
    expected = old['apply_five_pillars'](uppercase,refs,config,now=NOW)
    assert five_pillars.apply_five_pillars(snapshots,refs,config,now=NOW) == expected
    assert five_pillars.apply_five_pillars(uppercase,refs,config,now=NOW) == expected
    monkeypatch.setattr(top_gainers,'now_eastern',lambda: NOW)
    monkeypatch.setattr(snapshot_utils, 'time', SimpleNamespace(time=lambda: NOW.timestamp()))
    old = frozen_namespace('src/project_mai_tai/strategy_core/top_gainers.py', top_gainers)
    old['now_eastern'] = lambda: NOW
    expected = old['TopGainersTracker']().update(uppercase,refs,now=NOW)
    assert top_gainers.TopGainersTracker().update(snapshots,refs,now=NOW) == expected
    assert top_gainers.TopGainersTracker().update(uppercase,refs,now=NOW) == expected


@pytest.mark.parametrize('preferred,common', PAIRS)
def test_momentum_history_and_restore_never_alias_native_pair(preferred,common):
    engine = MomentumAlertEngine(MomentumAlertConfig(min_price=0,max_price=1000), now_provider=lambda:NOW)
    engine.record_snapshot([snapshot(preferred),snapshot(common)])
    assert set(engine._history[-1]) == {common}
    values = {preferred:[float(ROWS[preferred]['last_trade_price']),100],
              common:[float(ROWS[common]['last_trade_price']),200]}
    assert engine.restore_state({'history':[values],
        'cooldowns':[{'ticker':preferred,'alert_type':'SQ','last_fired_at':NOW.isoformat()},
                     {'ticker':common,'alert_type':'SQ','last_fired_at':NOW.isoformat()}],
        'last_spike_volume':{preferred:100,common:200},'volume_spike_tickers':[preferred,common],
        'recent_rejections':[{'ticker':preferred},{'ticker':common}]})
    assert engine._history[-1] == {common:tuple(values[common])}
    assert set(engine._last_spike_volume) == {common}
    assert engine._volume_spike_tickers == {common}
    assert {ticker for ticker,_ in engine._cooldowns} == {common}
    assert {row['ticker'] for row in engine._recent_rejections} == {common}
    engine.prefill_history([values])
    assert set(engine._history[-1]) == {common}
    scanner = MomentumConfirmedScanner(MomentumConfirmedConfig())
    scanner.seed_confirmed_candidates([{'ticker':preferred},{'ticker':common}])
    assert [row['ticker'] for row in scanner.get_all_confirmed()] == [common]


@pytest.mark.parametrize('kind',['T','Q'])
@pytest.mark.parametrize('preferred,common',PAIRS)
@pytest.mark.parametrize('session_hour_utc', [11, 15])
def test_watched_TQ_native_values_drop_mixed_and_keep_common(kind,preferred,common,session_hour_utc,monkeypatch):
    # PM/RTH delivery clocks are controlled; native values are not historical prints.
    clock = NOW.replace(hour=session_hour_utc)
    monkeypatch.setattr(massive_provider, 'datetime', SimpleNamespace(now=lambda _:clock))
    stream = MassiveTradeStream('CONTROLLED-NO-CONNECTION')
    stream._subscriptions = {common,'BRK.B'}
    received=[]
    stream._on_trade = stream._on_quote = received.append
    events=[]
    for symbol in [common,preferred,common]:
        row=ROWS[symbol]
        events.append(SimpleNamespace(ev=kind,sym=symbol,price=float(row['last_trade_price']),
            size=100,sip_timestamp=row['last_trade_timestamp_ns'],conditions=[],
            bid_price=float(row['bid_price']),ask_price=float(row['ask_price'])))
    stream._handle_messages(events)
    assert [row.symbol for row in received] == [common,common]
    if kind=='Q':
        assert all(row.bid_price==float(ROWS[common]['bid_price']) for row in received)
    else:
        assert all(row.price==float(ROWS[common]['last_trade_price']) for row in received)
    stream._handle_messages([SimpleNamespace(ev=kind,sym='BRK.B',price=100.0,size=1,
        bid_price=99.0,ask_price=101.0)])
    assert received[-1].symbol=='BRK.B'


@pytest.mark.parametrize('preferred,common',PAIRS)
def test_v2_core_quote_no_mixed_state_or_common_quote_overwrite(preferred,common):
    strategy=SchwabV2Strategy(Settings(_env_file=None))
    row=ROWS[common]
    stamp=NOW.timestamp()*1000
    q=Quote(common,float(row['bid_price']),float(row['ask_price']),float(row['last_trade_price']),int(stamp),0)
    strategy.on_quote(common,q)
    assert strategy.watchlist_state(common).last_quote is q
    bad=replace(q,symbol=preferred,bid_price=float(ROWS[preferred]['bid_price']))
    assert strategy.on_quote(preferred,bad) is None
    assert strategy.on_quote(common,bad) is None
    assert preferred not in strategy._symbol_states
    assert strategy.watchlist_state(common).last_quote is q
    bar=ChartBar(preferred,1,1,1,1,1,int(stamp))
    assert strategy.on_bar(common,bar) is None


@pytest.mark.parametrize('preferred,common',PAIRS)
def test_schwab_quote_chart_and_timesale_parsers_check_raw_case(preferred,common):
    streamer=SchwabV2Streamer
    def content(symbol):
        row=ROWS[symbol]
        return {'key':symbol,'1':float(row['bid_price']),'2':float(row['ask_price']),
            '3':float(row['last_trade_price']),'35':int(NOW.timestamp()*1000)}
    assert streamer._extract_level_one_ticks(content(preferred),int(NOW.timestamp()*1000)) == []
    ticks=streamer._extract_level_one_ticks(content(common),int(NOW.timestamp()*1000))
    assert ticks and all(t.symbol==common for t in ticks)
    assert streamer._extract_timesale_tick({'key':preferred,'2':22.46},int(NOW.timestamp()*1000)) is None
    assert streamer._extract_chart_bar({'key':preferred}) is None
    client=object.__new__(SchwabV2RestClient)
    client.settings=SimpleNamespace(schwab_base_url='https://CONTROLLED.INVALID')
    client._authorized_get=lambda _url:{symbol:{'quote':{
        'bidPrice':row['bid_price'],'askPrice':row['ask_price'],'lastPrice':row['last_trade_price']}}
        for symbol,row in ROWS.items()}
    quotes=client._fetch_quotes([common])
    assert {q.symbol for q in quotes}=={'TPC','BCPC'}
    assert next(q for q in quotes if q.symbol==common).bid_price==float(ROWS[common]['bid_price'])


def test_capture_mixed_confirm_fade_retention_are_dropped_before_symbol_lookup():
    factory=_FakeSessionFactory()
    scanner_confirmed_capture.capture_events(factory,trade_date=NOW.date(),now=NOW,
        all_confirmed=[{'ticker':key,'price':row['last_trade_price']} for key,row in ROWS.items()],
        faded_symbols=list(ROWS),dropped_retention_symbols=list(ROWS))
    inserts=[row for row in factory.executed_params if row and 'event_type' in row]
    assert len(inserts)==6
    assert {row['symbol'] for row in inserts}=={'TPC','BCPC'}
    assert {row['event_type'] for row in inserts}=={'CONFIRM','FADE','RETENTION_DROP'}


def test_v2_scanner_state_extract_keeps_common_and_dot_class_only():
    event=StrategyStateSnapshotEvent(source_service='CONTROLLED', payload=StrategyStateSnapshotPayload(
        top_confirmed=[{'ticker':'TpC'},{'ticker':'TPC'}],
        all_confirmed=[{'ticker':'BCpC'},{'ticker':'BCPC'}],watchlist=['BRK.B','brk.b']))
    assert SchwabV2BotService._extract_confirmed_symbols(event)==['TPC','BCPC','BRK.B']


@pytest.mark.parametrize('event_type',['trade_tick','quote_tick','live_bar','historical_bars'])
@pytest.mark.asyncio
async def test_strategy_redis_input_drops_mixed_before_parsing_or_state(event_type):
    service=object.__new__(strategy_engine_app.StrategyEngineService)
    await service._handle_stream_message('CONTROLLED', {'data':json.dumps({
        'event_type':event_type,'payload':{'symbol':'TpC'}})})
    assert service.__dict__=={}


@pytest.mark.parametrize('head', ['1781e549', 'd2769d2'])
def test_shared_case_helper_matches_stable_T_without_broadening_paper_eligibility(head):
    source = historical_source(head, 'src/project_mai_tai/momentum_paper/engine.py')
    function = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.FunctionDef) and node.name == 'symbol_is_eligible')
    namespace = {}
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<stable-T-eligibility>', 'exec'), namespace)
    eligible = namespace['symbol_is_eligible']
    for symbol, expected in (('TpC', False), ('TPC', True), ('BCpC', False), ('BCPC', True),
                             ('', False), ('BRK.B', False), (' TPC ', False), ('ABCDEF', False), (None, False)):
        assert eligible(symbol) is expected
    assert raw_symbol_is_uppercase('BRK.B') and not eligible('BRK.B')


def test_handoff_and_restored_scanner_rows_do_not_preupper_mixed_names():
    engine = strategy_engine_app.StrategyEngineState
    rows = [{'ticker':'TpC'}, {'ticker':'TPC'}, {'ticker':'BCpC'}, {'ticker':'BCPC'}, 'BRK.B']
    assert engine._normalize_symbol_items(rows) == ['TPC', 'BCPC', 'BRK.B']
    state = object.__new__(engine)
    state.global_manual_stop_symbols = set()
    state._seed_bot_handoff_state = lambda rows: None
    state._resync_bot_watchlists_from_current_confirmed = lambda: None
    state.restore_confirmed_runtime_view(rows[:-1], all_confirmed=rows[:-1])
    assert [row['ticker'] for row in state.current_confirmed] == ['TPC', 'BCPC']
    assert [row['ticker'] for row in state.all_confirmed] == ['TPC', 'BCPC']
    state._manual_stop_symbols_for_bot = lambda code: set()
    state._broker_blocked_symbols_for_bot = lambda code: set()
    assert state._watchlist_for_bot('controlled', rows) == ['TPC', 'BCPC', 'BRK.B']


def test_v2_reconfirm_identity_cannot_come_from_preferred_row():
    bot = object.__new__(SchwabV2BotService)
    bot._line_restoration_enabled = True
    bot._line_scanner_symbols = {'TPC'}
    bot._line_confirmed_identity = {'TPC':'common-confirmation'}
    state = StrategyStateSnapshotEvent(source_service='CONTROLLED', payload=StrategyStateSnapshotPayload(
        top_confirmed=[{'ticker':'TpC', 'confirmed_at':'preferred-confirmation'},
                       {'ticker':'TPC', 'confirmed_at':'common-confirmation'}]))
    assert bot._line_scanner_transition(state, {'TPC'}) == set()
    assert bot._line_confirmed_identity == {'TPC':'common-confirmation'}


@pytest.mark.asyncio
async def test_gateway_owner_and_provider_symbols_check_case_before_upper():
    svc, stream, _, _ = service()
    await svc.apply_subscription_event(event('v2', ['TpC','TPC','BCpC','BCPC','BRK.B']))
    assert svc.active_symbols() == {'TPC','BCPC','BRK.B'}
    assert stream._subscriptions == {'TPC','BCPC','BRK.B'}
    assert not any(channel in stream._ws.added for channel in ('T.TpC','Q.TpC','T.BCpC','Q.BCpC'))
    await svc.apply_subscription_event(event('v2', ['TpC'], 'remove'))
    assert svc.active_symbols() == {'TPC','BCPC','BRK.B'}


@pytest.mark.asyncio
async def test_gateway_durable_owner_restore_filters_raw_case_before_aliasing():
    svc, stream, redis, _ = service()
    redis.hashes[svc._subscription_state_key] = {
        '_migration_complete':'1', '_last_applied_id':'0-0',
        'v2':json.dumps(['TpC','TPC','BCpC','BCPC','BRK.B']),
    }
    await svc._restore_subscription_state()
    assert svc.active_symbols() == {'TPC','BCPC','BRK.B'}
    assert svc._desired_symbols_by_consumer['v2'] == {'TPC','BCPC','BRK.B'}


@pytest.mark.parametrize('preferred,common', PAIRS)
def test_strategy_Schwab_stream_raw_parsers_keep_native_common_values(preferred,common):
    def level(symbol):
        row = ROWS[symbol]
        return {'key':symbol, '1':row['bid_price'], '2':row['ask_price'],
                '3':row['last_trade_price'], '9':100, '35':int(NOW.timestamp()*1000)}
    client = SchwabStreamerClient
    assert client._extract_quote_record(level(preferred)) is None
    assert client._extract_trade_record(level(preferred)) is None
    assert client._extract_timesale_trade_record({'key':preferred,'2':ROWS[preferred]['last_trade_price']}) is None
    assert client._extract_chart_bar_record({'key':preferred}) is None
    quotes, trades, bars = client._extract_records({'data':[
        {'service':'TIMESALE_EQUITY','content':[{'key':preferred,'2':ROWS[preferred]['last_trade_price']}]},
        {'service':'LEVELONE_EQUITIES','content':[level(preferred),level(common)]},
    ]})
    assert not bars
    assert len(quotes) == len(trades) == 1
    assert quotes[0].symbol == trades[0].symbol == common
    assert quotes[0].bid_price == float(ROWS[common]['bid_price'])
    assert trades[0].price == float(ROWS[common]['last_trade_price'])
    assert client._extract_quote_record({'key':'BRK.B','1':99,'2':101}).symbol == 'BRK.B'


@pytest.mark.parametrize('watchlist_present', [True, False])
def test_actual_scanner_checkpoint_restore_drops_raw_markers_before_view(monkeypatch, watchlist_present):
    # Private controlled SQLite checkpoint, not a production DB or old-row repair.
    monkeypatch.setattr(strategy_engine_app, 'utcnow', lambda: NOW)
    session_start = strategy_engine_app.current_scanner_session_start_utc(NOW)
    symbols = ['TpC','TPC','BCpC','BCPC','BRK.B']
    factory = build_test_session_factory()
    with factory() as session:
        session.add(DashboardSnapshot(snapshot_type='scanner_cycle_history', payload={
            'persisted_at':NOW.isoformat(), 'scanner_session_start_utc':session_start.isoformat(),
            'session_handoff_active':True, 'watchlist':symbols if watchlist_present else [],
            'bot_handoff_symbols_by_strategy':{'macd_30s':symbols},
            'bot_handoff_history_by_strategy':{'macd_30s':symbols},
        }))
        session.commit()
    svc = strategy_engine_app.StrategyEngineService(settings=Settings(_env_file=None,
        dashboard_snapshot_persistence_enabled=True, scanner_feed_retention_enabled=False),
        redis_client=FakeRedis(), session_factory=factory)
    svc._restore_watchlist_from_scanner_cycle_history()
    assert {row['ticker'] for row in svc.state.current_confirmed} == {'TPC','BCPC','BRK.B'}
    assert svc.state.bot_handoff_symbols_by_strategy['macd_30s'] == {'TPC','BCPC','BRK.B'}
