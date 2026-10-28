"""Controlled configuration/refusal/lifecycle evidence, not actual PG or replay."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.atr_simulation import accepted_aggregate_bootstrap as boot
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_firstbar_readonly_mount import Recorder


def test_unconfigured_factory_is_inert_before_artifact_or_database(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('unconfigured source must stay inert')
    monkeypatch.setattr(boot, 'accepted_receipt_bytes', forbidden)
    monkeypatch.setattr(boot, 'AggregateEntryUIReader', forbidden)
    assert boot.configured_aggregate_port('CONTROLLED-no-db', None) is None
    assert Settings(_env_file=None).atr_simulation_aggregate_receipt_path is None


def test_literal_binding_remains_actual_14b_not_current_restored_engine():
    assert boot.PRODUCER_HEAD == '14b3e6198ad713d145f326a7388eb8298f748e55'
    assert len(boot.PRODUCER_PINS) == 17
    assert boot.PRODUCER_PINS['src/project_mai_tai/atr_simulation/types.py'] == '391bf7ec27bd3bbe03420d0130832fe2ba7150d2c491b13a9f418708874f7d98'
    assert boot.PRODUCER_PINS['src/project_mai_tai/atr_simulation/firstbar_exits.py'] == 'e895419f79346594133f619395c6f2d2cacf923662ff3a328a1eb5943c04d503'
    assert boot.RECEIPT_BYTES == 34886838
    assert boot.RECEIPT_SHA256 == '265b847b615df1efbb85cbf3b053bd9d597198309bdebd89d270ca458b0698e8'
    assert boot.PUBLICATION_SHA256 == '856a2601bd43a92dc2f92a1a2589d44814f25f775872474fc0d10c8c1ff2b9a3'


@pytest.mark.parametrize('fault', ('relative', 'missing', 'symlink', 'directory', 'size', 'writable', 'sha'))
def test_changed_or_unsafe_artifact_refuses_before_database(fault, tmp_path, monkeypatch):
    path = tmp_path / 'raw.json'
    path.write_bytes(b'x' * 64)
    path.chmod(0o600)
    monkeypatch.setattr(boot, 'RECEIPT_BYTES', 64)
    selected = path
    if fault == 'relative':
        selected = Path('relative.json')
    elif fault == 'missing':
        selected = tmp_path / 'absent.json'
    elif fault == 'symlink':
        selected = tmp_path / 'link.json'
        selected.symlink_to(path)
    elif fault == 'directory':
        selected = tmp_path
    elif fault == 'size':
        path.write_bytes(b'x')
    elif fault == 'writable':
        path.chmod(0o622)
    def forbidden(*args, **kwargs):
        raise AssertionError('invalid source must never create a reader')
    monkeypatch.setattr(boot, 'AggregateEntryUIReader', forbidden)
    with pytest.raises((ValueError, OSError)):
        boot.configured_aggregate_port('CONTROLLED-no-db', str(selected))


def test_setting_is_opt_in_and_invalid_explicit_path_refuses_startup(tmp_path):
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        settings = Settings(_env_file=None, atr_simulation_aggregate_receipt_path=str(tmp_path / 'missing'))
        with pytest.raises(OSError):
            cp.build_app(settings, session_factory=sessionmaker(engine), firstbar_read_port=Recorder())
    finally:
        engine.dispose()


def test_injected_port_never_loads_configured_artifact(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('caller-owned explicit port must not be replaced')
    monkeypatch.setattr(boot, 'configured_aggregate_port', forbidden)
    engine = sa.create_engine('sqlite:///:memory:')
    injected = SimpleNamespace(reader=SimpleNamespace())
    try:
        app = cp.build_app(Settings(_env_file=None, atr_simulation_aggregate_receipt_path='/CONTROLLED/unread'),
            session_factory=sessionmaker(engine), firstbar_read_port=Recorder(),
            aggregate_simulation_read_port=injected)
        assert app.state.aggregate_simulation_read_port is injected
    finally:
        engine.dispose()


@pytest.mark.parametrize('owned', (True, False))
def test_shutdown_closes_only_startup_owned_reader(monkeypatch, owned):
    closed = []
    port = SimpleNamespace(reader=SimpleNamespace(close=lambda: closed.append(True)))
    calls = []
    def prepare(database_url, path):
        calls.append((database_url, path))
        return port
    monkeypatch.setattr(boot, 'configured_aggregate_port', prepare)
    monkeypatch.setattr(cp, 'ControlPlaneRepository', lambda *args, **kwargs: SimpleNamespace())
    class Refresher:
        def __init__(self, settings):
            pass
        async def run(self):
            await asyncio.Event().wait()
    monkeypatch.setattr(cp, 'SchwabTokenRefresher', Refresher)
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        app = cp.build_app(Settings(_env_file=None, atr_simulation_aggregate_receipt_path='/CONTROLLED/startup'),
            session_factory=sessionmaker(engine), firstbar_read_port=Recorder(),
            redis_client=SimpleNamespace(), aggregate_simulation_read_port=None if owned else port)
        assert app.state.aggregate_simulation_read_port is port
        assert len(calls) == int(owned)
        async def scenario():
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
                assert closed == []
        asyncio.run(scenario())
        assert closed == ([True] if owned else [])
    finally:
        engine.dispose()


@pytest.mark.parametrize('cleanup_error', (None, 'redis', 'firstbar', 'aggregate'))
def test_failed_refresher_closes_all_owned_resources_and_preserves_primary(monkeypatch, cleanup_error):
    closed = []
    failure = ValueError('CONTROLLED_REF_FAILURE')
    def close(phase):
        closed.append(phase)
        if cleanup_error == phase:
            raise RuntimeError('CONTROLLED_CLOSE_FAILURE')
    async def redis_close():
        close('redis')
    monkeypatch.setattr(cp, 'Redis', SimpleNamespace(from_url=lambda *args, **kwargs:
        SimpleNamespace(aclose=redis_close)))
    monkeypatch.setattr(cp, 'ControlPlaneRepository', lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(boot, 'configured_aggregate_port', lambda *args:
        SimpleNamespace(reader=SimpleNamespace(close=lambda: close('aggregate'))))
    from project_mai_tai.atr_simulation import firstbar_ui_reader
    monkeypatch.setattr(firstbar_ui_reader, 'FirstBarSeedUIReader', lambda *args:
        SimpleNamespace(close=lambda: close('firstbar')))
    class Refresher:
        def __init__(self, settings):
            pass
        async def run(self):
            raise failure
    monkeypatch.setattr(cp, 'SchwabTokenRefresher', Refresher)
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        app = cp.build_app(Settings(_env_file=None, atr_simulation_aggregate_receipt_path='/CONTROLLED/startup'),
            session_factory=sessionmaker(engine))
        async def scenario():
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
        with pytest.raises(ValueError) as observed:
            asyncio.run(scenario())
        assert observed.value is failure
        assert closed == ['redis', 'firstbar', 'aggregate']
        if cleanup_error is not None:
            assert failure.__notes__ == ['shutdown_' + cleanup_error + '_failure=RuntimeError']
    finally:
        engine.dispose()


@pytest.mark.parametrize('failure_type', (KeyError, asyncio.CancelledError))
def test_body_error_or_cancellation_is_not_replaced_by_failed_refresher(monkeypatch, failure_type):
    closed = []
    primary = failure_type('CONTROLLED_BODY_FAILURE')
    port = SimpleNamespace(reader=SimpleNamespace(close=lambda: closed.append('aggregate')))
    monkeypatch.setattr(boot, 'configured_aggregate_port', lambda *args: port)
    monkeypatch.setattr(cp, 'ControlPlaneRepository', lambda *args, **kwargs: SimpleNamespace())
    class Refresher:
        def __init__(self, settings):
            pass
        async def run(self):
            raise ValueError('CONTROLLED_REF_FAILURE')
    monkeypatch.setattr(cp, 'SchwabTokenRefresher', Refresher)
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        app = cp.build_app(Settings(_env_file=None, atr_simulation_aggregate_receipt_path='/CONTROLLED/startup'),
            session_factory=sessionmaker(engine), firstbar_read_port=Recorder(), redis_client=SimpleNamespace())
        async def scenario():
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
                raise primary
        with pytest.raises(failure_type) as observed:
            asyncio.run(scenario())
        assert observed.value is primary and closed == ['aggregate']
        assert primary.__notes__ == ['shutdown_refresher_failure=ValueError']
    finally:
        engine.dispose()


def test_failed_refresher_never_closes_injected_readers(monkeypatch):
    closed = []
    primary = ValueError('CONTROLLED_REF_FAILURE')
    async def redis_close():
        closed.append('redis')
    monkeypatch.setattr(cp, 'Redis', SimpleNamespace(from_url=lambda *args, **kwargs:
        SimpleNamespace(aclose=redis_close)))
    monkeypatch.setattr(cp, 'ControlPlaneRepository', lambda *args, **kwargs: SimpleNamespace())
    class Refresher:
        def __init__(self, settings):
            pass
        async def run(self):
            raise primary
    monkeypatch.setattr(cp, 'SchwabTokenRefresher', Refresher)
    firstbar = SimpleNamespace(close=lambda: closed.append('injected-firstbar'))
    aggregate = SimpleNamespace(reader=SimpleNamespace(close=lambda: closed.append('injected-aggregate')))
    engine = sa.create_engine('sqlite:///:memory:')
    try:
        app = cp.build_app(Settings(_env_file=None, atr_simulation_aggregate_receipt_path='/CONTROLLED/not-loaded'),
            session_factory=sessionmaker(engine), firstbar_read_port=firstbar,
            aggregate_simulation_read_port=aggregate)
        async def scenario():
            async with app.router.lifespan_context(app):
                await asyncio.sleep(0)
        with pytest.raises(ValueError) as observed:
            asyncio.run(scenario())
        assert observed.value is primary and closed == ['redis']
    finally:
        engine.dispose()
