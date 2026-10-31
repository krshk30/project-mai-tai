"""Actual CLI/context/writer boundary with CONTROLLED collector; author UNRUN.

No SQL, engine, provider, owned child, or fabricated successful persistence.
"""
from datetime import datetime, timezone
from io import BytesIO
import importlib
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest


def test_actual_child_CLI_partial_keeps_previous_complete_cache_bytes(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / 'tools'))
    cli = importlib.import_module('atrsimlive1')
    cache = importlib.import_module('atrsimlive1_incremental_cache')
    namespace = importlib.import_module('atrsimlive1_namespace')
    collect = importlib.import_module('atrsimlive1_collect')
    models = importlib.import_module('atrsimlive1_models')
    null = importlib.import_module('atrsimlive1_null_policy')
    from atrsimlive1_window_collect import POLICY_SHA
    from project_mai_tai.atr_simulation.same_sip_binding import verify_same_sip_files

    producer = dict(head='a' * 40, source_pins={'CONTROLLED': 'b' * 64})
    parent = (tmp_path / 'cache-parent').resolve()
    parent.mkdir(mode=0o700)
    run_dir = (tmp_path / 'evidence').resolve()
    run_dir.mkdir(mode=0o700)
    day = datetime.now(timezone.utc).astimezone(ZoneInfo('America/New_York')).date()
    binding = dict(version=cache.INCREMENTAL_V1, input_scope='CURRENT_RR', trading_day=day.isoformat(),
        source_seal=cache.fingerprint(producer['source_pins']), policies=dict(execution_rules_sha256=POLICY_SHA,
            null_policy=null.NULL_REGULAR_V1, same_sip_policy=verify_same_sip_files()))
    directory = namespace.namespace_root(parent, binding)
    store = cache.CacheStore(directory, binding)
    store.save(dict(windows={'ELPW': {}, 'PENDING': {}},
        worlds={'CONTROLLED:whole': {'original_complete_model': 'MUST REMAIN BYTE IDENTICAL'}}))
    before = (directory / 'cache.json').read_bytes()
    observed = []
    def controlled_collect(**kwargs):
        run, partial = kwargs['incremental'], kwargs['deadline_partial']
        asof = datetime.now(timezone.utc)
        run.bind_snapshot(dict(snapshot_id='CONTROLLED_RR', read_only='on', isolation='repeatable read'),
            dict(day=day, start=asof, end=asof, asof=asof), ('ELPW', 'PENDING'))
        # Reproduce the real failure premise: ALL windows materialized while
        # no model completed before the soft deadline. The old persist guard
        # alone would accept these windows and overwrite the complete cache.
        run.next['windows'] = {'ELPW': {}, 'PENDING': {}}
        partial.bind_roster(day=day.isoformat(), source_asof=asof, snapshot_id='CONTROLLED_RR', roster=run.symbols)
        assert partial.stop_before_next_symbol() is True
        observed.append(run)
        return []
    monkeypatch.setattr(cli, 'source_binding', lambda _: producer)
    monkeypatch.setattr(cli, 'read_today_routes', lambda: (None, 'CONTROLLED'))
    monkeypatch.setattr(collect, 'dsn_from_environment', lambda _: 'CONTROLLED_NOT_A_CONNECTION')
    monkeypatch.setattr(collect, 'collect', controlled_collect)
    monkeypatch.setattr(models, 'produce', lambda *a, **k: pytest.fail('pending model ran'))
    output = BytesIO()
    monkeypatch.setattr(cli.sys, 'stdout', SimpleNamespace(buffer=output))
    status = cli.main(['--child', '--source-manifest', str(tmp_path / 'CONTROLLED-source'),
        '--dsn-env', 'DATABASE_URL', '--policy', 'CONTROLLED-policy-not-read',
        '--stored-null-policy', null.NULL_REGULAR_V1,
        '--incremental-version', cache.INCREMENTAL_V1, '--incremental-cache-root', str(parent),
        '--evidence-run-dir', str(run_dir), '--deadline-partial-seconds', '0.000000001'])
    assert status == 0  # Child draft only; parent partial mode still exits2.
    assert len(observed) == 1
    assert observed[0].cache_write_receipt == dict(status='NOT_WRITTEN_INCOMPLETE_MODEL_ROSTER',
        original_symbols=2, completed_symbols=0, pending_symbols=2, previous_cache_preserved=True)
    assert (directory / 'cache.json').read_bytes() == before
    assert not (directory / '.next.json').exists()
    draft = importlib.import_module('atrsimlive1_runtime').decode(output.getvalue())
    assert draft['completed_symbols'] == [] and draft['pending_symbols'] == ['ELPW', 'PENDING']
