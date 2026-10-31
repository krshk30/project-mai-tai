"""Explicit v2 process/publication junction; controlled pure/private FILE only."""
from contextlib import nullcontext
from copy import deepcopy
from io import BytesIO
import gzip
import json
from types import SimpleNamespace
import pytest

from tests.unit.test_atrsimlive1_output_split import assembled, original, directory, NOW


@pytest.mark.parametrize('compact', (False, True))
def test_child_preserves_v1_default_or_streams_all_v2_input_and_model(monkeypatch, tmp_path, compact):
    import atrsimlive1 as cli
    import atrsimlive1_collect as collection
    import atrsimlive1_models as models
    doc = original()
    packet = {'symbols': ['ELPW'], 'all_original_bars': [{'id': 'ORIGINAL_BAR', 'conditions': None}]}
    calls = []
    monkeypatch.setattr(cli, 'source_binding', lambda path: {'head': 'a'*40})
    monkeypatch.setattr(cli, 'read_today_routes', lambda: (None, 'CONTROLLED'))
    monkeypatch.setattr(collection, 'dsn_from_environment', lambda name: 'CONTROLLED')
    monkeypatch.setattr(collection, 'collect', lambda **kw: [kw['processor'](packet)])
    monkeypatch.setattr(models, 'produce', lambda *a, **kw: deepcopy(doc))
    monkeypatch.setattr(models, 'combine', lambda values: calls.append('legacy_combine') or values[0])
    output = BytesIO()
    monkeypatch.setattr(cli.sys, 'stdout', SimpleNamespace(buffer=output))
    args = ['--child', '--source-manifest', str(tmp_path/'source.json'), '--dsn-env', 'DATABASE_URL']
    run = directory(tmp_path)
    if compact:
        args += ['--evidence-run-dir', str(run)]
    assert cli.main(args) == 0
    result = json.loads(output.getvalue())
    assert result['schema'] == ('ATRSIMLIVE1_TODAY_COMPACT_V2' if compact else 'ATRSIMLIVE1_TODAY_V1')
    if not compact:
        assert result == doc and calls == ['legacy_combine'] and list(run.iterdir()) == []
    else:
        records = [json.loads(line) for line in gzip.decompress((run/'atrsim-evidence.jsonl.gz').read_bytes()).splitlines()]
        assert records[0]['payload'] == packet and records[1]['payload'] == doc
        assert len(records) == 3 and records[2]['kind'] == 'run' and calls == []
        assert len(output.getvalue()) < 1024*1024 and 'engine_receipts' not in output.getvalue().decode()


def test_parent_makes_distinct_private_run_dirs_and_forwards_only_child_path(monkeypatch, tmp_path):
    import atrsimlive1 as cli
    root = directory(tmp_path, 'runs')
    calls = []
    monkeypatch.setattr(cli, 'source_binding', lambda path: {})
    monkeypatch.setattr(cli, 'exclusive_run', lambda path: nullcontext())
    monkeypatch.setattr(cli, 'publish', lambda *args: 2)
    def child(command, **kwargs):
        calls.append(command)
        assert '--evidence-root' not in command
        return b'{"schema":"ATRSIMLIVE1_TODAY_COMPACT_V2"}', 0.01
    monkeypatch.setattr(cli, 'bounded_child', child)
    args = ['--source-manifest', str(tmp_path/'source.json'), '--dsn-env', 'DATABASE_URL',
        '--destination', str(tmp_path/'current.json'), '--evidence-root', str(root)]
    assert cli.main(args) == cli.main(args) == 0
    paths = [command[command.index('--evidence-run-dir')+1] for command in calls]
    assert len(set(paths)) == 2
    from pathlib import Path
    assert all(Path(path).parent == root and Path(path).stat().st_mode & 0o777 == 0o700 for path in paths)


@pytest.mark.parametrize('compact', (False, True))
def test_parent_refuses_schema_other_than_explicit_requested_mode(monkeypatch, tmp_path, compact):
    import atrsimlive1 as cli
    monkeypatch.setattr(cli, 'source_binding', lambda path: {})
    monkeypatch.setattr(cli, 'exclusive_run', lambda path: nullcontext())
    monkeypatch.setattr(cli, 'publish', lambda *args: pytest.fail('wrong mode must not publish'))
    wrong = 'ATRSIMLIVE1_TODAY_V1' if compact else 'ATRSIMLIVE1_TODAY_COMPACT_V2'
    monkeypatch.setattr(cli, 'bounded_child', lambda *a, **kw: (json.dumps({'schema': wrong}).encode(), 0.1))
    args = ['--source-manifest', str(tmp_path/'source.json'), '--dsn-env', 'DATABASE_URL',
        '--destination', str(tmp_path/'current.json')]
    if compact:
        args += ['--evidence-root', str(directory(tmp_path))]
    with pytest.raises(ValueError, match='explicit_requested_output_schema_required'):
        cli.main(args)


@pytest.mark.parametrize('child', (False, True))
def test_parent_and_child_evidence_flags_cannot_cross_authority(monkeypatch, child):
    import atrsimlive1 as cli
    monkeypatch.setattr(cli, 'source_binding', lambda path: pytest.fail('must refuse before source'))
    args = ['--source-manifest', '/CONTROLLED/source.json', '--dsn-env', 'DATABASE_URL']
    args += ['--child', '--evidence-root', '/CONTROLLED'] if child else ['--evidence-run-dir', '/CONTROLLED']
    with pytest.raises(SystemExit) as error:
        cli.main(args)
    assert error.value.code == 2


def test_compact_runtime_publishes_original_object_and_reader_accepts(tmp_path):
    import atrsimlive1_runtime as runtime
    from project_mai_tai.atr_simulation.today_file import read_today_file
    path, compact, seal = assembled(tmp_path)
    before = deepcopy(compact)
    assert runtime.validate(compact, NOW) is compact
    current = path/'current.json'
    runtime.publish(compact, current, NOW)
    assert json.loads(current.read_bytes()) == before
    assert read_today_file(current, now=NOW)['file_status'] == 'AVAILABLE'


@pytest.mark.parametrize('fault', ('day', 'future', 'invalid_row'))
def test_compact_runtime_rejects_wrong_day_future_or_bad_rows(tmp_path, fault):
    import atrsimlive1_runtime as runtime
    path, compact, seal = assembled(tmp_path)
    if fault == 'day':
        compact['trading_day'] = '2026-10-27'
    elif fault == 'future':
        compact['generated_at'] = '2026-10-29T18:00:00+00:00'
    else:
        compact['tables']['FIRSTBAR']['rows'][0]['row']['pnl_label_known'] = 'true'
    with pytest.raises(ValueError):
        runtime.publish(compact, path/'current.json', NOW)
    assert not (path/'current.json').exists()


def test_output_helper_cannot_be_omitted_from_reviewed_source_manifest(tmp_path):
    import atrsimlive1 as cli
    name = 'tools/atrsimlive1_output.py'
    assert name in cli.REQUIRED_SOURCE
    path = tmp_path/'source.json'
    path.write_text(json.dumps({'schema': 'ATRSIMLIVE1_REVIEWED_SOURCE_V1', 'head': 'a'*40,
        'files': {name: 'a'*64 for name in cli.REQUIRED_SOURCE if name != 'tools/atrsimlive1_output.py'}}))
    with pytest.raises(ValueError, match='all_loadbearing_source_pins_required'):
        cli.source_binding(path)
