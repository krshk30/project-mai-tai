"""SOURCE controlled change-proof tests; no SQL execution/model calls."""
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path

import pytest


LEFT = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)


def setup(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    module = importlib.import_module('atrsimlive1_change_proof')
    reads = importlib.import_module('atrsimlive1_incremental_reads')
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    def cert(asof, snapshot):
        return reads.certify(dict(row_count=2, content_sha256='a' * 64, source_timezone='UTC'),
            kind='trades', symbol='CONTROLLED', start=LEFT, end=LEFT + timedelta(minutes=2),
            day_start=LEFT, asof=asof, snapshot_id=snapshot,
            query_sha256=reads.query_pair('trades')['query_sha256'], read_only='on',
            isolation='repeatable read', complete=True)
    return module, cache, cert(LEFT + timedelta(minutes=3), 'OLD'), cert(LEFT + timedelta(minutes=4), 'NEW')


def test_change_discovery_OFF_has_no_new_read_or_permission(monkeypatch):
    module, _, old, current = setup(monkeypatch)
    monkeypatch.setattr(module, 'read_plan', lambda *a, **k: pytest.fail('OFF new proof work'))
    assert module.plan_window(old, current, current_asof=current['asof'],
        current_snapshot='NEW', open_window=True) == {'action': 'ORIGINAL_FULL_PREFIX_PROOF', 'discovery_enabled': False}


@pytest.mark.parametrize('kind,floor', [('trades', 'start'), ('quotes', 'day_start')])
def test_since_query_is_bound_candidate_only_with_quote_carry_scope(monkeypatch, kind, floor):
    module, _, _, _ = setup(monkeypatch)
    query = module.candidate_query(kind, symbol="CONTROLLED'quoted", previous_asof=LEFT,
        current_asof=LEFT + timedelta(minutes=5), start=LEFT + timedelta(minutes=1),
        end=LEFT + timedelta(minutes=4), day_start=LEFT)
    assert "CONTROLLED'quoted" not in query['sql']
    assert query['params']['symbol'] == "CONTROLLED'quoted"
    assert (f'event_ts >= %({floor})s' if kind == 'trades' else 'event_ts>=%(day_start)s') in query['sql']
    assert 'received_at > %(previous_asof)s' in query['sql']
    assert query['role'] == 'DISCOVERY_ONLY_NOT_UNCHANGED_OR_COMPLETE_PROOF'


@pytest.mark.parametrize('fault', ['corrected', 'deleted', 'late-backdated'])
def test_empty_since_suffix_cannot_hide_changed_full_prefix(monkeypatch, fault):
    module, _, old, current = setup(monkeypatch)
    if fault == 'deleted': current['row_count'] = 1
    else: current['content_sha256'] = 'b' * 64
    result = module.plan_window(old, current, current_asof=current['asof'],
        current_snapshot='NEW', open_window=True, enabled=True, discovery_row_count=0)
    assert result['action'] == 'FRESH_WINDOW'
    assert result['absence_from_suffix_is_not_proof'] is True


def test_actual_exact_current_prefix_allows_original_delta_not_false_complete(monkeypatch):
    module, _, old, current = setup(monkeypatch)
    result = module.plan_window(old, current, current_asof=current['asof'],
        current_snapshot='NEW', open_window=True, enabled=True, discovery_row_count=0)
    assert result['action'] == 'DELTA'
    assert result['start'] == old['end']
    assert result['end'] == current['asof']


def test_stale_RR_discovery_still_refuses(monkeypatch):
    module, cache, old, current = setup(monkeypatch)
    current['snapshot_id'] = 'FOREIGN'
    with pytest.raises(cache.CacheRefusal, match='current_snapshot_fingerprint_required'):
        module.plan_window(old, current, current_asof=current['asof'],
            current_snapshot='NEW', open_window=False, enabled=True, discovery_row_count=0)
