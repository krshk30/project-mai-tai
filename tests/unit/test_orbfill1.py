import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import StrategyBarHistory
from project_mai_tai.orb_schwab_fill import OrbSchwabMacdFill, fill_window, merge_macd_fill
from project_mai_tai.orb_schwab_macd import MacdVerdict, schwab_completed_bar_macd_gate
from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar, completed_bar_macd_gate

NOW = datetime(2026, 10, 9, 13, 28, tzinfo=UTC)
START, CUTOFF, DEADLINE = fill_window(NOW)


def bar(time, close):
    return OrbBar(timestamp=time, open=close, high=close, low=close, close=close, volume=200)


def candle(time, close):
    return {"datetime": int(time.timestamp() * 1000), "close": close}


def factory(count=3, *, gap=None):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    StrategyBarHistory.__table__.create(engine)
    sf = sessionmaker(engine)
    with sf.begin() as session:
        for i in range(count):
            time = CUTOFF - timedelta(minutes=count - 1 - i)
            if time == gap:
                continue
            session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol="TEST",
                                           interval_secs=60, bar_time=time, open_price=2 + i / 100,
                                           high_price=2 + i / 100, low_price=2 + i / 100,
                                           close_price=2 + i / 100, volume=200))
    return sf, engine


def filler(candles=None, *, clock=None, fetch=None):
    candles = candles if candles is not None else [candle(START, 1), candle(CUTOFF, 2)]
    return OrbSchwabMacdFill(Settings(), clock=clock or (lambda: NOW),
                             fetch=fetch or (lambda *_: {"symbol": "TEST", "empty": False,
                                                         "candles": candles}))


def test_fill_saved_wins_carry_close_and_existing_macd_exact():
    saved = [bar(CUTOFF, 3)]
    source = [candle(START, 1), candle(START + timedelta(minutes=20), 2), candle(CUTOFF, 99),
              candle(START - timedelta(minutes=1), 400), candle(CUTOFF + timedelta(minutes=1), 500)]
    merged = merge_macd_fill(saved, source, START, CUTOFF)
    assert len(merged) == 58
    assert merged[0].close == 1 and merged[19].close == 1 and merged[20].close == 2
    assert merged[-1].close == 3
    assert merged[0].timestamp == START and merged[-1].timestamp == CUTOFF
    assert all(x.volume == 0 for x in merged)
    expected = [bar(START + timedelta(minutes=i), 1 if i < 20 else 2) for i in range(58)]
    expected[-1] = saved[-1]
    assert completed_bar_macd_gate(merged, NOW) == completed_bar_macd_gate(expected, NOW)


def test_gate_fills_but_never_writes_saved_history():
    sf, engine = factory()
    writes = []
    @event.listens_for(engine, "before_cursor_execute")
    def observe(_conn, _cursor, statement, *_):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)
    result = schwab_completed_bar_macd_gate(sf, "TEST", NOW, fill=filler())
    assert result[0] == MacdVerdict.ALLOWED
    with sf() as session:
        rows = session.query(StrategyBarHistory).all()
        assert len(rows) == 3 and float(rows[-1].close_price) == 2.02
        assert all(row.volume == 200 for row in rows)
    assert writes == []


@pytest.mark.parametrize("count", [35, 58, 148])
def test_ge35_is_byte_equivalent_and_no_fetch_even_if_fill_fails(count):
    sf, _ = factory(count)
    def forbidden(*_):
        pytest.fail(">=35 must never fetch")
    baseline = schwab_completed_bar_macd_gate(sf, "TEST", NOW)
    assert schwab_completed_bar_macd_gate(sf, "TEST", NOW, fill=filler(fetch=forbidden)) == baseline


def test_ge35_hole_is_not_repaired():
    sf, _ = factory(40, gap=CUTOFF - timedelta(minutes=7))
    assert schwab_completed_bar_macd_gate(sf, "TEST", NOW, fill=filler())[1] == "missing_schwab_minute"


def test_missing_leading_price_is_not_invented_vivk_shaped(caplog):
    sf, _ = factory()
    f = filler([candle(CUTOFF - timedelta(minutes=29), 2), candle(CUTOFF, 3)])
    assert schwab_completed_bar_macd_gate(sf, "TEST", NOW, fill=f)[1] == "insufficient_schwab_history"
    assert "symbol=TEST result=skipped reason=insufficient_schwab_history" in caplog.text


def test_one_attempt_per_symbol_session_including_timeouts(caplog):
    calls = []
    def timeout(*args):
        calls.append(args)
        raise TimeoutError
    f = filler(fetch=timeout)
    for now in (NOW, NOW + timedelta(minutes=1)):
        with pytest.raises(ValueError, match="unavailable"):
            f.supplement("TEST", [], now)
    assert len(calls) == 1 and calls[0] == ("TEST", START, CUTOFF, 2.0)
    with pytest.raises(ValueError):
        f.supplement("OTHER", [], NOW)
    f.clock = lambda: NOW + timedelta(days=1)
    with pytest.raises(ValueError):
        f.supplement("TEST", [], NOW + timedelta(days=1))
    assert len(calls) == 3
    assert "reason=fetch_timeout" in caplog.text and "reason=schwab_fill_unavailable" in caplog.text


def test_cached_outside_candles_never_adopted_later(caplog):
    f = filler([candle(START, 2), candle(CUTOFF, 3), candle(CUTOFF + timedelta(minutes=1), 900)])
    first = f.supplement("TEST", [], NOW)
    with pytest.raises(ValueError, match="cached_cutoff_unproven"):
        f.supplement("TEST", [], NOW + timedelta(minutes=1))
    assert first[-1].close == 3 and "reason=cached_cutoff_unproven" in caplog.text


@pytest.mark.parametrize("payload", [None, {}, {"symbol": "FOREIGN", "empty": False, "candles": []},
                                      {"symbol": "TEST", "empty": False, "candles": []},
                                      {"symbol": "TEST", "empty": True, "candles": [candle(START, 1)]},
                                      {"symbol": "TEST", "empty": False, "nextToken": "x",
                                       "candles": [candle(START, 1)]}])
def test_unproven_envelope_fail_closed(payload):
    with pytest.raises(ValueError):
        filler(fetch=lambda *_: payload).supplement("TEST", [], NOW)


@pytest.mark.parametrize("source", [[candle(START, 0)], [candle(START, float("nan"))],
                                    [candle(START, 1), candle(START, 2)],
                                    [{"datetime": True, "close": 2}],
                                    [candle(START + timedelta(seconds=1), 2)]])
def test_invalid_provider_close_or_timestamp_fail_closed(source):
    with pytest.raises(ValueError):
        filler(source).supplement("TEST", [], NOW)


@pytest.mark.parametrize("now", [DEADLINE, DEADLINE + timedelta(microseconds=1)])
def test_deadline_no_request(now):
    calls = []
    f = filler(clock=lambda: now, fetch=lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="deadline"):
        f.supplement("TEST", [], now)
    assert calls == []


def test_fetch_completed_after_deadline_never_authorizes(caplog):
    clock = [NOW]
    def late(*_):
        clock[0] = DEADLINE
        return {"symbol": "TEST", "empty": False, "candles": [candle(START, 2)]}
    with pytest.raises(ValueError, match="deadline"):
        filler(clock=lambda: clock[0], fetch=late).supplement("TEST", [], NOW)
    assert "reason=fill_deadline" in caplog.text


def test_http_exact_bound_timeout_read_token_only(tmp_path, monkeypatch):
    from project_mai_tai import orb_schwab_fill as module
    token = tmp_path / "token.json"
    token.write_text('{"access_token":"controlled-not-live"}')
    before = token.read_bytes()
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, limit):
            assert limit == 1_048_577
            return b'{"symbol":"TEST","empty":false,"candles":[]}'
    monkeypatch.setattr(module, "urlopen", lambda request, timeout:
                        calls.append((request.full_url, request.get_method(), timeout)) or Response())
    settings = Settings(schwab_token_store_path=str(token))
    OrbSchwabMacdFill(settings)._fetch("TEST", START, CUTOFF, 1.25)
    from urllib.parse import parse_qs, urlparse
    url, method, timeout = calls[0]
    params = parse_qs(urlparse(url).query)
    assert params["startDate"] == [str(int(START.timestamp() * 1000))]
    assert params["endDate"] == [str(int((CUTOFF + timedelta(minutes=1)).timestamp() * 1000) - 1)]
    assert params["needExtendedHoursData"] == ["true"] and method == "GET" and timeout == 1.25
    assert token.read_bytes() == before


def test_actual_service_decision_worker_offloop_and_no_late_place(monkeypatch):
    sf, _ = factory()
    service = OrbSchwabService(settings=Settings(orb_schwab_macd_fill_enabled=True,
                               orb_live_schwab_orders_enabled=True, orb_schwab_atr_entry_gate_enabled=False),
                               session_factory=sf)
    clock = [NOW]
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    opening = NOW.replace(minute=30)
    monkeypatch.setattr(service, "_session_open_utc", lambda: opening)
    service._universe = {"TEST"}
    loop_thread = threading.get_ident()
    calls = []
    def fetch(*_):
        assert threading.get_ident() != loop_thread
        calls.append("fetch")
        clock[0] = DEADLINE
        return {"symbol": "TEST", "empty": False, "candles": [candle(START, 1), candle(CUTOFF, 3)]}
    service._macd_fill.fetch = fetch
    service._macd_fill.clock = lambda: NOW  # Exercise producer's independent post-worker cutoff.
    emitted = []
    async def publish(*args):
        emitted.append(args)
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", publish)
    for minute in (25, 26, 27):
        service._on_bar("TEST", bar(NOW.replace(minute=minute), 3), observed_at=NOW)
    asyncio.run(service._process_closed_bars())
    assert calls == ["fetch"] and emitted == []
    assert service._opening_orders["TEST"].cancelled
    assert service._states == {}


def test_earlier_saved_close_can_anchor_without_later_price_lookahead():
    saved = [bar(START - timedelta(minutes=1), 2)]
    merged = merge_macd_fill(saved, [candle(START + timedelta(minutes=5), 10)], START, CUTOFF)
    assert merged[0] is saved[0]
    assert all(x.close == 2 for x in merged[1:6])
    assert merged[6].close == 10


def test_actual_service_atr_worker_cannot_extend_entry_deadline(monkeypatch):
    from types import SimpleNamespace
    sf, _ = factory()
    service = OrbSchwabService(settings=Settings(orb_schwab_macd_fill_enabled=True,
                                                orb_live_schwab_orders_enabled=True), session_factory=sf)
    clock = [NOW]
    service._universe = {"TEST"}
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    monkeypatch.setattr(service, "_session_open_utc", lambda: NOW.replace(minute=30))
    service._macd_fill = filler()
    def late_atr(*_):
        clock[0] = DEADLINE
        return SimpleNamespace(verdict="allowed", evidence=lambda: {})
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.schwab_atr_entry_gate", late_atr)
    emitted = []
    async def publish(*args): emitted.append(args)
    monkeypatch.setattr("project_mai_tai.services.orb_schwab_app.publish_orb_schwab_intent", publish)
    for minute in (25, 26, 27):
        service._on_bar("TEST", bar(NOW.replace(minute=minute), 3), observed_at=NOW)
    asyncio.run(service._process_closed_bars())
    assert emitted == [] and service._opening_orders["TEST"].cancelled


def test_quote_handler_does_not_fetch_or_query(monkeypatch):
    service = OrbSchwabService(settings=Settings(orb_schwab_macd_fill_enabled=True),
                               session_factory=lambda: pytest.fail("quote SQL forbidden"))
    service._last_gateway_symbols = ["TEST"]
    service._macd_fill.fetch = lambda *_: pytest.fail("quote HTTP forbidden")
    service._handle_market_data({"data": json.dumps({"event_type": "quote_tick",
                               "payload": {"symbol": "TEST"}})})
    assert service._macd_fill._attempts == {}


@pytest.mark.parametrize("cancel", [False, True])
def test_physical_held_fetch_is_bounded_retained_and_quote_handler_runs(monkeypatch, caplog, cancel):
    from types import SimpleNamespace
    sf, _ = factory()
    service = OrbSchwabService(settings=Settings(orb_schwab_macd_fill_enabled=True), session_factory=sf)
    clock = [DEADLINE - timedelta(milliseconds=50)]
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    service._macd_fill.clock = lambda: clock[0]
    entered, release = threading.Event(), threading.Event()
    calls, quotes = [], []
    def held(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return {"symbol": "TEST", "empty": False, "candles": [candle(START, 1), candle(CUTOFF, 2)]}
    service._macd_fill.fetch = held
    service._last_gateway_symbols = ["TEST"]
    service._aggregators["TEST"] = SimpleNamespace(flush_before=lambda _: quotes.append("quote"))
    async def run():
        request = asyncio.create_task(service._completed_macd_gate("TEST", NOW))
        try:
            await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
            service._handle_market_data({"data": json.dumps({"event_type": "quote_tick",
                                      "payload": {"symbol": "TEST"}})})
            assert quotes == ["quote"]
            if cancel:
                request.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request
            else:
                result = await asyncio.wait_for(request, timeout=1)
                assert result[1] == "decision_worker_timeout"
                assert "reason=decision_worker_timeout" in caplog.text
                assert (await service._completed_macd_gate("TEST", NOW))[1] == "decision_worker_timeout"
            retained = next(iter(service._macd_fill_tasks.values()))
            assert not retained.done() and not retained.cancelled() and len(calls) == 1
            clock[0] = DEADLINE
            assert (await service._completed_macd_gate("TEST", NOW))[1] == "fill_deadline"
        finally:
            release.set()
            await asyncio.gather(*service._macd_fill_tasks.values())
    asyncio.run(run())
    assert len(calls) == 1


def test_flag_default_off_catalog_true():
    assert Settings().orb_schwab_macd_fill_enabled is False
    path = Path(__file__).resolve().parents[2] / "ops/health/expected_flags.json"
    flags = json.loads(path.read_text())["flags"]
    flag = next(x for x in flags if x["name"] == "orb_schwab_macd_fill_enabled")
    assert flag["expected"] is True and flag["owning_service"] == "orb-schwab"


def test_first0925_response_cannot_invent0927_cutoff_and_never_refetch():
    first = NOW.replace(minute=25)
    later = NOW.replace(minute=27)
    sf, _ = factory(0)
    calls = []
    def fetch(*args):
        calls.append(args)
        return {"symbol": "TEST", "empty": False, "candles": [candle(START, 1), candle(args[2], 2)]}
    f = filler(fetch=fetch)
    assert schwab_completed_bar_macd_gate(sf, "TEST", first, fill=f)[0] == MacdVerdict.ALLOWED
    assert schwab_completed_bar_macd_gate(sf, "TEST", later, fill=f) == (
        MacdVerdict.BAR_NOT_YET, "cached_cutoff_unproven", None)
    assert len(calls) == 1 and calls[0][2] == NOW.replace(minute=24)
    # Only real newly saved minutes can extend the once-fetched response.
    for minute in (25, 26):
        with sf.begin() as session:
            session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol="TEST", interval_secs=60,
                bar_time=NOW.replace(minute=minute), open_price=2, high_price=2, low_price=2,
                close_price=2, volume=100))
    assert schwab_completed_bar_macd_gate(sf, "TEST", later, fill=f)[0] == MacdVerdict.ALLOWED
    assert len(calls) == 1


def test_timedout_old_minute_task_cannot_authorize_new_cutoff(monkeypatch):
    sf, _ = factory(0)
    service = OrbSchwabService(settings=Settings(orb_schwab_macd_fill_enabled=True), session_factory=sf)
    first, later = NOW.replace(minute=25), NOW.replace(minute=27)
    clock = [first]
    monkeypatch.setattr(service, "_processing_time", lambda: clock[0])
    service._macd_fill.clock = lambda: clock[0]
    entered, release = threading.Event(), threading.Event()
    calls = []
    def held(*args):
        calls.append(args)
        entered.set()
        assert release.wait(5)
        return {"symbol": "TEST", "empty": False, "candles": [candle(START, 1), candle(args[2], 2)]}
    service._macd_fill.fetch = held
    async def run():
        try:
            request = asyncio.create_task(service._completed_macd_gate("TEST", first))
            await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=1)
            assert (await asyncio.wait_for(request, timeout=3))[1] == "decision_worker_timeout"
            clock[0] = later
            assert (await service._completed_macd_gate("TEST", later))[1] == "fill_worker_pending"
            retained = next(iter(service._macd_fill_tasks.values()))
            release.set()
            assert (await retained)[0] == MacdVerdict.ALLOWED
            # Completed old result is NOT directly reused at 09:27.
            result = await service._completed_macd_gate("TEST", later)
            assert result == (MacdVerdict.BAR_NOT_YET, "cached_cutoff_unproven", None)
            assert len(calls) == 1 and service._macd_fill_tasks == {}
        finally:
            release.set()
            await asyncio.gather(*service._macd_fill_tasks.values())
    asyncio.run(run())


def test_completed_old_day_tasks_cleaned_but_physical_work_not_cancelled():
    sf, _ = factory(35)
    service = OrbSchwabService(settings=Settings(orb_schwab_macd_fill_enabled=True), session_factory=sf)
    async def run():
        release = asyncio.Event()
        completed = asyncio.create_task(asyncio.sleep(0))
        pending = asyncio.create_task(release.wait())
        await completed
        old_start = START - timedelta(days=1)
        service._macd_fill_tasks[("DONE", old_start, CUTOFF)] = completed
        service._macd_fill_tasks[("PENDING", old_start, CUTOFF)] = pending
        result = await service._completed_macd_gate("TEST", NOW)
        assert result == schwab_completed_bar_macd_gate(sf, "TEST", NOW)
        assert list(service._macd_fill_tasks.values()) == [pending] and not pending.cancelled()
        release.set()
        await pending
    asyncio.run(run())


@pytest.mark.parametrize("foreign_time", [START - timedelta(days=1), CUTOFF + timedelta(minutes=1)])
def test_raw_nonempty_foreign_window_cannot_fill_from_saved_anchor(foreign_time, caplog):
    sf, _ = factory(0)
    with sf.begin() as session:
        session.add(StrategyBarHistory(strategy_code="schwab_1m_v2", symbol="TEST", interval_secs=60,
            bar_time=START - timedelta(minutes=1), open_price=2, high_price=2, low_price=2,
            close_price=2, volume=100))
    calls = []
    def foreign(*args):
        calls.append(args)
        return {"symbol": "TEST", "empty": False, "candles": [candle(foreign_time, 99)]}
    f = filler(fetch=foreign)
    for _ in range(2):
        assert schwab_completed_bar_macd_gate(sf, "TEST", NOW, fill=f) == (
            MacdVerdict.BAR_NOT_YET, "schwab_fill_unavailable", None)
    assert len(calls) == 1
    assert "symbol=TEST result=skipped reason=empty_scoped_schwab_fill" in caplog.text
