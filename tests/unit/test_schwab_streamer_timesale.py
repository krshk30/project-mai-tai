"""Smoke tests for the LEVELONE/CHART/TIMESALE multi-service streamer."""
from __future__ import annotations

import asyncio

import pytest

from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.settings import Settings


def test_levelone_trade_extraction_unchanged() -> None:
    payload = {
        "data": [
            {
                "service": "LEVELONE_EQUITIES",
                "content": [{"key": "ABC", "3": 10.5, "8": 1000, "9": 100, "35": 1700000000000}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(payload)
    assert len(trades) == 1
    assert trades[0].symbol == "ABC"
    assert trades[0].price == 10.5
    assert trades[0].cumulative_volume == 1000
    assert bars == []


def test_timesale_trade_extraction() -> None:
    payload = {
        "data": [
            {
                "service": "TIMESALE_EQUITY",
                "content": [{"key": "ABC", "1": 1700000000000, "2": 11.25, "3": 50, "4": 999}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(payload)
    assert len(trades) == 1
    assert trades[0].symbol == "ABC"
    assert trades[0].price == 11.25
    assert trades[0].size == 50
    # TIMESALE record has no cumulative_volume — bar builder should fall back to size
    assert trades[0].cumulative_volume is None


def test_levelone_trade_dedupe_when_symbol_subscribed_to_timesale() -> None:
    """When a symbol is subscribed to TIMESALE_EQUITY, LEVELONE trades for that
    symbol must be suppressed to avoid double-counting volume."""
    payload = {
        "data": [
            {
                "service": "LEVELONE_EQUITIES",
                "content": [{"key": "ABC", "3": 10.5, "8": 1000, "9": 100, "35": 1700000000000}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(
        payload,
        timesale_symbols={"ABC"},
    )
    # Quote should still be extracted (so spreads stay live), but trade is suppressed.
    assert trades == []
    # Quote needs valid bid + ask — payload above only has trade fields, so quotes is also empty.
    # That's fine; the assertion above is the dedupe contract.


def test_chart_equity_bar_extraction() -> None:
    payload = {
        "data": [
            {
                "service": "CHART_EQUITY",
                "content": [{"key": "ABC", "2": 10.0, "3": 10.5, "4": 9.9, "5": 10.2, "6": 5000, "7": 1700000060000}],
            }
        ]
    }
    quotes, trades, bars = SchwabStreamerClient._extract_records(payload)
    assert len(bars) == 1
    assert bars[0].symbol == "ABC"
    assert bars[0].open == 10.0
    assert bars[0].close == 10.2
    assert bars[0].volume == 5000
    assert bars[0].interval_secs == 60


class _HangingWebSocket:
    async def close(self) -> None:
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_stop_bounds_total_time_when_websocket_close_and_task_both_hang() -> None:
    """stop() must not hang past ~6s when ws.close() and the connection task both stall."""
    client = SchwabStreamerClient(Settings())

    async def hanging_connection_loop() -> None:
        await asyncio.Event().wait()

    client._task = asyncio.create_task(hanging_connection_loop())
    client._ws = _HangingWebSocket()  # type: ignore[assignment]

    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.wait_for(client.stop(), timeout=10.0)
    elapsed = loop.time() - start

    assert elapsed < 8.0, f"stop() took {elapsed:.2f}s; expected < 8s"
    assert client._task is None
    assert client._ws is None
    assert client._stop_event.is_set()


@pytest.mark.asyncio
async def test_stop_completes_fast_when_streamer_is_idle() -> None:
    """stop() returns quickly when there's no task and no websocket — no regression on the happy path."""
    client = SchwabStreamerClient(Settings())

    loop = asyncio.get_running_loop()
    start = loop.time()
    await client.stop()
    elapsed = loop.time() - start

    assert elapsed < 0.5
    assert client._stop_event.is_set()


def test_chart_exchange_deadline_exceeds_bar_interval() -> None:
    """Regression guard for the 2026-05-26 production flap (root cause introduced
    by commit 518beea). The CHART exchange-deadline compares a COMPLETED 1-min
    bar's close against the continuous tick clock, so it MUST exceed the bar
    interval or it force-reconnects a HEALTHY feed every minute. Default
    schwab_stream_symbol_stale_after_seconds=8 -> 60 + max(30, 8*4) = 92s."""
    client = SchwabStreamerClient(Settings())
    deadline = client._chart_exchange_deadline_seconds()
    assert deadline >= client.CHART_BAR_INTERVAL_SECONDS, (
        "exchange-deadline must exceed the 1-min bar interval or it trips every minute"
    )
    assert deadline == client.CHART_BAR_INTERVAL_SECONDS + 32.0
    # The separate 90s message-stale branch (genuine dead-feed guard) is unchanged.
    assert client._service_stale_after_seconds(client.CHART_EQUITY_SERVICE) == 90.0


def _seed_pending_chart_request(
    client: SchwabStreamerClient,
    *,
    request_id: int,
    command: str,
    symbols: tuple[str, ...],
) -> None:
    client._pending_subscription_requests[request_id] = (
        client.CHART_EQUITY_SERVICE,
        command,
        symbols,
    )


def test_chart_new_subscription_resets_last_completed_bar_close_timestamp() -> None:
    """Regression guard for the 2026-05-28 04:00 ET scanner-session warmup spike
    (163 false exchange_deadline_exceeded reconnects in one hour, all feed-alive
    chart_msg_age 1-2s). At subscription-set change, a freshly-added CHART symbol
    has no completed bar yet, but the per-service last_completed_bar_close_timestamp
    can carry stale data from before the change (e.g., yesterday's last close). The
    deadline check (latest_other_exchange_ts > chart_close_ts + 92s) then trips
    spuriously while TIMESALE/LEVELONE start streaming today's clock instantly.

    Fix: resetting to None on net-new symbols disables the deadline check (returns
    False on None) until any subscribed symbol publishes a fresh bar."""
    client = SchwabStreamerClient(Settings())
    chart_state = client._service_states[client.CHART_EQUITY_SERVICE]

    # Simulate a carry-over timestamp from before the subscription change.
    chart_state.last_completed_bar_close_timestamp = 1.0

    _seed_pending_chart_request(client, request_id=1, command="ADD", symbols=("ABC",))
    client._mark_subscription_request_confirmed(
        request_id=1,
        service=client.CHART_EQUITY_SERVICE,
        command="ADD",
        observed_at=100.0,
    )

    assert "ABC" in chart_state.confirmed_symbols
    assert chart_state.last_completed_bar_close_timestamp is None
    # Direct deadline check confirms it's disabled until a fresh bar arrives.
    assert client._chart_exchange_deadline_exceeded(now=1_000_000.0) is False


def test_chart_resubscription_of_existing_symbols_preserves_timestamp() -> None:
    """The reset must only trigger on NET-NEW symbols. A redundant SUBS/ADD
    confirmation for an already-confirmed symbol (e.g., keep-alive or no-op
    re-sync) must NOT wipe the fresh bar-close timestamp."""
    client = SchwabStreamerClient(Settings())
    chart_state = client._service_states[client.CHART_EQUITY_SERVICE]

    chart_state.confirmed_symbols.add("ABC")
    chart_state.last_completed_bar_close_timestamp = 12345.0

    _seed_pending_chart_request(client, request_id=1, command="ADD", symbols=("ABC",))
    client._mark_subscription_request_confirmed(
        request_id=1,
        service=client.CHART_EQUITY_SERVICE,
        command="ADD",
        observed_at=100.0,
    )

    assert chart_state.last_completed_bar_close_timestamp == 12345.0


def test_chart_unsubs_does_not_reset_timestamp() -> None:
    """An UNSUBS removes a symbol; remaining symbols' bar-close timestamps stay
    valid for the deadline check. Reset must not fire on removal."""
    client = SchwabStreamerClient(Settings())
    chart_state = client._service_states[client.CHART_EQUITY_SERVICE]

    chart_state.confirmed_symbols.update({"ABC", "DEF"})
    chart_state.last_completed_bar_close_timestamp = 12345.0

    _seed_pending_chart_request(client, request_id=1, command="UNSUBS", symbols=("ABC",))
    client._mark_subscription_request_confirmed(
        request_id=1,
        service=client.CHART_EQUITY_SERVICE,
        command="UNSUBS",
        observed_at=100.0,
    )

    assert "ABC" not in chart_state.confirmed_symbols
    assert "DEF" in chart_state.confirmed_symbols
    assert chart_state.last_completed_bar_close_timestamp == 12345.0


def test_timesale_new_subscription_does_not_reset_chart_timestamp() -> None:
    """The reset is CHART_EQUITY-scoped. A new TIMESALE subscription must not
    disturb the chart deadline state — TIMESALE is the other-exchange clock,
    not the bar-close clock."""
    client = SchwabStreamerClient(Settings())
    chart_state = client._service_states[client.CHART_EQUITY_SERVICE]

    chart_state.last_completed_bar_close_timestamp = 12345.0

    _seed_pending_chart_request(client, request_id=1, command="ADD", symbols=("XYZ",))
    # Overwrite the service field to TIMESALE so the pending tuple matches.
    client._pending_subscription_requests[1] = (
        client.TIMESALE_EQUITY_SERVICE,
        "ADD",
        ("XYZ",),
    )
    client._mark_subscription_request_confirmed(
        request_id=1,
        service=client.TIMESALE_EQUITY_SERVICE,
        command="ADD",
        observed_at=100.0,
    )

    assert chart_state.last_completed_bar_close_timestamp == 12345.0
