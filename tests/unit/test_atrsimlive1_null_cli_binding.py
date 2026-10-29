"""Controlled successor CLI wiring; no actual inputs or economic claims."""
from contextlib import nullcontext
from io import BytesIO
import json
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('enabled', (False, True))
def test_child_binds_same_explicit_null_policy_to_selection_and_model(monkeypatch, tmp_path, enabled):
    import atrsimlive1 as cli
    import atrsimlive1_collect as collection
    import atrsimlive1_models as models
    from atrsimlive1_null_policy import NULL_REGULAR_V1
    from project_mai_tai.atr_simulation import same_sip_binding as binding
    calls = []
    expected = NULL_REGULAR_V1 if enabled else None
    monkeypatch.setattr(cli, 'source_binding', lambda path: {'head': 'a' * 40})
    monkeypatch.setattr(collection, 'dsn_from_environment', lambda name: 'CONTROLLED')
    monkeypatch.setattr(cli, 'read_today_routes', lambda: (None, 'CONTROLLED'))
    monkeypatch.setattr(binding, 'verify_same_sip_files', lambda: 'CONTROLLED_TIES')
    def collect(**kwargs):
        calls.append(('collect', kwargs['null_policy']))
        return [kwargs['processor']({'CONTROLLED': True})]
    def produce(data, **kwargs):
        calls.append(('produce', kwargs['null_policy']))
        assert kwargs['same_sip_policy'] == 'CONTROLLED_TIES'
        return {'CONTROLLED': True}
    monkeypatch.setattr(collection, 'collect', collect)
    monkeypatch.setattr(models, 'produce', produce)
    monkeypatch.setattr(models, 'combine', lambda values: values[0])
    output = BytesIO()
    monkeypatch.setattr(cli.sys, 'stdout', SimpleNamespace(buffer=output))
    args = ['--child', '--source-manifest', str(tmp_path/'source.json'),
            '--dsn-env', 'DATABASE_URL', '--policy', str(tmp_path/'policy.json')]
    if enabled:
        args += ['--stored-null-policy', NULL_REGULAR_V1]
    assert cli.main(args) == 0
    assert calls == [('collect', expected), ('produce', expected)]
    assert json.loads(output.getvalue()) == {'CONTROLLED': True}


def test_parent_forwards_literal_null_version_to_child(monkeypatch, tmp_path):
    import atrsimlive1 as cli
    from atrsimlive1_null_policy import NULL_REGULAR_V1
    calls = []
    monkeypatch.setattr(cli, 'source_binding', lambda path: {})
    monkeypatch.setattr(cli, 'exclusive_run', lambda path: nullcontext())
    def child(command, *, seconds):
        calls.append(command)
        assert seconds == 240
        return b'{}', 0.01
    monkeypatch.setattr(cli, 'bounded_child', child)
    monkeypatch.setattr(cli, 'publish', lambda *args: 2)
    assert cli.main(['--source-manifest', str(tmp_path/'source.json'), '--dsn-env', 'DATABASE_URL',
        '--policy', str(tmp_path/'policy.json'), '--stored-null-policy', NULL_REGULAR_V1,
        '--destination', str(tmp_path/'current.json')]) == 0
    assert calls[0][calls[0].index('--stored-null-policy') + 1] == NULL_REGULAR_V1


@pytest.mark.parametrize('mode', ('unknown_version', 'missing_policy'))
def test_bad_or_unbound_null_option_refuses_before_source_or_inputs(monkeypatch, mode):
    import atrsimlive1 as cli
    from atrsimlive1_null_policy import NULL_REGULAR_V1
    monkeypatch.setattr(cli, 'source_binding', lambda path: pytest.fail('must refuse before source'))
    args = ['--source-manifest', '/CONTROLLED/source.json', '--dsn-env', 'DATABASE_URL',
            '--stored-null-policy', NULL_REGULAR_V1 if mode == 'missing_policy' else 'TRUTHY']
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_null_decoder_helper_is_mandatory_external_source_pin(tmp_path):
    import atrsimlive1 as cli
    name = 'tools/atrsimlive1_null_policy.py'
    assert name in cli.REQUIRED_SOURCE
    path = tmp_path/'source.json'
    path.write_text(json.dumps({'schema': 'ATRSIMLIVE1_REVIEWED_SOURCE_V1',
        'head': 'a'*40, 'files': {p: 'a'*64 for p in cli.REQUIRED_SOURCE if p != name}}))
    with pytest.raises(ValueError, match='all_loadbearing_source_pins_required'):
        cli.source_binding(path)
