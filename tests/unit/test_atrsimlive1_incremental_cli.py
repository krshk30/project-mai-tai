"""Explicit private-cache junction; controlled seams, no data/model qualification."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from io import BytesIO, StringIO
import json
from types import SimpleNamespace

import pytest

from tests.unit.test_atrsimlive1_output_split import original, directory


def base_args():
    return ['--source-manifest', '/CONTROLLED/source.json', '--dsn-env', 'DATABASE_URL']


@pytest.mark.parametrize('fault', ['only_version', 'only_root', 'unknown_version',
    'no_policy', 'no_null', 'no_evidence', 'relative_root'])
def test_bad_incremental_binding_refuses_before_source_or_any_inputs(monkeypatch, fault):
    import atrsimlive1 as cli
    from atrsimlive1_null_policy import NULL_REGULAR_V1
    monkeypatch.setattr(cli, 'source_binding', lambda path: pytest.fail('must refuse before source'))
    values = {'--incremental-version': cli.INCREMENTAL_VERSION,
        '--incremental-cache-root': '/CONTROLLED/cache', '--policy': '/CONTROLLED/policy.json',
        '--stored-null-policy': NULL_REGULAR_V1, '--evidence-root': '/CONTROLLED/evidence'}
    if fault == 'only_version': values.pop('--incremental-cache-root')
    elif fault == 'only_root': values.pop('--incremental-version')
    elif fault == 'unknown_version': values['--incremental-version'] = 'TRUTHY'
    elif fault == 'no_policy': values.pop('--policy')
    elif fault == 'no_null': values.pop('--stored-null-policy')
    elif fault == 'no_evidence': values.pop('--evidence-root')
    else: values['--incremental-cache-root'] = 'relative/cache'
    with pytest.raises(SystemExit) as error:
        cli.main(base_args() + [part for pair in values.items() for part in pair])
    assert error.value.code == 2


def test_default_context_does_not_import_or_open_cache():
    import atrsimlive1 as cli
    # Other fields are deliberately absent. Default must not inspect them.
    with cli.incremental_context(SimpleNamespace(incremental_version=None), {}, None) as value:
        assert value is None


def test_parent_forwards_exact_version_root_and_preserves_physical240_budget(monkeypatch, tmp_path):
    import atrsimlive1 as cli
    from atrsimlive1_null_policy import NULL_REGULAR_V1
    calls = []
    monkeypatch.setattr(cli, 'source_binding', lambda path: {})
    monkeypatch.setattr(cli, 'exclusive_run', lambda path: nullcontext())
    monkeypatch.setattr(cli, 'publish', lambda *args: 2)
    def child(command, *, seconds):
        calls.append(command)
        assert seconds == 240
        assert command[command.index('--incremental-version')+1] == cli.INCREMENTAL_VERSION
        assert command[command.index('--incremental-cache-root')+1] == str(tmp_path/'cache')
        assert '--evidence-run-dir' in command and '--evidence-root' not in command
        return b'{"schema":"ATRSIMLIVE1_TODAY_COMPACT_V2"}', .01
    monkeypatch.setattr(cli, 'bounded_child', child)
    assert cli.main(base_args() + ['--policy', '/CONTROLLED/policy.json',
        '--stored-null-policy', NULL_REGULAR_V1, '--evidence-root', str(directory(tmp_path)),
        '--destination', str(tmp_path/'current.json'), '--incremental-version', cli.INCREMENTAL_VERSION,
        '--incremental-cache-root', str(tmp_path/'cache')]) == 0
    assert len(calls) == 1


@pytest.mark.parametrize('enabled', [False, True])
def test_child_binds_same_context_to_collector_and_model_and_seals_before_commit(monkeypatch, tmp_path, enabled):
    import atrsimlive1 as cli
    import atrsimlive1_collect as collection
    import atrsimlive1_models as models
    from atrsimlive1_null_policy import NULL_REGULAR_V1
    from project_mai_tai.atr_simulation import same_sip_binding
    events, instance = [], object()
    packet, doc = {'symbols': ['ELPW']}, original()
    run = directory(tmp_path)
    monkeypatch.setattr(cli, 'source_binding', lambda path: events.append('source_check') or {'head':'a'*40})
    monkeypatch.setattr(cli, 'read_today_routes', lambda: (None, 'CONTROLLED'))
    monkeypatch.setattr(collection, 'dsn_from_environment', lambda name: 'CONTROLLED')
    monkeypatch.setattr(same_sip_binding, 'verify_same_sip_files', lambda: 'CONTROLLED_TIES')
    @contextmanager
    def context(args, producer, conservative):
        events.append('cache_enter')
        yield instance if enabled else None
        assert (run/'atrsim-evidence.jsonl.gz').is_file()
        events.append('cache_commit')
    monkeypatch.setattr(cli, 'incremental_context', context)
    def collect(**kwargs):
        assert kwargs.get('incremental') is (instance if enabled else None)
        assert ('incremental' in kwargs) is enabled
        events.append('collect')
        return [kwargs['processor'](packet)]
    def produce(data, **kwargs):
        assert kwargs.get('incremental') is (instance if enabled else None)
        assert ('incremental' in kwargs) is enabled
        events.append('model')
        return deepcopy(doc)
    monkeypatch.setattr(collection, 'collect', collect)
    monkeypatch.setattr(models, 'produce', produce)
    output = BytesIO()
    monkeypatch.setattr(cli.sys, 'stdout', SimpleNamespace(buffer=output))
    args = base_args() + ['--child', '--policy', '/CONTROLLED/policy.json',
        '--stored-null-policy', NULL_REGULAR_V1, '--evidence-run-dir', str(run)]
    if enabled: args += ['--incremental-version', cli.INCREMENTAL_VERSION,
                         '--incremental-cache-root', str(tmp_path/'cache')]
    assert cli.main(args) == 0
    assert events.index('collect') < events.index('model') < events.index('cache_commit')
    assert events.count('cache_commit') == 1 and events[-2:] == ['source_check', 'cache_commit']
    assert json.loads(output.getvalue())['schema'] == 'ATRSIMLIVE1_TODAY_COMPACT_V2'


def test_failed_cache_save_propagates_and_parent_never_publishes(monkeypatch, tmp_path):
    import atrsimlive1 as cli
    monkeypatch.setattr(cli, 'source_binding', lambda path: {})
    monkeypatch.setattr(cli, 'exclusive_run', lambda path: nullcontext())
    monkeypatch.setattr(cli, 'publish', lambda *args: pytest.fail('failed child cannot publish'))
    def child(*args, **kwargs):
        raise ChildProcessError('controlled physically ended cache-save failure')
    monkeypatch.setattr(cli, 'bounded_child', child)
    with pytest.raises(ChildProcessError):
        cli.main(base_args() + ['--destination', str(tmp_path/'current.json')])
    assert not (tmp_path/'current.json').exists()


@pytest.mark.parametrize('name', ['incremental_cache', 'incremental_codec', 'incremental_reads',
                                  'incremental_collect', 'incremental_models', 'incremental_file',
                                  'incremental_container'])
def test_each_incremental_helper_is_mandatory_external_source_pin(tmp_path, name):
    import atrsimlive1 as cli
    relative = 'tools/atrsimlive1_' + name + '.py'
    assert relative in cli.REQUIRED_SOURCE
    path = tmp_path/'source.json'
    path.write_text(json.dumps({'schema':'ATRSIMLIVE1_REVIEWED_SOURCE_V1','head':'a'*40,
        'files':{p:'a'*64 for p in cli.REQUIRED_SOURCE if p != relative}}))
    with pytest.raises(ValueError, match='all_loadbearing_source_pins_required'):
        cli.source_binding(path)


def test_only_literal_incremental_stage_names_forward_no_sql_or_unknown_text(monkeypatch):
    import atrsimlive1_progress as progress
    target = StringIO()
    monkeypatch.setattr(progress.sys, 'stderr', target)
    good = b'1793230000000 INCREMENTAL_FINGERPRINT:trades:ELPW+\n'
    good += b'1793230000001 INCREMENTAL_ROWS:quotes:ELPW-:5000\n'
    good += b'1793230000002 INCREMENTAL_TERMINAL:CHSN+\n1793230000003 CACHE_SAVE-\n'
    good += b'1793230000004 CACHE_LOAD+\n1793230000005 CACHE_WRITE-:100\n'
    progress.StageForwarder().feed(good+b'1793230000004 INCREMENTAL_SQL:SELECT_SECRET+\n')
    assert target.getvalue() == good.decode('ascii')


@pytest.mark.parametrize('failure', [False, True])
def test_real_context_binds_current_RR_scope_and_only_commits_after_success(monkeypatch, failure):
    import atrsimlive1 as cli
    import atrsimlive1_incremental_collect as coordinator
    import atrsimlive1_incremental_cache as cache
    import atrsimlive1_namespace as namespaces
    from atrsimlive1_window_collect import POLICY_SHA
    token, calls = object(), []
    args = SimpleNamespace(incremental_version=cli.INCREMENTAL_VERSION,
        incremental_cache_root='/CONTROLLED/never-opened', stored_null_policy='CONTROLLED_NULL')
    def namespace(root, binding):
        assert root == args.incremental_cache_root
        return '/CONTROLLED/never-opened/' + namespaces.namespace_name(binding)
    monkeypatch.setattr(namespaces, 'namespace_root', namespace)
    @contextmanager
    def session(root, binding, *, version):
        assert root == args.incremental_cache_root + '/' + namespaces.namespace_name(binding)
        assert version == cli.INCREMENTAL_VERSION
        assert binding['input_scope'] == 'CURRENT_RR'
        assert set(binding) == {'version', 'input_scope', 'trading_day', 'source_seal', 'policies'}
        assert binding['source_seal'] == cache.fingerprint({'source.py':'a'*64})
        assert binding['policies'] == dict(execution_rules_sha256=POLICY_SHA,
            null_policy='CONTROLLED_NULL', same_sip_policy='CONTROLLED_TIES')
        calls.append('enter')
        yield token
        calls.append('commit')
    monkeypatch.setattr(coordinator, 'incremental_session', session)
    if failure:
        with pytest.raises(ValueError, match='controlled model failure'):
            with cli.incremental_context(args, {'source_pins':{'source.py':'a'*64}}, 'CONTROLLED_TIES') as run:
                assert run is token
                raise ValueError('controlled model failure')
        assert calls == ['enter']
    else:
        with cli.incremental_context(args, {'source_pins':{'source.py':'a'*64}}, 'CONTROLLED_TIES') as run:
            assert run is token
            assert calls == ['enter']
        assert calls == ['enter', 'commit']
