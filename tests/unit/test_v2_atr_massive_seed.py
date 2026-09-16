from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_mai_tai.market_data.massive_atr_seed import (
    MassiveAtrSeedBar,
    select_massive_atr_seed_bars,
)
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy


def _chart(symbol: str, row: list[float]) -> ChartBar:
    return ChartBar(
        symbol,
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        int(row[5]),
        int(row[0]),
    )


def _massive(ts: int, close: float = 10.0) -> MassiveAtrSeedBar:
    return MassiveAtrSeedBar(ts, close, close + 0.2, close - 0.2, close, 1_000)


def test_splice_is_time_bounded_deduplicated_and_schwab_wins_shared_minute() -> None:
    first_schwab = 1_789_556_400_000  # 2026-09-16 07:00 ET
    rows = [
        _massive(1_789_545_540_000),  # prior session
        _massive(1_789_545_600_000),  # 04:00 ET, included
        _massive(first_schwab - 60_000),
        _massive(first_schwab),  # shared minute, Schwab owns it
        _massive(first_schwab + 60_000),
        _massive(first_schwab - 60_000, close=11.0),  # revised row deduplicates
    ]

    selected = select_massive_atr_seed_bars(
        rows, first_schwab_bar_ts_ms=first_schwab
    )

    assert [row.timestamp_ms for row in selected] == [
        1_789_545_600_000,
        first_schwab - 60_000,
    ]
    assert selected[-1].close == 11.0
    assert all(row.timestamp_ms < first_schwab for row in selected)


def test_seed_advances_only_atr_state_even_when_seed_contains_a_buy_flip() -> None:
    strategy = SchwabV2Strategy(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_flip_period=5,
            strategy_schwab_1m_v2_atr_flip_factor=3.5,
            strategy_schwab_1m_v2_atr_flip_rearm_enabled=False,
        )
    )
    base = 1_789_545_600_000
    prices = [
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 99.8, 100.0),
        (99.8, 99.9, 97.9, 98.0),
        (97.8, 97.9, 97.5, 97.6),
        (97.4, 97.5, 97.1, 97.2),
        (97.1, 97.2, 96.8, 96.9),
        (96.9, 97.0, 96.6, 96.7),
        (96.8, 96.9, 96.5, 96.6),
        (96.7, 96.8, 96.4, 96.5),
        (96.7, 99.5, 96.6, 99.3),
    ]
    bars = [
        ChartBar("TEST", o, h, low, close, 50_000, base + index * 60_000)
        for index, (o, h, low, close) in enumerate(prices)
    ]
    state = strategy.watchlist_state("TEST")

    result = strategy.seed_atr_state("TEST", bars)

    assert any(side == "BUY" for _timestamp, side in result["flips"])
    assert state.atr_state == "long"
    assert state.atr_trail is not None
    assert list(state.bars) == []
    assert state.vwap_sum_pv == 0.0 and state.vwap_sum_v == 0.0
    assert state.cw_armed is False and state.resting_active is False
    assert state.atr_fired_in_short_seg is False
    assert strategy.drain_pending_intents() == []
    assert strategy.pending_atr_sell_observations() == []


def test_first_schwab_bar_still_runs_full_session_reset_without_losing_seed() -> None:
    strategy = SchwabV2Strategy(Settings(_env_file=None))
    state = strategy.watchlist_state("TEST")
    state.cw_armed = True
    state.resting_active = True
    state.atr_fired_in_short_seg = True
    first_schwab = 1_789_556_400_000
    seed = [
        ChartBar("TEST", 10.0, 10.2, 9.8, 10.0, 1_000, first_schwab - (10 - i) * 60_000)
        for i in range(10)
    ]

    strategy.seed_atr_state("TEST", seed)
    assert state.cw_armed is True
    assert state.resting_active is True
    assert state.atr_fired_in_short_seg is True

    signal = strategy._update_atr_state(
        state,
        OHLCVBar(first_schwab, 10.0, 10.2, 9.8, 10.0, 1_000),
        observation_phase="live",
    )

    assert signal is not None
    assert state.cw_armed is False
    assert state.resting_active is False
    assert state.atr_fired_in_short_seg is False
    assert state.atr_prev_bar is not None
    assert state.atr_prev_bar.timestamp_ms == first_schwab


def test_seeded_short_flip_stamp_survives_first_schwab_full_reset() -> None:
    strategy = SchwabV2Strategy(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_flip_period=5,
            strategy_schwab_1m_v2_atr_flip_factor=3.5,
        )
    )
    first_schwab = 1_789_556_400_000
    prices = [(100.0, 100.2, 99.8, 100.0)] * 9 + [
        (99.8, 99.9, 97.9, 98.0)
    ]
    seed = [
        ChartBar(
            "TEST",
            open_price,
            high,
            low,
            close,
            1_000,
            first_schwab - (10 - index) * 60_000,
        )
        for index, (open_price, high, low, close) in enumerate(prices)
    ]
    state = strategy.watchlist_state("TEST")

    result = strategy.seed_atr_state("TEST", seed)

    assert result["flips"][-1] == (seed[-1].timestamp_ms, "SELL")
    assert state.atr_state == "short"
    assert state.atr_short_flip_bar_ts == seed[-1].timestamp_ms
    assert state.atr_fired_in_short_seg is False

    strategy._update_atr_state(
        state,
        OHLCVBar(first_schwab, 98.0, 98.1, 97.5, 97.8, 1_000),
        observation_phase="live",
    )

    assert state.atr_state == "short"
    assert state.atr_short_flip_bar_ts == seed[-1].timestamp_ms
    assert state.atr_fired_in_short_seg is False


def test_meds_real_tape_seeded_pair_and_unseeded_control() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1]
            / "backtest"
            / "fixtures"
            / "MEDS_20260916_atr_seed.json"
        ).read_text()
    )

    def run(seed: bool) -> dict[str, tuple[str | None, float | None, str | None]]:
        strategy = SchwabV2Strategy(
            Settings(
                _env_file=None,
                strategy_schwab_1m_v2_atr_flip_period=5,
                strategy_schwab_1m_v2_atr_flip_factor=3.5,
            )
        )
        if seed:
            strategy.seed_atr_state(
                "MEDS", [_chart("MEDS", row) for row in fixture["massive"]]
            )
        state = strategy.watchlist_state("MEDS")
        observed = {}
        for row in fixture["schwab"]:
            signal = strategy._update_atr_state(
                state,
                OHLCVBar(
                    timestamp_ms=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=int(row[5]),
                ),
                observation_phase="replay",
                state_only=True,
            )
            at = datetime.fromtimestamp(int(row[0]) / 1000.0, UTC).strftime("%H:%M")
            if at in {"11:24", "11:25", "12:34"}:
                observed[at] = (
                    state.atr_state,
                    state.atr_trail,
                    signal.get("flip") if signal else None,
                )
        return observed

    seeded = run(True)
    unseeded = run(False)

    assert seeded["11:24"][0::2] == ("long", None)
    assert seeded["11:24"][1] == pytest.approx(3.5729444854594203)
    assert seeded["11:25"][0::2] == ("short", "SELL")
    assert seeded["11:25"][1] == pytest.approx(4.065402873515202)
    assert seeded["12:34"][0::2] == ("long", "BUY")
    assert seeded["12:34"][1] == pytest.approx(3.506206462351973)
    assert unseeded["11:24"][0::2] == ("long", None)
    assert unseeded["11:24"][1] == pytest.approx(3.4490026230167894)
    assert unseeded["11:25"][2] is None
    assert unseeded["12:34"][2] is None


class _SeedClient:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls = 0

    def fetch(self, symbol: str, session_start_ms: int, first_schwab_ms: int):
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("unavailable")
        if self.mode == "timeout":
            time.sleep(0.05)
        if self.mode == "bars":
            return [
                _massive(first_schwab_ms - (10 - index) * 60_000)
                for index in range(10)
            ]
        return []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected"),
    [("raise", "UNSEEDED"), ("empty", "EMPTY"), ("timeout", "UNSEEDED")],
)
async def test_fetch_failure_is_fail_closed_and_same_bar_is_processed(
    mode: str, expected: str, caplog
) -> None:
    caplog.set_level(logging.INFO)
    client = _SeedClient(mode)
    service = SchwabV2BotService(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_massive_seed_enabled=True,
        ),
        massive_atr_seed_client=client,
        massive_atr_seed_timeout_seconds=0.01,
    )
    processed = []

    def record_bar(symbol, bar, observation_phase):
        processed.append((symbol, bar.timestamp_ms, observation_phase))
        return None

    service._strategy_on_bar = record_bar
    first_schwab = 1_789_556_400_000
    bar = ChartBar("MEDS", 3.5, 3.6, 3.4, 3.55, 10_000, first_schwab)

    await service._handle_bar("MEDS", bar, observation_phase="replay")
    await service._handle_bar("MEDS", bar, observation_phase="replay")

    outcome = service._atr_massive_seed_outcomes[("MEDS", 1_789_545_600_000)]
    assert outcome["status"] == expected
    assert processed == [
        ("MEDS", first_schwab, "replay"),
        ("MEDS", first_schwab, "replay"),
    ]
    assert client.calls == 1
    assert sum("[V2-ATR-SEED] sym=MEDS" in record.message for record in caplog.records) == 1
    assert any(
        "[V2-ATR-SEED-CENSUS]" in record.message and "denominator=1" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_successful_fetch_seeds_before_the_first_schwab_bar() -> None:
    client = _SeedClient("bars")
    service = SchwabV2BotService(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_massive_seed_enabled=True,
        ),
        massive_atr_seed_client=client,
    )
    first_schwab = 1_789_556_400_000
    bar = ChartBar("MEDS", 10.0, 10.2, 9.8, 10.0, 10_000, first_schwab)

    await service._handle_bar("MEDS", bar, observation_phase="live")

    outcome = service._atr_massive_seed_outcomes[("MEDS", 1_789_545_600_000)]
    state = service.strategy.watchlist_state("MEDS")
    assert client.calls == 1
    assert outcome["status"] == "SEEDED"
    assert outcome["bars_seeded"] == 10
    assert outcome["first_schwab_ms"] == first_schwab
    assert state.atr_prev_bar is not None
    assert state.atr_prev_bar.timestamp_ms == first_schwab


def test_setting_defaults_off() -> None:
    assert Settings(_env_file=None).strategy_schwab_1m_v2_atr_massive_seed_enabled is False


def test_session_census_exposes_watchlist_denominator_before_first_bar(caplog) -> None:
    caplog.set_level(logging.INFO)
    service = SchwabV2BotService(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_massive_seed_enabled=True,
        ),
        massive_atr_seed_client=_SeedClient("empty"),
    )
    service._watchlist = {"MEDS", "TEST"}

    service._log_atr_massive_seed_census(force=True)

    assert any(
        "[V2-ATR-SEED-CENSUS]" in record.message
        and "seeded=0 unseeded=0 empty=0 denominator=2" in record.message
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_rest_and_streamer_race_share_one_seed_fetch() -> None:
    client = _SeedClient("timeout")
    service = SchwabV2BotService(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_massive_seed_enabled=True,
        ),
        massive_atr_seed_client=client,
        massive_atr_seed_timeout_seconds=1.0,
    )
    first_schwab = 1_789_556_400_000

    await asyncio.gather(
        service._ensure_atr_massive_seed_before_bar("MEDS", first_schwab),
        service._ensure_atr_massive_seed_before_bar("MEDS", first_schwab),
    )

    assert client.calls == 1
    assert service._atr_massive_seed_outcomes[("MEDS", 1_789_545_600_000)][
        "status"
    ] == "EMPTY"


@pytest.mark.asyncio
async def test_adjacent_rest_and_streamer_bars_keep_the_earliest_seed_boundary() -> None:
    client = _SeedClient("timeout")
    service = SchwabV2BotService(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_massive_seed_enabled=True,
        ),
        massive_atr_seed_client=client,
        massive_atr_seed_timeout_seconds=1.0,
    )
    first_schwab = 1_789_556_400_000

    await asyncio.gather(
        service._ensure_atr_massive_seed_before_bar("MEDS", first_schwab),
        service._ensure_atr_massive_seed_before_bar("MEDS", first_schwab + 60_000),
    )

    outcome = service._atr_massive_seed_outcomes[("MEDS", 1_789_545_600_000)]
    assert client.calls == 1
    assert outcome["first_schwab_ms"] == first_schwab


@pytest.mark.asyncio
async def test_seed_never_runs_after_a_current_session_schwab_bar() -> None:
    client = _SeedClient("empty")
    service = SchwabV2BotService(
        Settings(
            _env_file=None,
            strategy_schwab_1m_v2_atr_massive_seed_enabled=True,
        ),
        massive_atr_seed_client=client,
    )
    first_schwab = 1_789_556_400_000
    service.strategy.on_observed_bar(
        "MEDS",
        ChartBar("MEDS", 3.5, 3.6, 3.4, 3.55, 10_000, first_schwab),
        observation_phase="live",
    )

    await service._ensure_atr_massive_seed_before_bar(
        "MEDS", first_schwab + 60_000
    )

    assert client.calls == 0
    assert service._atr_massive_seed_outcomes[("MEDS", 1_789_545_600_000)][
        "error"
    ] == "first_schwab_bar_already_processed"
