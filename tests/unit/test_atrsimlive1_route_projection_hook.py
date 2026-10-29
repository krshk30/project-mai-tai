"""Bounded local evidence transport only; no route, tape or financial proof."""
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tests.unit.test_atrsimlive1_runtime import module


def written(tmp_path, raw):
    path = tmp_path / 'route-projection.json'
    path.write_bytes(raw)
    return str(path), hashlib.sha256(raw).hexdigest()


def test_bound_packet_roundtrips_without_deriving_route_or_finance(tmp_path):
    cli = module('atrsimlive1')
    packet = dict(schema='ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1', lines=[])
    path, digest = written(tmp_path, json.dumps(packet).encode())
    assert cli.country_route_projection(path, digest) == (packet, 'BOUND_LOCAL_PROJECTION')


@pytest.mark.parametrize('path,digest', [(None, 'a'*64), ('relative.json', 'a'*64), ('/absent', 'BAD')])
def test_partial_relative_or_invalid_binding_never_grants_route(path, digest):
    cli = module('atrsimlive1')
    assert cli.country_route_projection(path, digest) == (None, 'INVALID_BINDING')
    assert cli.country_route_projection(None, None) == (None, 'NOT_SUPPLIED')


@pytest.mark.parametrize('raw', [b'{"schema":"foreign"}', b'[]', b'{"schema":1,"schema":2}',
                                 b'{"value":1e400}', b'{"value":"\\ud800"}'])
def test_malformed_or_foreign_projection_is_unknown_even_with_matching_hash(tmp_path, raw):
    cli = module('atrsimlive1')
    path, digest = written(tmp_path, raw)
    packet, status = cli.country_route_projection(path, digest)
    assert packet is None and status in {'INVALID_PROJECTION_SCHEMA', 'UNREADABLE_OR_INVALID'}


def test_changed_hash_missing_symlink_and_oversize_never_grant_route(tmp_path):
    cli = module('atrsimlive1')
    path, digest = written(tmp_path, b'{"schema":"ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1"}')
    assert cli.country_route_projection(path, '0'*64) == (None, 'CHANGED_OR_HASH_MISMATCH')
    alias = tmp_path / 'alias.json'
    alias.symlink_to(path)
    assert cli.country_route_projection(str(alias), digest) == (None, 'UNREADABLE_OR_INVALID')
    assert cli.country_route_projection(str(tmp_path / 'missing'), digest) == (None, 'UNREADABLE_OR_INVALID')
    Path(path).write_bytes(b'x' * (2*1024*1024+1))
    assert cli.country_route_projection(path, digest) == (None, 'FILE_BOUND_OR_SHAPE')


def test_change_during_read_is_unknown_despite_correct_content_hash(tmp_path, monkeypatch):
    cli = module('atrsimlive1')
    path, digest = written(tmp_path, b'{"schema":"ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1"}')
    original = cli.os.fstat
    calls = []
    def changed(fd):
        value = original(fd)
        calls.append(fd)
        return SimpleNamespace(st_dev=value.st_dev, st_ino=value.st_ino, st_size=value.st_size,
            st_mode=value.st_mode, st_uid=value.st_uid, st_ctime_ns=value.st_ctime_ns,
            st_mtime_ns=value.st_mtime_ns + (1 if len(calls) == 2 else 0))
    monkeypatch.setattr(cli.os, 'fstat', changed)
    assert cli.country_route_projection(path, digest) == (None, 'CHANGED_OR_HASH_MISMATCH')
    assert len(calls) == 2


def test_child_loads_bound_projection_once_and_shares_same_packet_across_symbols(tmp_path, monkeypatch, capsys):
    cli = module('atrsimlive1')
    packet = dict(schema='ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1', lines=[])
    path, digest = written(tmp_path, json.dumps(packet).encode())
    opened, produced = [], []
    original = cli.os.open
    def observed(name, *args, **kwargs):
        if str(name) == path:
            opened.append(name)
        return original(name, *args, **kwargs)
    monkeypatch.setattr(cli.os, 'open', observed)
    monkeypatch.setattr(cli, 'source_binding', lambda name: {'head': 'a'*40})
    def collect(*, dsn, processor, capture_log_path):
        assert dsn == 'CONTROLLED_NO_DATABASE'
        return [processor({'symbol': s}) for s in ('ELPW', 'POM')]
    def produce(data, *, policy_path, producer, route_packet):
        produced.append((data['symbol'], route_packet, producer['country_route_projection_status']))
        return data
    monkeypatch.setitem(sys.modules, 'atrsimlive1_collect', SimpleNamespace(
        collect=collect, dsn_from_environment=lambda name: 'CONTROLLED_NO_DATABASE',
        encoded=lambda value: json.dumps(value).encode()))
    monkeypatch.setitem(sys.modules, 'atrsimlive1_models', SimpleNamespace(
        produce=produce, combine=lambda rows: {'rows': rows}))
    assert cli.main(['--source-manifest', 'CONTROLLED', '--dsn-env', 'MAI_TAI_DATABASE_URL', '--child',
        '--country-route-projection', path, '--country-route-sha256', digest]) == 0
    assert len(opened) == 1 and len(produced) == 2
    assert produced[0][1] is produced[1][1] and produced[0][1] == packet
    assert all(row[2] == 'BOUND_LOCAL_PROJECTION' for row in produced)
    assert len(json.loads(capsys.readouterr().out)['rows']) == 2


def test_parent_alias_cannot_redirect_a_literal_evidence_path(tmp_path):
    cli = module('atrsimlive1')
    target = tmp_path / 'target'
    target.mkdir()
    path, digest = written(target, b'{"schema":"ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1"}')
    alias = tmp_path / 'parent-alias'
    alias.symlink_to(target, target_is_directory=True)
    assert cli.country_route_projection(str(alias / Path(path).name), digest) == (None, 'UNREADABLE_OR_INVALID')


def test_writable_evidence_is_not_accepted_as_an_owned_immutable_input(tmp_path):
    cli = module('atrsimlive1')
    path, digest = written(tmp_path, b'{"schema":"ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1"}')
    Path(path).chmod(0o666)
    assert cli.country_route_projection(path, digest) == (None, 'FILE_BOUND_OR_SHAPE')
