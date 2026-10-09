import asyncio
import logging
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from project_mai_tai.db.models import DashboardSnapshot, StrategyBarHistory
from project_mai_tai.orb_live_decisions import LiveDecisionTape, QUEUE_LIMIT, SNAPSHOT_TYPE, read_decisions
from project_mai_tai.orb_schwab_macd import MacdVerdict
from project_mai_tai.services import control_plane, orb_schwab_app
from project_mai_tai.services.orb_schwab_app import OrbSchwabService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.orb_intrabar import OrbBar
from tests.unit.test_control_plane import FakeLegacyClient, FakeRedis, build_test_session_factory


DAY = datetime(2026, 10, 9, tzinfo=UTC)
OPEN = DAY.replace(hour=13, minute=30)
# Actual orb-schwab.log October9:11 evaluations, including repeated attempts.
RECORDED = [
    ("13:29:03.322", "VEEA", 28, "allowed", 8.188300878601493e-05, "no_action"),
    ("13:29:30.399", "VIVK", 27, "bar_not_yet", None, "bar_missing"),
    ("13:29:30.403", "VIVK", 27, "bar_not_yet", None, "bar_missing"),
    ("13:30:03.351", "VEEA", 29, "negative", -0.008041293157427156, "macd_negative_no_entry"),
    ("13:30:03.413", "VEEA", 29, "negative", -0.008041293157427156, "macd_negative_no_entry"),
    ("13:30:31.009", "VIVK", 28, "bar_not_yet", None, "bar_missing"),
    ("13:30:31.016", "VIVK", 28, "bar_not_yet", None, "bar_missing"),
    ("13:30:31.024", "VIVK", 28, "bar_not_yet", None, "bar_missing"),
    ("13:30:31.033", "VIVK", 28, "bar_not_yet", None, "bar_missing"),
    ("13:31:30.698", "VIVK", 29, "bar_not_yet", None, "bar_missing"),
    ("13:31:30.703", "VIVK", 29, "bar_not_yet", None, "bar_missing"),
]


def service(factory):
    return OrbSchwabService(settings=Settings(
        _env_file=None, orb_enabled=True, orb_live_schwab_orders_enabled=True,
        orb_schwab_atr_entry_gate_enabled=False,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_broker_provider="schwab",
    ), session_factory=factory)


def bar(minute=27):
    return OrbBar(timestamp=DAY.replace(hour=13, minute=minute),
                  open=5, high=5, low=5, close=5, volume=100, breakout_high=5)


async def drain(tape):
    task = asyncio.create_task(tape.run())
    try:
        await asyncio.wait_for(tape.queue.join(), 2)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


def test_recorded_eleven_decisions_db_to_actual_page_and_api(monkeypatch):
    factory = build_test_session_factory()
    bot = service(factory)
    bot._live_decision_tape.session_factory = factory
    for stamp, symbol, minute, verdict, hist, reason in RECORDED:
        at = datetime.fromisoformat(f"2026-10-09T{stamp}+00:00")
        bot._record_live_decision(symbol, bar(minute), at, MacdVerdict(verdict), hist,
                                  "none", reason, "not_evaluated")
    # The ATR refusal is a separate actual branch, not claimed among the11 log lines.
    bot._record_live_decision("VEEA", bar(), OPEN, MacdVerdict.ALLOWED, 0.1,
                              "none", "atr_below_line", "blocked")
    asyncio.run(drain(bot._live_decision_tape))
    monkeypatch.setattr(control_plane, "utcnow", lambda: OPEN + timedelta(minutes=3))
    app = control_plane.build_app(
        settings=bot.settings, session_factory=factory,
        redis_client=FakeRedis({}), legacy_client=FakeLegacyClient(),
    )
    with TestClient(app) as client:
        result = client.get("/api/bot/orb-schwab").json()
        assert len(result["recent_decisions"]) == 12
        rows = result["recent_decisions"]
        assert sum(row["reason"] == "bar_missing" for row in rows) == 8
        assert sum(row["reason"] == "macd_negative_no_entry" for row in rows) == 2
        assert rows[0]["strategy_code"] == "orb_schwab"
        assert {row["action"] for row in rows} == {"none"}
        html = client.get("/bot/orb").text
        assert "No recent decision-tape events" not in html
        assert "atr_below_line" in html and "macd_negative_no_entry" in html and "bar_missing" in html
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(StrategyBarHistory)) == 0
        assert session.scalar(select(func.count()).select_from(DashboardSnapshot)
                              .where(DashboardSnapshot.snapshot_type == "orb_paper_events")) == 0


@pytest.mark.parametrize("kind", ["normal", "cutoff", "missing", "atr"])
def test_runtime_enqueues_every_decision_branch(monkeypatch, kind):
    bot = service(lambda: None)
    bot._universe = {"VEEA"}
    monkeypatch.setattr(bot, "_session_open_utc", lambda: OPEN)
    now = OPEN - timedelta(minutes=2)
    verdict = MacdVerdict.NEGATIVE
    if kind == "cutoff":
        now = OPEN - timedelta(seconds=30)
    elif kind == "missing":
        verdict, now = MacdVerdict.BAR_NOT_YET, OPEN
    elif kind == "atr":
        bot.settings.orb_schwab_atr_entry_gate_enabled = True
        verdict = MacdVerdict.ALLOWED
        monkeypatch.setattr(orb_schwab_app, "schwab_atr_entry_gate", lambda *_: SimpleNamespace(
            verdict="blocked", reason="below_line", evidence=lambda: {}))
    monkeypatch.setattr(bot, "_processing_time", lambda: now)
    monkeypatch.setattr(orb_schwab_app, "schwab_completed_bar_macd_gate",
                        lambda *_: (verdict, "negative", -0.1))
    bot._closed_bars = [("VEEA", bar(), now)]
    asyncio.run(bot._process_closed_bars())
    row = bot._live_decision_tape.queue.get_nowait()
    assert row["symbol"] == "VEEA" and row["macd_verdict"] == verdict.value
    assert row["bar_at"] == "2026-10-09T13:27:00+00:00"
    assert row["evaluated_at"] == now.isoformat()
    assert row["reason"] == {"normal": "no_action", "cutoff": "macd_negative_no_entry",
                             "missing": "bar_missing", "atr": "atr_below_line"}[kind]


def test_db_failure_and_bounded_queue_never_block_decision(monkeypatch, caplog):
    def unavailable():
        raise RuntimeError("database down")
    bot = service(unavailable)
    bot._live_decision_tape.session_factory = unavailable
    monkeypatch.setattr(bot, "_session_open_utc", lambda: OPEN)
    monkeypatch.setattr(bot, "_processing_time", lambda: OPEN)
    monkeypatch.setattr(orb_schwab_app, "schwab_completed_bar_macd_gate",
                        lambda *_: (MacdVerdict.BAR_NOT_YET, "missing", None))
    bot._closed_bars = [("VIVK", bar(), OPEN)]
    caplog.set_level(logging.WARNING, logger="orb-schwab")
    asyncio.run(bot._process_closed_bars())
    assert bot._opening_orders["VIVK"].cancelled is True
    asyncio.run(drain(bot._live_decision_tape))
    assert "write_failed count=1 error=RuntimeError" in caplog.text
    for _ in range(QUEUE_LIMIT + 10):
        bot._record_live_decision("VIVK", bar(), OPEN, MacdVerdict.BAR_NOT_YET,
                                  None, "none", "bar_missing", "not_evaluated")
    assert bot._live_decision_tape.queue.qsize() == QUEUE_LIMIT
    assert "dropped reason=queue_full" in caplog.text


def test_slow_physical_writer_is_offloop_without_replacement_workers(monkeypatch):
    release, entered = threading.Event(), threading.Event()
    tape = LiveDecisionTape()
    calls = []
    main_thread = threading.get_ident()
    def slow(batch):
        assert threading.get_ident() != main_thread
        calls.append(len(batch))
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(tape, "_persist", slow)
    async def exercise():
        tape.offer({"symbol": "VEEA"})
        worker = asyncio.create_task(tape.run())
        try:
            while not entered.is_set():
                await asyncio.sleep(0.001)
            for _ in range(100):
                tape.offer({"symbol": "VIVK"})
                await asyncio.sleep(0)
            assert calls == [1] and tape.queue.qsize() == 100
            release.set()
            await asyncio.wait_for(tape.queue.join(), 2)
        finally:
            release.set()
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker
    asyncio.run(exercise())


def test_broken_tape_does_not_change_actual_live_place(monkeypatch):
    bot = service(lambda: None)
    monkeypatch.setattr(bot, "_session_open_utc", lambda: OPEN)
    now = OPEN - timedelta(minutes=2)
    monkeypatch.setattr(bot, "_processing_time", lambda: now)
    monkeypatch.setattr(orb_schwab_app, "schwab_completed_bar_macd_gate",
                        lambda *_: (MacdVerdict.ALLOWED, "nonnegative", 0.1))
    monkeypatch.setattr(bot._live_decision_tape, "offer",
                        lambda _: (_ for _ in ()).throw(RuntimeError("down")))
    from project_mai_tai.strategy_core.orb_schwab_open import OrbSchwabOpeningOrder
    order = OrbSchwabOpeningOrder(OPEN)
    order.bars = {bar(25).timestamp: bar(25), bar(26).timestamp: bar(26)}
    bot._opening_orders["VEEA"] = order
    bot._closed_bars = [("VEEA", bar(), now)]
    sent = []
    async def capture(_redis, _settings, intent, _now):
        sent.append(intent)
    monkeypatch.setattr(orb_schwab_app, "publish_orb_schwab_intent", capture)
    asyncio.run(bot._process_closed_bars())
    assert len(sent) == 1
    assert sent[0].payload.intent_type == "open"
    assert sent[0].payload.reason == "ORB_FIXED_HIGH_SCHWAB_STOP_LIMIT"


def test_persistent_tape_bounded_scoped_and_only_today_visible():
    factory = build_test_session_factory()
    bot = service(factory)
    tape = bot._live_decision_tape
    tape.session_factory = factory
    with factory() as session:
        session.add(DashboardSnapshot(snapshot_type="unrelated", payload={"kept": True},
                                      created_at=DAY - timedelta(days=20)))
        session.add(DashboardSnapshot(snapshot_type=SNAPSHOT_TYPE, payload={"old": True},
                                      created_at=DAY - timedelta(days=20)))
        session.commit()
    for index in range(80):
        bot._record_live_decision("VEEA", bar(), OPEN + timedelta(seconds=index),
                                  MacdVerdict.NEGATIVE, -0.1, "none", str(index), "disabled")
    asyncio.run(drain(tape))
    with factory() as session:
        rows = read_decisions(session, DAY, DAY + timedelta(days=1))
        assert len(rows) == 50 and rows[0]["reason"] == "79" and rows[-1]["reason"] == "30"
        assert session.scalar(select(func.count()).select_from(DashboardSnapshot)) == 51


def test_observer_exception_and_observe_only_are_inert(monkeypatch, caplog):
    bot = service(lambda: None)
    monkeypatch.setattr(bot._live_decision_tape, "offer",
                        lambda _: (_ for _ in ()).throw(RuntimeError("broken observer")))
    bot._record_live_decision("VEEA", bar(), OPEN, MacdVerdict.ALLOWED, 0.1, "place", "open", "allowed")
    assert "enqueue_failed" in caplog.text
    bot._observe_only = True
    caplog.clear()
    bot._record_live_decision("VEEA", bar(), OPEN, MacdVerdict.ALLOWED, 0.1, "place", "open", "allowed")
    assert "enqueue_failed" not in caplog.text
