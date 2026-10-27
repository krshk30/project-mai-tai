"""Controlled mount and concurrency checks, not actual PG/timing qualification."""

import asyncio
from copy import deepcopy
from datetime import UTC, date, datetime
from threading import Event, get_ident
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from project_mai_tai.services import atr_aggregate_routes as routes
from project_mai_tai.services import control_plane as cp
from project_mai_tai.settings import Settings
from tests.unit.test_atr_simulation_aggregate_entry_publication import operands
from tests.unit.test_atr_simulation_aggregate_entry_read_model import bound, independent, port
from tests.unit.test_atrpnl1 import fill, page_data
from tests.unit.test_firstbar_readonly_mount import Recorder as FirstbarRecorder


class Recorder:
    def __init__(self, stored):
        self.stored = stored
        self.calls = []

    def read(self, source_id, start, end, *, limit):
        self.calls.append((source_id, start, end, limit, get_ident()))
        return deepcopy(self.stored)


@pytest.fixture
def configured(operands):
    receipt, producer = operands
    binding = bound(independent(receipt), producer)
    reader = Recorder(port(binding))
    return routes.AggregateSimulationReadPort(reader, binding)


def app_for(configured):
    app = FastAPI()
    app.include_router(routes.build_aggregate_router(configured,
        render_row=cp._render_completed_position_row))
    return app


def test_default_app_has_no_aggregate_database_or_source():
    assert cp.app.state.aggregate_simulation_read_port is None
    with TestClient(app_for(None)) as client:
        response = client.get('/atr-aggregate')
    assert response.status_code == 200 and 'UNKNOWN' in response.text
    assert routes.TITLE in response.text


def test_source_omission_never_reads_even_when_configured(configured):
    context = asyncio.run(routes.load_aggregate_display(configured))
    assert context['view'] is None and context['read_status'] == 'UNAVAILABLE'
    assert configured.reader.calls == []


def test_foreign_source_cannot_select_another_stored_run(configured):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(routes.load_aggregate_display(configured, source_id='aggregate-entry:' + 'f' * 64))
    assert caught.value.status_code == 400 and configured.reader.calls == []


@pytest.mark.parametrize('query', ('days=0', 'days=33', 'day=bad', 'source_id=bad/path'))
def test_bad_query_is_refused_before_read(configured, query):
    with TestClient(app_for(configured)) as client:
        assert client.get('/atr-aggregate/api?' + query).status_code == 422
    assert configured.reader.calls == []


def test_outside_seed_is_refused_before_read(configured):
    with pytest.raises(HTTPException):
        asyncio.run(routes.load_aggregate_display(configured,
            source_id=configured.binding.source_id, day=date(2026, 10, 23), days=2))
    assert configured.reader.calls == []


def test_actual_projector_and_read_are_offloop_with_exact_scope(configured, monkeypatch):
    loop_thread = get_ident()
    projector_threads = []
    original = routes.aggregate_entry_display_view
    def project(*args, **kwargs):
        projector_threads.append(get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(routes, 'aggregate_entry_display_view', project)
    context = asyncio.run(routes.load_aggregate_display(configured,
        source_id=configured.binding.source_id))
    assert context['read_status'] == 'AVAILABLE' and len(context['view']['rows']) == 253
    assert configured.reader.calls[0][:4] == (configured.binding.source_id,
        date(2026, 9, 22), date(2026, 10, 23), 10000)
    assert configured.reader.calls[0][4] != loop_thread
    assert projector_threads and all(value != loop_thread for value in projector_threads)


def test_json_and_html_use_same_bound_rows_and_live_columns(configured):
    with TestClient(app_for(configured)) as client:
        query = '?source_id=' + configured.binding.source_id
        api = client.get('/atr-aggregate/api' + query)
        html = client.get('/atr-aggregate' + query)
        assert client.post('/atr-aggregate').status_code == 405
        assert client.post('/atr-aggregate/api').status_code == 405
    assert api.status_code == html.status_code == 200
    assert api.headers['cache-control'] == html.headers['cache-control'] == 'no-store'
    view = api.json()['view']
    assert len(view['rows']) == 253 and view['actual_realized_pnl_included'] is False
    assert view['known_pnl_label'] == '$+12,650.00'
    assert routes.TITLE in html.text and cp._COMPLETED_POSITION_COLUMNS in html.text
    assert 'not actual realized' in html.text and 'Shown 25 of 253' in html.text


@pytest.mark.parametrize('failure', ('read-error', 'missing-row', 'foreign-run'))
def test_unavailable_or_invalid_evidence_never_becomes_zero_pnl(configured, failure):
    if failure == 'read-error':
        def fail(*args, **kwargs):
            raise OSError('controlled read error')
        configured.reader.read = fail
    elif failure == 'missing-row':
        configured.reader.stored['rows'].pop()
    else:
        configured.reader.stored['run']['input_sha256'] = 'f' * 64
    context = asyncio.run(routes.load_aggregate_display(configured,
        source_id=configured.binding.source_id))
    assert context['view']['read_status'] != 'AVAILABLE'
    assert context['view']['rows'] == [] and context['view']['known_pnl_label'] == 'UNKNOWN'


def test_slow_aggregate_read_does_not_block_unrelated_http(configured):
    started, release = Event(), Event()
    original = configured.reader.read
    def blocking(*args, **kwargs):
        started.set()
        assert release.wait(4)
        return original(*args, **kwargs)
    configured.reader.read = blocking
    async def scenario():
        app = app_for(configured)
        @app.get('/unrelated')
        async def unrelated():
            return {'ok': True}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://controlled') as client:
            task = asyncio.create_task(client.get('/atr-aggregate/api?source_id=' + configured.binding.source_id))
            try:
                assert await asyncio.to_thread(started.wait, 2)
                assert (await asyncio.wait_for(client.get('/unrelated'), 2)).status_code == 200
                assert not task.done()
            finally:
                release.set()
                assert (await task).status_code == 200
    asyncio.run(scenario())


def test_shared_atr_mount_preserves_realized_live_rows_and_input(configured, monkeypatch):
    monkeypatch.setattr(cp, 'utcnow', lambda: datetime(2026, 10, 23, 20, tzinfo=UTC))
    data = page_data([fill('buy', 10, 4, 'CONTROLLED-entry'),
        fill('sell', 10, 5, 'CONTROLLED-entry-ocoexit-native')])
    before = deepcopy(data)
    original_render = cp._render_bot_detail_page
    preserved = []
    def observed_render(snapshot, strategy_code, **kwargs):
        rendered = original_render(snapshot, strategy_code, **kwargs)
        fragment = kwargs.get('firstbar_simulation_html', '')
        baseline = original_render(snapshot, strategy_code,
            **dict(kwargs, firstbar_simulation_html=''))
        assert fragment and rendered.count(fragment) == 1
        assert rendered.replace(fragment, '', 1).encode() == baseline.encode()
        assert '$+10.00' in baseline
        preserved.append(True)
        return rendered
    monkeypatch.setattr(cp, '_render_bot_detail_page', observed_render)
    engine = sa.create_engine('sqlite:///:memory:')
    app = cp.build_app(Settings(_env_file=None, trade_coach_enabled=False, bot_control_enabled=False),
        session_factory=sessionmaker(engine), firstbar_read_port=FirstbarRecorder(),
        aggregate_simulation_read_port=configured)
    async def dashboard():
        return deepcopy(data)
    app.state.repository = SimpleNamespace(load_bot_dashboard_data=dashboard)
    async def request():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://controlled') as client:
            return await client.get('/bot/1m-schwab-v2', params={
                'aggregate_source_id': configured.binding.source_id})
    try:
        response = asyncio.run(request())
        assert response.status_code == 200
        assert 'data-aggregate-entry-sim="true"' in response.text
        assert '<span>Live replay</span>' in response.text
        assert '<h3>FIRSTBAR</h3>' in response.text and 'partial timing UNKNOWN' in response.text
        assert '$+12,650.00' in response.text and data == before
        assert 'data-pnl-label="$+12,650.00"' in response.text
        assert '<span>Live replay</span><strong>+$12,650.00</strong>' in response.text
        assert '$+10.00' in response.text
        assert preserved == [True]
    finally:
        engine.dispose()
