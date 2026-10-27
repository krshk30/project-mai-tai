"""Controlled response construction, not a quiet-host latency measurement."""

import asyncio
from threading import Event, get_ident

from fastapi import FastAPI
import httpx
import pytest

from project_mai_tai.services import atr_firstbar_routes, atr_simulation_legacy_routes


@pytest.mark.parametrize("kind", ["firstbar", "legacy"])
def test_sim_json_response_encoding_does_not_block_other_requests(monkeypatch, kind):
    module = atr_firstbar_routes if kind == "firstbar" else atr_simulation_legacy_routes
    # Exercise the actual encoder, not an obsolete response-class import.
    encoder = "JSONResponse" if kind == "firstbar" else "_EncodedLegacyView"
    original = getattr(module, encoder)
    started, release, threads = Event(), Event(), []

    def held_response(*args, **kwargs):
        threads.append(get_ident())
        started.set()
        assert release.wait(3), "controlled encoder was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(module, encoder, held_response)
    app = FastAPI()
    class Reader:
        def __init__(self):
            self.calls = []

        def read(self, *args, **kwargs):
            from project_mai_tai.atr_simulation.ui_reader import unavailable_storage
            self.calls.append(get_ident())
            return unavailable_storage("CONTROLLED_SELECTED_NO_PUBLISHED_SOURCE")

    reader = Reader()
    router = (module.build_firstbar_router(None, render_row=lambda row: "")
        if kind == "firstbar" else module.build_legacy_router(reader))
    app.include_router(router)

    @app.get("/unrelated")
    async def unrelated():
        return {"status": "readable"}

    async def scenario():
        loop_thread = get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                base_url="http://controlled") as client:
            path = ("/atr-firstbar/api" if kind == "firstbar"
                else "/atr-simulation/api?source_id=CONTROLLED-selected")
            task = asyncio.create_task(client.get(path))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                response = await asyncio.wait_for(client.get("/unrelated"), 2)
                assert response.status_code == 200 and not task.done()
                assert threads and all(thread != loop_thread for thread in threads)
            finally:
                release.set()
                response = await task
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["coverage_status"] == "UNKNOWN"
            if kind == "legacy":
                assert len(reader.calls) == 1 and reader.calls[0] != loop_thread

    asyncio.run(scenario())


def test_selected_legacy_encoder_failure_still_encodes_unknown_offloop(monkeypatch):
    module = atr_simulation_legacy_routes
    original = module._EncodedLegacyView
    threads = []

    def fail_first(value):
        threads.append(get_ident())
        if len(threads) == 1:
            raise TypeError("CONTROLLED selected encoding failure")
        return original(value)

    monkeypatch.setattr(module, "_EncodedLegacyView", fail_first)
    app = FastAPI()
    app.include_router(module.build_legacy_router(None))

    async def scenario():
        loop_thread = get_ident()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                base_url="http://controlled") as client:
            response = await client.get("/atr-simulation/api?source_id=CONTROLLED-selected")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["coverage_status"] == "UNKNOWN"
        assert response.json()["read_status"] == "UNAVAILABLE"
        assert response.json()["read_issues"] == ["LEGACY_SIMULATION_MALFORMED_VIEW"]
        assert len(threads) == 2 and all(thread != loop_thread for thread in threads)

    asyncio.run(scenario())
