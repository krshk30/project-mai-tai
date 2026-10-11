"""Composed Sunday v2 candidate: real callbacks, offline delivery boundaries."""

import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.models import Base, BrokerAccount, Strategy
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, Quote
from project_mai_tai.settings import Settings
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook
from project_mai_tai.v2_removed_wait import RemovedWait, RemovedWaitStore
from tests.unit.test_line_candidate_live_set import ROWS


PROCESS = json.loads((Path(__file__).parents[1] / "fixtures"
                      / "sunday_v2_live_flags_20261011.json").read_text())
CANDIDATE = {
    key.removeprefix("MAI_TAI_").lower(): int(value) if key.endswith("_MAX_RETRIES")
    else value == "true"
    for key, value in PROCESS["flags"].items()
}
NEW_FLAGS = (
    "strategy_schwab_1m_v2_removed_wait_clear_rules_enabled",
    "strategy_schwab_1m_v2_fresh_sell_owner_release_enabled",
    "strategy_schwab_1m_v2_gap_trade_conditions_filter_enabled",
)
CANDIDATE.update(dict.fromkeys(NEW_FLAGS, True))
CANDIDATE.update(PROCESS["accounts"])


def test_sunday_composition_keeps_every_captured_live_v2_flag():
    assert set(CANDIDATE) <= set(Settings.model_fields)
    settings = Settings(_env_file=None, **CANDIDATE)
    for name, value in CANDIDATE.items():
        assert getattr(settings, name) == value, name
    assert len(PROCESS["flags"]) == 40
    assert settings.strategy_schwab_1m_v2_atr_only_mode
    assert settings.strategy_schwab_1m_v2_retry_one_max_retries == 0
    assert not settings.strategy_schwab_1m_v2_atr_reprice_handoff_enabled


@pytest.mark.asyncio
async def test_sunday_60s_real_bar_and_quote_handlers_stay_under_50ms(monkeypatch, caplog, tmp_path):
    from scripts import line_repair_preopen_replay as replay
    monkeypatch.setattr(replay, "LIVE_CONTROLS", CANDIDATE)
    row = next(r for r in ROWS if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    current = row["boundary_ms"] + 9 * 60_000
    case, _provider = replay.setup(row, current, admission_control=True)
    assert await replay.repair(row, case, current)
    case.bot._boot_state_restoration_complete = True
    case.bot._cw_boot_hold_check()
    assert not case.strategy._entries_held
    engine = create_engine("sqlite:///" + str(tmp_path / "rate.sqlite"),
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    accounts = (case.settings.strategy_schwab_1m_v2_account_name,
                case.settings.strategy_schwab_1m_v2_webull_account_name)
    with factory() as session:
        for name in accounts:
            session.add(BrokerAccount(id=uuid4(), name=name, provider="replay",
                                      environment="isolated"))
        session.add(Strategy(id=uuid4(), code="schwab_1m_v2", name="isolated v2"))
        session.commit()
    request = RemovedWait("MI", 0, "rate-clear", case.now_ms - 40_000, accounts)
    store = RemovedWaitStore(factory)
    store.record(request, True)
    case.bot.session_factory = factory
    case.bot._removed_wait_store = store
    writes = []
    loop_thread = threading.get_ident()
    original_write = RemovedWaitStore.commit_clear

    def slow_write(self, plan, valid):
        writes.append(threading.get_ident())
        assert writes[-1] != loop_thread, "durable clear must not run on the v2 loop"
        time.sleep(0.1)
        return original_write(self, plan, valid)

    monkeypatch.setattr(RemovedWaitStore, "commit_clear", slow_write)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = datetime.fromtimestamp(case.now_ms / 1000, UTC)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)

    monkeypatch.setattr("project_mai_tai.v2_removed_wait.datetime", Clock)
    case.strategy.configure_removed_wait(store.record_rules, restored={"MI": request},
                                         readable=True, dispatch_persist=store.record_dispatch)
    candles = {c["datetime"]: c for c in row["schwab"]["candles"]}
    loop = asyncio.get_running_loop()
    start = loop.time()
    stop = start + 60
    stalls, quote_ms, bar_ms = [], [], []

    async def heartbeat():
        deadline = loop.time() + 0.002
        while loop.time() < stop:
            await asyncio.sleep(max(0, deadline - loop.time()))
            stalls.append(max(0, loop.time() - deadline) * 1000)
            deadline = loop.time() + 0.002

    events = 0
    with case.clock():
        watcher = asyncio.create_task(heartbeat())
        clear = asyncio.create_task(case.bot._removed_wait_poll())
        while loop.time() < stop:
            # Advance one recorded market minute per wall second; duplicate bars
            # exercise the same-bucket callback without inventing extra flips.
            stamp = current + (int(loop.time() - start) + 1) * 60_000
            candle = candles.get(stamp)
            if candle is None:
                close = case.strategy.watchlist_state("MI").bars[-1].close
                bar = ChartBar("MI", close, close, close, close, 0, stamp)
            else:
                bar = ChartBar("MI", *(candle[k] for k in ("open", "high", "low", "close")),
                               candle["volume"], stamp)
            case.now_ms = stamp + 61_000
            case.strategy.apply_flip_position_book(FlipPositionBook(case.now_ms, True, {}))
            quote = Quote("MI", 1.0, 1.01, 1.0, case.now_ms, trade_time_ms=case.now_ms)
            before = loop.time()
            await case.bot._handle_quote("MI", quote)
            quote_ms.append((loop.time() - before) * 1000)
            before = loop.time()
            await case.bot._handle_bar("MI", bar, observation_phase="live")
            bar_ms.append((loop.time() - before) * 1000)
            events += 1
            await asyncio.sleep(max(0, start + events / 225 - loop.time()))
        await watcher
        await clear
    duration = loop.time() - start
    print(f"SUNDAY-V2-RATE quote_events={events} bar_events={events} duration_s={duration:.3f} "
          f"each_events_s={events / duration:.3f} max_stall_ms={max(stalls):.3f} "
          f"max_quote_ms={max(quote_ms):.3f} max_bar_ms={max(bar_ms):.3f} "
          f"last_market_time={datetime.fromtimestamp(case.now_ms / 1000, UTC).isoformat()}")
    assert duration >= 60
    assert events / duration >= 200
    assert max(stalls) < 50
    assert max(quote_ms) < 50
    assert max(bar_ms) < 50
    assert case.bot._persist_bar.call_count == events
    assert writes and store.restore() == {}
    assert not case.strategy._removed_wait_requests
    assert not any(record.exc_info for record in caplog.records)
    engine.dispose()
