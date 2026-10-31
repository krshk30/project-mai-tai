"""Main-only C1/C2 composition controls; no pruning/cleanup or model calls.

The ON control requires Main's reviewed16e32 C2 module in the composed root.
Its absence is a failure, never a skip or successful permission stub.
"""
import builtins
import fcntl
import importlib
import os
from pathlib import Path

import pytest


def source(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'tools'))
    return (importlib.import_module('atrsimlive1_incremental_cache'),
            importlib.import_module('atrsimlive1_namespace'))


def binding(cache):
    return dict(version=cache.INCREMENTAL_V1, trading_day='2026-10-28', source_seal='a' * 64,
        input_scope='CURRENT_RR', policies=dict(execution_rules_sha256='b' * 64, null_policy=None, same_sip_policy=None))


def test_reader_lease_OFF_never_imports_C2_or_creates_parent_lock(monkeypatch, tmp_path):
    cache, _ = source(monkeypatch)
    root = (tmp_path / 'old-profile').resolve()
    root.mkdir(mode=0o700)
    original = builtins.__import__
    def checked(name, *args, **kwargs):
        if name == 'atrsimlive1_namespace_retention':
            pytest.fail('OFF imported optional C2')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', checked)
    with cache.locked_cache(root, binding(cache)) as store:
        store.save({'CONTROLLED': 'unchanged ordinary bytes'})
        assert store.load() == {'CONTROLLED': 'unchanged ordinary bytes'}
    assert not (root / '.retention.lock').exists()


def test_actual_C2_shared_parent_lock_spans_complete_cache_context_and_lazy_reads(monkeypatch, tmp_path):
    cache, namespace = source(monkeypatch)
    # Actual Main-owned module, not monkeypatched lease/permission behavior.
    importlib.import_module('atrsimlive1_namespace_retention')
    parent = (tmp_path / 'volatile-parent').resolve()
    parent.mkdir(mode=0o700)
    bound = binding(cache)
    root = namespace.namespace_root(parent, bound, retention_enabled=True)
    assert (parent / '.retention.lock').stat().st_mode & 0o777 == 0o600
    descriptor = os.open(parent / '.retention.lock', os.O_RDWR | os.O_NOFOLLOW)
    try:
        with cache.locked_cache(root, bound, retention_enabled=True) as store:
            store.save(dict(worlds={'CONTROLLED|FIRSTBAR': {'member_keys': ['c' * 64],
                'bindings': ['d' * 64], 'CONTROLLED': 'not an executable model'}}))
            loaded = store.load()
            assert loaded['worlds']['CONTROLLED|FIRSTBAR']['CONTROLLED'] == 'not an executable model'
            with pytest.raises(BlockingIOError):
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
