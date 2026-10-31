"""Private error evidence, privacy and real CLI error-boundary controls only."""
import hashlib
import importlib
import json
from pathlib import Path

import pytest


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return (importlib.import_module('atrsimlive1_failure'),
            importlib.import_module('atrsimlive1'), importlib.import_module('atrsimlive1_runtime'))


def run_dir(tmp_path):
    path = tmp_path / 'evidence-run'
    path.mkdir(mode=0o700)
    return path


def raised(error):
    try:
        raise error
    except Exception as found:
        return found


def test_private_receipt_hash_permissions_and_existing_input_are_preserved(monkeypatch, tmp_path):
    failure, _, _ = modules(monkeypatch)
    directory = run_dir(tmp_path)
    original = directory / '.atrsim-evidence.jsonl.gz.partial'
    original.write_bytes(b'original unsealed input remains byte-identical')
    token = failure.write_failure(raised(ValueError('cache_file_byte_cap')), directory, role='child')
    path = directory / 'child-exception.json'
    raw = path.read_bytes()
    packet = json.loads(raw)
    assert token == {'exception_class': 'ValueError', 'evidence_sha256': hashlib.sha256(raw).hexdigest()}
    assert packet['message'] == 'cache_file_byte_cap'
    assert path.stat().st_mode & 0o777 == 0o600 and path.stat().st_nlink == 1
    assert original.read_bytes() == b'original unsealed input remains byte-identical'
    assert not (directory / '.child-exception.json.partial').exists()
    assert all(set(frame) == {'file', 'line', 'function'} for frame in packet['frames'])
    with pytest.raises(FileExistsError):
        failure.write_failure(raised(KeyError('source_asof')), directory, role='child')
    assert path.read_bytes() == raw  # A second exception never retags the first receipt.


@pytest.mark.parametrize('error', [
    ValueError('SELECT * FROM secret_table WHERE password=PRIVATE_SECRET'),
    RuntimeError('postgresql://user:PRIVATE_SECRET@private-host/database'),
    KeyError('PRIVATE_SECRET_API_KEY'),
    AttributeError('payload=PRIVATE_SECRET'),
    ValueError('PRIVATE_SECRET' * 10000),
])
def test_untrusted_messages_sql_dsns_values_and_large_strings_are_redacted(monkeypatch, tmp_path, error):
    failure, _, _ = modules(monkeypatch)
    directory = run_dir(tmp_path)
    failure.write_failure(raised(error), directory, role='child')
    raw = (directory / 'child-exception.json').read_bytes()
    assert b'PRIVATE_SECRET' not in raw and b'SELECT' not in raw and b'postgresql://' not in raw
    assert json.loads(raw)['message'] == 'REDACTED_UNTRUSTED_MESSAGE'
    assert len(raw) <= failure.MAX_BYTES


def test_schema_key_is_retained_without_its_value(monkeypatch, tmp_path):
    failure, _, _ = modules(monkeypatch)
    directory = run_dir(tmp_path)
    secret_local_value = 'PRIVATE_SECRET_VALUE'
    failure.write_failure(raised(KeyError('source_asof')), directory, role='parent')
    raw = (directory / 'parent-exception.json').read_bytes()
    assert json.loads(raw)['message'] == 'missing schema key: source_asof'
    assert secret_local_value.encode() not in raw


def test_real_source_dataclass_attribute_message_never_represents_object_values(monkeypatch, tmp_path):
    failure, _, _ = modules(monkeypatch)
    from atrsimlive1_working_windows import WorkingWindow
    value = WorkingWindow('PRIVATE_SECRET_SYMBOL', 0, None, 100, 'PRIVATE_SECRET_ORIGIN', False)
    try:
        value.snapshot_id
    except AttributeError as error:
        packet = failure.describe(error, 'child')
    assert packet['exception_class'] == 'AttributeError'
    assert packet['message'] == 'WorkingWindow missing attribute: snapshot_id'
    assert 'PRIVATE_SECRET' not in json.dumps(packet)


def test_only_last_twenty_frame_locations_no_locals_or_values(monkeypatch, tmp_path):
    failure, _, _ = modules(monkeypatch)
    def recurse(depth):
        _secret_local = 'PRIVATE_SECRET_LOCAL'
        if depth:
            return recurse(depth - 1)
        raise KeyError('source_asof')
    try:
        recurse(30)
    except KeyError as error:
        packet = failure.describe(error, 'child')
    assert len(packet['frames']) == 20
    assert all(set(frame) == {'file', 'line', 'function'} for frame in packet['frames'])
    assert all(type(frame['line']) is int and frame['line'] > 0 for frame in packet['frames'])
    assert 'PRIVATE_SECRET' not in json.dumps(packet)


def test_message_has_a_two_kib_utf8_bound(monkeypatch, tmp_path):
    failure, _, _ = modules(monkeypatch)
    contract = dict(failure.literals(), codes=['\u03bb' * 3000])
    monkeypatch.setattr(failure, 'literals', lambda: contract)
    message = failure.describe(raised(ValueError('\u03bb' * 3000)), 'child')['message']
    assert len(message.encode()) == 2048 and message == '\u03bb' * 1024


def test_unknown_exception_class_is_not_a_value_channel(monkeypatch, tmp_path):
    failure, _, _ = modules(monkeypatch)
    unknown = type('PRIVATE_SECRET_CLASS', (Exception,), {})('PRIVATE_SECRET_VALUE')
    packet = failure.describe(raised(unknown), 'child')
    assert packet['exception_class'] == 'OTHER' and 'PRIVATE_SECRET' not in json.dumps(packet)


def test_tampered_source_literal_file_cannot_whitelist_a_secret(monkeypatch, tmp_path):
    failure, _, _ = modules(monkeypatch)
    forged = json.loads((failure.ROOT / 'tools/atrsimlive1_failure_literals.json').read_bytes())
    forged['codes'].append('PRIVATE_SECRET')
    root = tmp_path / 'forged-source'
    (root / 'tools').mkdir(parents=True)
    (root / 'tools/atrsimlive1_failure_literals.json').write_text(json.dumps(forged))
    failure.literals.cache_clear()
    monkeypatch.setattr(failure, 'ROOT', root)
    try:
        packet = failure.describe(raised(ValueError('PRIVATE_SECRET')), 'child')
        assert packet['literal_contract_status'] == 'UNAVAILABLE_REDACTED'
        assert 'PRIVATE_SECRET' not in json.dumps(packet)
    finally:
        failure.literals.cache_clear()


@pytest.mark.parametrize('kind', ['group-readable', 'symlink'])
def test_failure_writer_refuses_nonprivate_or_symlink_directory(monkeypatch, tmp_path, kind):
    failure, _, _ = modules(monkeypatch)
    directory = run_dir(tmp_path)
    if kind == 'group-readable':
        directory.chmod(0o750)
    else:
        link = tmp_path / 'link'
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    with pytest.raises(ValueError):
        failure.write_failure(raised(KeyError('source_asof')), directory, role='child')
    assert not (directory / 'child-exception.json').exists()


@pytest.mark.parametrize('raw', [
    b'not-json', b'{}', b'{"exception_class":[],"evidence_sha256":null}',
    b'{"exception_class":"SECRET_CLASS","evidence_sha256":null}',
    b'{"exception_class":"ValueError","evidence_sha256":"/secret/path"}',
    b'{"exception_class":"ValueError","evidence_sha256":null,"message":"PRIVATE_SECRET"}',
    b'{"exception_class":"ValueError","exception_class":"KeyError","evidence_sha256":null}',
    b'x' * 513,
])
def test_parent_rejects_unbounded_or_extra_child_error_fields(monkeypatch, raw):
    failure, _, _ = modules(monkeypatch)
    assert failure.failure_token(raw) is None


def test_cli_failure_writes_private_file_and_only_class_plus_sha(monkeypatch, tmp_path, capsys):
    failure, cli, _ = modules(monkeypatch)
    directory = run_dir(tmp_path)
    def fail(argv, *, _failure):
        _failure.update(directory=str(directory), role='child')
        raise KeyError('source_asof')
    monkeypatch.setattr(cli, '_run_main', fail)
    with pytest.raises(KeyError):
        cli.main([])
    token = json.loads(capsys.readouterr().out)
    assert set(token) == {'exception_class', 'evidence_sha256'}
    assert token == failure.failure_token(json.dumps(token).encode())
    assert token['evidence_sha256'] == hashlib.sha256((directory / 'child-exception.json').read_bytes()).hexdigest()


def test_parent_cli_keeps_the_original_child_token_without_replacing_cause(monkeypatch, tmp_path, capsys):
    failure, cli, runtime = modules(monkeypatch)
    directory = run_dir(tmp_path)
    token = failure.write_failure(raised(AttributeError('PRIVATE_SECRET')), directory, role='child')
    def fail(argv, *, _failure):
        _failure.update(directory=str(directory), role='parent')
        raise runtime.ChildFailure(token)
    monkeypatch.setattr(cli, '_run_main', fail)
    with pytest.raises(runtime.ChildFailure):
        cli.main([])
    assert json.loads(capsys.readouterr().out) == token
    assert not (directory / 'parent-exception.json').exists()


def test_normal_cli_stdout_and_no_receipt_side_effect_are_unchanged(monkeypatch, tmp_path, capsys):
    _, cli, _ = modules(monkeypatch)
    def success(argv, *, _failure):
        print('CONTROLLED_ORIGINAL_COMPACT_BYTES', end='')
        return 0
    monkeypatch.setattr(cli, '_run_main', success)
    assert cli.main([]) == 0
    assert capsys.readouterr().out == 'CONTROLLED_ORIGINAL_COMPACT_BYTES'
    assert list(tmp_path.iterdir()) == []


def test_actual_child_cache_commit_failure_emits_only_error_token_never_compact_prefix(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from copy import deepcopy
    from io import BytesIO
    failure, cli, _ = modules(monkeypatch)
    import atrsimlive1_collect as collection
    import atrsimlive1_models as models
    from atrsimlive1_incremental_cache import CacheRefusal
    from atrsimlive1_null_policy import NULL_REGULAR_V1
    from project_mai_tai.atr_simulation import same_sip_binding
    from tests.unit.test_atrsimlive1_output_split import original
    directory = run_dir(tmp_path)
    monkeypatch.setattr(cli, 'source_binding', lambda _: {'head': 'a' * 40})
    monkeypatch.setattr(cli, 'read_today_routes', lambda: (None, 'CONTROLLED'))
    monkeypatch.setattr(collection, 'dsn_from_environment', lambda _: 'CONTROLLED')
    monkeypatch.setattr(same_sip_binding, 'verify_same_sip_files', lambda: 'CONTROLLED_TIES')
    monkeypatch.setattr(collection, 'collect', lambda **kw: [kw['processor']({'symbols': ['ELPW']})])
    monkeypatch.setattr(models, 'produce', lambda *a, **kw: deepcopy(original()))
    @contextmanager
    def failed_commit(*args):
        yield object()
        assert (directory / 'atrsim-evidence.jsonl.gz').is_file()
        raise CacheRefusal('cache_file_byte_cap')
    monkeypatch.setattr(cli, 'incremental_context', failed_commit)
    class Output:
        def __init__(self):
            self.buffer = BytesIO()
        def write(self, text):
            return self.buffer.write(text.encode())
        def flush(self):
            pass
    output = Output()
    monkeypatch.setattr(cli.sys, 'stdout', output)
    with pytest.raises(CacheRefusal):
        cli.main(['--source-manifest', '/CONTROLLED/source.json', '--dsn-env', 'DATABASE_URL',
            '--child', '--policy', '/CONTROLLED/policy.json', '--stored-null-policy', NULL_REGULAR_V1,
            '--evidence-run-dir', str(directory), '--incremental-version', cli.INCREMENTAL_VERSION,
            '--incremental-cache-root', str(tmp_path / 'cache')])
    token = failure.failure_token(output.buffer.getvalue())
    assert token is not None and token['exception_class'] == 'CacheRefusal'
    assert set(json.loads(output.buffer.getvalue())) == {'exception_class', 'evidence_sha256'}
    assert token['evidence_sha256'] == hashlib.sha256((directory / 'child-exception.json').read_bytes()).hexdigest()


@pytest.mark.parametrize('relative', ['tools/atrsimlive1_failure.py', 'tools/atrsimlive1_failure_literals.json'])
def test_private_error_implementation_and_literal_contract_are_mandatory_pins(monkeypatch, tmp_path, relative):
    _, cli, _ = modules(monkeypatch)
    assert relative in cli.REQUIRED_SOURCE
    path = tmp_path / 'source.json'
    path.write_text(json.dumps(dict(schema='ATRSIMLIVE1_REVIEWED_SOURCE_V1', head='a' * 40,
        files={name: 'b' * 64 for name in cli.REQUIRED_SOURCE if name != relative})))
    with pytest.raises(ValueError, match='all_loadbearing_source_pins_required'):
        cli.source_binding(path)
