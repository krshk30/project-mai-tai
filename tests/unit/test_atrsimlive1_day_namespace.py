"""Controlled private FILE day/seal boundaries, never timer/native evidence."""
from copy import deepcopy
import hashlib
import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return (importlib.import_module('atrsimlive1_namespace'),
            importlib.import_module('atrsimlive1_incremental_cache'))


def binding():
    return dict(version='C1_20261028_200908_ATRSIM_INCREMENTAL_V1',
        trading_day='2026-10-28', source_seal='a' * 64, input_scope='CURRENT_RR',
        policies=dict(execution_rules_sha256='b' * 64, null_policy=None,
                      same_sip_policy='CONTROLLED_TIES'))


def parent(tmp_path):
    path = tmp_path / 'cache-v2'
    path.mkdir(mode=0o700)
    return path


def test_same_binding_reopens_exact_namespace_without_touching_existing_bytes(monkeypatch, tmp_path):
    ns, cache = modules(monkeypatch)
    root, bind = parent(tmp_path), binding()
    first = ns.namespace_root(root, bind)
    assert first.name == bind['trading_day'] + '-' + cache.fingerprint(bind)
    assert first.stat().st_mode & 0o777 == 0o700 and first.stat().st_uid == os.geteuid()
    sentinel = first / 'untouched-prior-receipt'
    sentinel.write_bytes(b'CONTROLLED-original-retained-bytes')
    inode = first.stat().st_ino
    assert ns.namespace_root(root, deepcopy(bind)) == first
    assert first.stat().st_ino == inode
    assert sentinel.read_bytes() == b'CONTROLLED-original-retained-bytes'


@pytest.mark.parametrize('change', ['day', 'source', 'policy'])
def test_new_day_source_or_policy_gets_empty_namespace_and_keeps_prior_cache(monkeypatch, tmp_path, change):
    ns, cache = modules(monkeypatch)
    root, before = parent(tmp_path), binding()
    old = ns.namespace_root(root, before)
    cache.CacheStore(old, before).save(dict(windows={}, worlds={}, roster=[]))
    raw = (old / 'cache.json').read_bytes()
    after = deepcopy(before)
    if change == 'day':
        after['trading_day'] = '2026-10-29'
    elif change == 'source':
        after['source_seal'] = 'c' * 64
    else:
        after['policies']['same_sip_policy'] = 'CONTROLLED_OTHER_POLICY'
    new = ns.namespace_root(root, after)
    assert new != old and list(new.iterdir()) == []
    assert cache.CacheStore(new, after).load() is None
    assert (old / 'cache.json').read_bytes() == raw
    assert set(root.iterdir()) == {old, new}


@pytest.mark.parametrize('fault', ['child-symlink', 'child-file', 'child-mode', 'parent-mode', 'parent-symlink'])
def test_foreign_geometry_symlink_and_permissions_refuse_without_adoption(monkeypatch, tmp_path, fault):
    ns, cache = modules(monkeypatch)
    root, bind = parent(tmp_path), binding()
    child = root / ns.namespace_name(bind)
    original = None
    if fault == 'child-symlink':
        target = tmp_path / 'foreign'
        target.mkdir(mode=0o700)
        child.symlink_to(target, target_is_directory=True)
    elif fault == 'child-file':
        original = b'CONTROLLED_FOREIGN_COLLISION'
        child.write_bytes(original)
    elif fault == 'child-mode':
        child.mkdir(mode=0o750)
        child.chmod(0o750)  # The enclosing FILE fence intentionally uses umask077.
        assert child.stat().st_mode & 0o777 == 0o750
    elif fault == 'parent-mode':
        root.chmod(0o750)
    else:
        link = tmp_path / 'foreign-parent'
        link.symlink_to(root, target_is_directory=True)
        root = link
    with pytest.raises((cache.CacheRefusal, OSError)):
        ns.namespace_root(root, bind)
    if original is not None:
        assert child.read_bytes() == original
    assert not (child / 'cache.json').exists()


def test_foreign_child_owner_refuses_even_when_parent_is_owned(monkeypatch, tmp_path):
    ns, cache = modules(monkeypatch)
    root, bind = parent(tmp_path), binding()
    child = root / ns.namespace_name(bind)
    child.mkdir(mode=0o700)
    inode, device = child.stat().st_ino, child.stat().st_dev
    original = os.fstat
    def foreign(fd):
        info = original(fd)
        if (info.st_ino, info.st_dev) == (inode, device):
            return SimpleNamespace(st_ino=inode, st_dev=device, st_mode=info.st_mode,
                                   st_uid=info.st_uid + 1)
        return info
    monkeypatch.setattr(ns.os, 'fstat', foreign)
    with pytest.raises(cache.CacheRefusal, match='owned_exact_namespace_identity_required'):
        ns.namespace_root(root, bind)
    assert list(child.iterdir()) == []


@pytest.mark.parametrize('day', ['../2026-10-28', '20261028', '2026-10-28T00:00:00', 'not-a-day'])
def test_noncanonical_day_never_selects_or_creates_a_namespace(monkeypatch, tmp_path, day):
    ns, _ = modules(monkeypatch)
    root, bind = parent(tmp_path), binding()
    bind['trading_day'] = day
    with pytest.raises(ValueError):
        ns.namespace_root(root, bind)
    assert list(root.iterdir()) == []


def test_namespace_helper_is_a_mandatory_reviewed_pin(monkeypatch):
    modules(monkeypatch)
    cli = importlib.import_module('atrsimlive1')
    assert 'tools/atrsimlive1_namespace.py' in cli.REQUIRED_SOURCE
