"""Controlled projection/cleanup boundaries, not actual PG month-load evidence."""

import asyncio
from threading import get_ident

import pytest

from project_mai_tai.services import atr_firstbar_routes as routes
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_firstbar_readonly_mount import Recorder


def test_nested_metadata_refuses_as_unknown_instead_of_escaping_page():
    class Nested(Recorder):
        def read(self, *args, **kwargs):
            stored = super().read(*args, **kwargs)
            nested = []
            stored["deep_metadata"] = nested
            for _ in range(2000):
                child = []
                nested.append(child)
                nested = child
            return stored

    context = asyncio.run(routes.load_firstbar_display(Nested(), source_id="CONTROLLED-nested"))
    assert context["view"]["coverage_status"] == "UNKNOWN"
    assert context["read_status"] == "UNAVAILABLE"
    assert context["view"]["outcomes"] == []


def test_projection_uses_worker_thread_not_page_loop(monkeypatch):
    original = routes.firstbar_display_view
    calls = []

    def tracked(*args, **kwargs):
        calls.append(get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(routes, "firstbar_display_view", tracked)

    async def scenario():
        loop_thread = get_ident()
        reader = Recorder()
        context = await routes.load_firstbar_display(reader, source_id="CONTROLLED-selected")
        assert len(reader.calls) == 1 and reader.calls[0][0] == "CONTROLLED-selected"
        assert context["read_status"] == "UNAVAILABLE"
        assert calls and all(thread != loop_thread for thread in calls)

    asyncio.run(scenario())


def test_owned_reader_closes_even_when_owned_redis_close_fails(monkeypatch):
    closed = []

    class Reader:
        def __init__(self, *args):
            pass

        def close(self):
            closed.append(True)

    class Redis:
        @staticmethod
        def from_url(*args, **kwargs):
            return Redis()

        async def aclose(self):
            raise RuntimeError("CONTROLLED redis close failure")

    class Refresher:
        def __init__(self, *args):
            pass

        async def run(self):
            await asyncio.Event().wait()

    from project_mai_tai.atr_simulation import firstbar_ui_reader

    monkeypatch.setattr(firstbar_ui_reader, "FirstBarSeedUIReader", Reader)
    monkeypatch.setattr(cp, "Redis", Redis)
    monkeypatch.setattr(cp, "SchwabTokenRefresher", Refresher)
    monkeypatch.setattr(cp, "ControlPlaneRepository", lambda *args, **kwargs: object())
    app = cp.build_app(Settings(database_url="sqlite+pysqlite:///:memory:"), session_factory=object())

    async def scenario():
        with pytest.raises(RuntimeError, match="CONTROLLED redis close failure"):
            async with app.router.lifespan_context(app):
                pass

    asyncio.run(scenario())
    assert closed == [True]


def test_unselected_projection_fast_path_reads_no_storage_and_is_local(monkeypatch):
    original = routes.firstbar_display_view
    calls = []

    def tracked(*args, **kwargs):
        calls.append(get_ident())
        return original(*args, **kwargs)

    class ForbiddenReader:
        def read(self, *args, **kwargs):
            raise AssertionError("unselected local UNKNOWN must not read storage")

    monkeypatch.setattr(routes, "firstbar_display_view", tracked)

    async def scenario():
        loop_thread = get_ident()
        context = await routes.load_firstbar_display(ForbiddenReader())
        assert calls == [loop_thread]
        assert context["source_id"] is None
        assert context["read_status"] == "UNAVAILABLE"
        assert context["view"]["coverage_status"] == "UNKNOWN"
        assert context["view"]["outcomes"] == []

    asyncio.run(scenario())
