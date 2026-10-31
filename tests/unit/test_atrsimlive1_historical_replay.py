"""Controlled historical route/row/kernel obligations; author UNRUN."""
from copy import deepcopy
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from atrsimlive1_captured_rr import ClosedOriginalRR, SCOPE, SOURCE_HEAD, SealedRoutes, CapturedRefusal
from atrsimlive1_historical_replay import compare_known, known_rows, private_root


def witness():
    return ClosedOriginalRR('/CONTROLLED/never-read.gz', 'a' * 64, 1, 'b' * 64, 1,
        (0, 0, 1, 0, 0), 'CONTROLLED', '2026-10-29T03:36:15.030688+00:00',
        ('X',), 'c' * 64, 'd' * 64)


def pair():
    data = dict(symbols=['X'], snapshot_id='CONTROLLED', trading_day='2026-10-28',
                source_asof=witness().source_asof)
    fact = dict(symbol='X', segment='CONTROLLED', broker='webull', notional='300',
                selected_text_sha256='a' * 64)
    route = dict(symbol='X', status='RECORDED_DAY_ROUTE', broker='webull', notional='300',
                 facts=[fact], source={'artifact': '/CONTROLLED/log', 'sha256': 'b' * 64}, issues=[])
    return data, {'producer': dict(head=SOURCE_HEAD, observed_symbols=['X'], day_routes=[route])}


def test_sealed_parser_facts_preserved_without_fabricating_raw_line():
    data, original = pair()
    routes = SealedRoutes(witness(), data, original)
    expected = deepcopy(original['producer']['day_routes'][0])
    expected.pop('symbol')
    assert routes.resolve(data, 'X') == expected
    assert 'text' not in routes.resolve(data, 'X')['facts'][0]
    assert original['producer']['day_routes'][0]['symbol'] == 'X'


@pytest.mark.parametrize('fault', ['selected-hash', 'route-quantity', 'duplicate', 'foreign-snapshot'])
def test_sealed_fact_conflict_or_foreign_capture_refuses(fault):
    data, original = pair()
    route = original['producer']['day_routes'][0]
    if fault == 'selected-hash':
        route['facts'][0]['selected_text_sha256'] = None
    elif fault == 'route-quantity':
        route['notional'] = '600'
    elif fault == 'duplicate':
        original['producer']['day_routes'].append(deepcopy(route))
    else:
        data['snapshot_id'] = 'FOREIGN'
    with pytest.raises((CapturedRefusal, TypeError)):
        SealedRoutes(witness(), data, original)


def test_original_unknown_route_stays_unknown():
    data, original = pair()
    original['producer']['day_routes'] = [dict(symbol='X', status='UNKNOWN', broker=None,
        notional=None, issues=['UNKNOWN_ACTUAL_DAY_COUNTRY_ROUTE'])]
    assert SealedRoutes(witness(), data, original).resolve(data, 'X')['notional'] is None


def test_known_dollar_bytes_not_only_rounded_amount_are_compared():
    original = {'tables': {'FIRSTBAR': {'rows': [dict(outcome_id='ONE', identity={'symbol': 'X'},
        row=dict(pnl_label_known=True, pnl='1.0001', quantity='100'))]}}}
    assert len(compare_known(original, deepcopy(original))) == 1
    changed = deepcopy(original)
    changed['tables']['FIRSTBAR']['rows'][0]['row']['pnl'] = '1.0002'
    with pytest.raises(CapturedRefusal, match='dollar_row_bytes_changed'):
        compare_known(original, changed)


def test_unknown_duplicate_is_not_hidden_or_counted_zero():
    item = dict(outcome_id='ONE', row={'pnl_label_known': False, 'pnl': None})
    with pytest.raises(CapturedRefusal, match='duplicate_historical_outcome'):
        known_rows({'tables': {'FIRSTBAR': {'rows': [item, deepcopy(item)]}}})


def test_existing_output_root_is_never_overwritten(tmp_path):
    path = (tmp_path / 'output').resolve()
    path.mkdir(mode=0o700)
    with pytest.raises(FileExistsError):
        private_root(path, create=True)


@pytest.mark.parametrize('family', ['FIRSTBAR', 'CORE'])
def test_historical_connected_cold_and_warm_preserve_original_kernel_results(family):
    from tests.support.atr_same_sip_cases import replay, trade
    from project_mai_tai.atr_simulation import same_sip_types as t
    from project_mai_tai.atr_simulation import same_sip_firstbar_replay as fb
    from project_mai_tai.atr_simulation.read_model import result_rows
    from project_mai_tai.atr_simulation.same_sip import SAME_SIP_CONSERVATIVE_V1
    from atrsimlive1_historical_models import HistoricalModels, WORLDS, typed_input_fingerprint
    from atrsimlive1_incremental_cache import fingerprint

    raw = replay((trade(61, '2.03'), trade(62, '2.25'), trade(240, '2.25')))
    episode = t.EpisodeInput(**{field.name: getattr(raw.episode, field.name) for field in fields(raw.episode)},
                             same_sip_policy=SAME_SIP_CONSERVATIVE_V1)
    episode = replace(episode, today_entries=(t.EntryFact('CONTROLLED:entry', episode.trades[0].key,
        Decimal('2.03'), 100, episode.trades[0].key.source),))
    item = fb.FirstBarReplayInput(episode, raw.flip_bar, raw.entry_cutoff) if family == 'FIRSTBAR' else episode
    # Input codec obligation: preserve actual nested fields and scalar/type
    # distinctions; do not merely omit the input from the dependency key.
    baseline_key = typed_input_fingerprint((item,))
    changed_episode = replace(episode, quantity=101)
    changed_item = replace(item, episode=changed_episode) if family == 'FIRSTBAR' else changed_episode
    assert typed_input_fingerprint((changed_item,)) != baseline_key
    changed_episode = replace(episode, sizing_source=replace(episode.sizing_source, pointer='CHANGED'))
    changed_item = replace(item, episode=changed_episode) if family == 'FIRSTBAR' else changed_episode
    assert typed_input_fingerprint((changed_item,)) != baseline_key
    changed_trade = replace(episode.trades[0], size=101)
    changed_episode = replace(episode, trades=(changed_trade, *episode.trades[1:]))
    changed_item = replace(item, episode=changed_episode) if family == 'FIRSTBAR' else changed_episode
    assert typed_input_fingerprint((changed_item,)) != baseline_key
    assert typed_input_fingerprint((item,)) != typed_input_fingerprint([item])
    assert typed_input_fingerprint(Decimal('2.0300')) != typed_input_fingerprint('2.0300')
    assert typed_input_fingerprint(Decimal('2.0300')) != typed_input_fingerprint(Decimal('2.03'))
    assert typed_input_fingerprint(episode.sizing_source.kind) != typed_input_fingerprint(episode.sizing_source.kind.value)
    data = dict(symbols=['X'], source_asof=datetime(2026, 10, 29, tzinfo=timezone.utc), rows={})
    options = {'fill_policy': fb.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME} if family == 'FIRSTBAR' else {}
    pins = {'CONTROLLED:only': 'f' * 64}
    binding = dict(input_scope=SCOPE, source_seal=fingerprint(pins), policies={})
    run = SimpleNamespace(binding=binding, audit=[])
    cold = HistoricalModels(run, witness(), data, {})
    results, reports = cold.execute(family, data, (item,), ('CONTROLLED',), None, [], **options)
    document = dict(tables={world: {'rows': []} for world in WORLDS[family]}, producer={'engine_receipts': {}})
    for result in results:
        receipts = [asdict(result)] if family == 'FIRSTBAR' else result_rows(result)
        for receipt in receipts:
            world = receipt['mode'] if family == 'FIRSTBAR' else receipt['mode'] + ':' + receipt['model']
            if world not in document['tables']:
                continue
            outcome = world + ':CONTROLLED'
            known = receipt.get('gross_pnl') is not None and not receipt.get('issues')
            document['tables'][world]['rows'].append(dict(identity=asdict(result.identity), outcome_id=outcome,
                row=dict(pnl_label_known=known, pnl_pct_label_known=known)))
            document['producer']['engine_receipts'][outcome] = receipt
    cold.record(family, data, results, reports, document)
    assert any(cold.next[family]['candidates'].values())
    warmrun = SimpleNamespace(binding=binding, audit=[])
    warm = HistoricalModels(warmrun, witness(), data, cold.next)
    reused, rereports = warm.execute(family, data, (item,), ('CONTROLLED',), None, [], **options)
    assert reused == results and rereports == reports
    assert any(warmrun.audit[0]['reused'].values())
    # Explicit independent position obligation, beyond whole-output equality.
    known_receipts = [receipt for receipt in document['producer']['engine_receipts'].values()
                      if receipt.get('gross_pnl') is not None and not receipt.get('issues')]
    assert known_receipts
    assert all(sum(fill['quantity'] for fill in receipt['fills']) == 100
               and len(receipt['exits']) == 1 and receipt['exits'][0]['quantity'] == 100
               for receipt in known_receipts)
