"""[codex] Recorded prices with controlled source/clock states; no production I/O."""
from copy import deepcopy
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import (
    SchwabV2RestClient, anchored_session_poll_open,
)
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft
from tests.unit.test_line_chart_restoration_integration import (
    _bars, _bot, _ingest, _ms, _payload, _proof,
)

EMPTY_MEASUREMENT = json.loads((Path(__file__).parents[1] / "fixtures" /
                               "linesrc1_oct7_empty_history_measurement.json").read_text())
EMPTY_RESPONSES = EMPTY_MEASUREMENT["responses"]
OWN_EMPTY_RECEIPT = json.loads((Path(__file__).parents[1] / "fixtures" /
                              "linesrc1_oct7_empty_history_own_receipt.json").read_text())
OWN_EMPTY_ROWS = {row["symbol"]: row for row in OWN_EMPTY_RECEIPT["rows"]}


@pytest.mark.parametrize("day,trading_day", [
    ("2026-01-06", True), ("2026-07-06", True),
    ("2026-03-06", True), ("2026-03-08", False), ("2026-03-09", True),
    ("2026-10-30", True), ("2026-11-01", False), ("2026-11-02", True),
])
@pytest.mark.parametrize("clock,expected", [
    ("00:00:00", False), ("06:54:59.999", False), ("06:55:00", False),
    ("06:59:59.999", False), ("07:00:00", True),
    ("09:30:00", True), ("15:59:59.999", True), ("16:00:00", False),
    ("20:00:00", False), ("23:59:59", False),
])
def test_exact_et_session_boundaries_and_dst(day, trading_day, clock, expected):
    local = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ZoneInfo("America/New_York"))
    now = int(local.astimezone(UTC).timestamp() * 1000)
    assert anchored_session_poll_open(now) is (expected and trading_day)


def _client(bot=None):
    if bot is None:
        bot = _bot("RETO", _ms("2026-10-05T11:27:00-04:00"))
    client = SchwabV2RestClient(bot.settings, on_chart_bar=AsyncMock(), on_quote=AsyncMock())
    client._session_request = bot._line_source_request
    client._on_session_history = bot._accept_line_source
    client._on_session_failure = Mock(wraps=bot._line_source_failure)
    return client


@pytest.mark.parametrize("day", ["2026-01-06", "2026-07-06"])
@pytest.mark.parametrize("clock", ["07:01:00", "15:59:59"])
def test_in_window_service_requests_exact_anchor_and_previous_closed_minute(day, clock):
    local = datetime.fromisoformat(f"{day}T{clock}").replace(tzinfo=ZoneInfo("America/New_York"))
    now = int(local.timestamp() * 1000)
    bot = _bot("RETO", now - 61_000)
    epoch, anchor, current = bot._line_source_request("RETO")
    assert epoch == bot._line_sessions["RETO"].epoch
    assert anchor == int(local.replace(hour=4, minute=0, second=0).timestamp() * 1000)
    assert current == now // 60_000 * 60_000 - 60_000


@pytest.mark.parametrize("clock", ["06:54:59", "06:55:00", "07:00:59", "16:00:00", "20:00:00"])
def test_outside_session_provider_does_not_read_or_validate(clock):
    now = _ms(f"2026-10-05T{clock}-04:00")
    client = _client()
    client._authorized_get = Mock(side_effect=AssertionError("outside-window GET"))
    bars, proof = client.fetch_session_history("RETO", _ms("2026-10-05T04:00:00-04:00"),
                                             now // 60_000 * 60_000 - 60_000)
    assert bars == [] and proof is None
    client._authorized_get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("clock", ["06:54:59", "06:55:00", "07:00:59", "16:00:00", "20:00:00"])
async def test_outside_session_service_is_quiet_without_epoch_change(monkeypatch, caplog, clock):
    now = _ms(f"2026-10-05T{clock}-04:00")
    bot = _bot("RETO", now - 61_000)
    before = (bot._line_epoch, dict(bot._line_sessions))
    bot._sync_line_epochs = Mock(side_effect=AssertionError("outside-window epoch sync"))
    client = _client(bot)
    client._authorized_get = Mock(side_effect=AssertionError("outside-window GET"))
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.INFO):
        await client._anchored_bar_loop_pass(["RETO"], 5)
    assert (bot._line_epoch, bot._line_sessions) == before
    client._authorized_get.assert_not_called()
    client._on_session_failure.assert_not_called()
    client._on_chart_bar.assert_not_awaited()
    assert not caplog.records


@pytest.mark.asyncio
async def test_client_also_fences_an_outside_session_context(monkeypatch, caplog):
    client = _client()
    client._session_request = lambda symbol: (7, _ms("2026-10-05T04:00:00-04:00"),
                                              _ms("2026-10-05T15:59:00-04:00"))
    client.fetch_session_history = Mock(side_effect=AssertionError("outside-window fetch"))
    client._on_session_history = Mock()
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.INFO):
        await client._anchored_bar_loop_pass(["RETO"], 5)
    client.fetch_session_history.assert_not_called()
    client._on_session_history.assert_not_called()
    client._on_session_failure.assert_not_called()
    assert not caplog.records


def test_recorded_missing_current_returns_bars_without_coverage_proof():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms + 60_000)
    client = _client(bot)
    client._authorized_get = lambda url: _payload("RETO", bars)
    response, proof = client.fetch_session_history(
        "RETO", bot._line_sessions["RETO"].anchor_ms, bars[-1].timestamp_ms + 60_000,
    )
    assert response == bars and proof is None


@pytest.mark.asyncio
async def test_initial_missing_current_cannot_publish_partial_line_or_emit(monkeypatch):
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms + 60_000)
    ledger = bot._line_sessions["RETO"]
    client = _client(bot)
    client._authorized_get = lambda url: _payload("RETO", bars)
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    await client._anchored_bar_loop_pass(["RETO"], 5)
    assert ledger._coverage is None and ledger.revision == 0 and not ledger._bars
    assert not bot._line_published and not bot.strategy.line_buy_ready("RETO")
    assert not bot.strategy.drain_pending_intents()
    client._on_session_failure.assert_not_called()
    client._on_chart_bar.assert_not_awaited()


@pytest.mark.parametrize("damage", ["duplicate", "foreign", "malformed", "truncated"])
def test_in_session_invalid_source_is_not_disguised_as_no_new_bar(damage):
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms + 60_000)
    payload = _payload("RETO", bars)
    if damage == "duplicate":
        payload["candles"].append(deepcopy(payload["candles"][0]))
    elif damage == "foreign":
        payload["candles"][0]["datetime"] = bot._line_sessions["RETO"].anchor_ms - 60_000
    elif damage == "malformed":
        payload["candles"][0].pop("high")
    else:
        payload["truncated"] = True
    client = _client(bot)
    client._authorized_get = lambda url: payload
    with pytest.raises((ValueError, KeyError)):
        client.fetch_session_history("RETO", bot._line_sessions["RETO"].anchor_ms,
                                     bars[-1].timestamp_ms + 60_000)


@pytest.mark.asyncio
async def test_missing_current_retains_epoch_but_blocks_entry_even_after_old_rebuild(monkeypatch):
    bars = _bars("RETO", _ms("2026-10-05T11:21:00-04:00"))
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    assert await bot._rebuild_session_line("RETO", ledger)
    assert bot.strategy.line_buy_ready("RETO")
    epoch, revision, coverage = ledger.epoch, ledger.revision, ledger._coverage
    draft = TradeIntentDraft("RETO", "buy", "open", 2, "controlled", {
        "line_restore_version": bot._line_version("RETO"),
    })
    assert bot._line_draft_allowed(draft)
    bot.strategy._now_ms = lambda: bars[-1].timestamp_ms + 121_000
    client = _client(bot)
    client._authorized_get = lambda url: _payload("RETO", bars)
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    await client._anchored_bar_loop_pass(["RETO"], 5)
    await client._anchored_bar_loop_pass(["RETO"], 5)
    assert ledger.epoch == epoch and ledger.revision == revision and ledger._coverage is coverage
    client._on_session_failure.assert_not_called()
    client._on_chart_bar.assert_not_awaited()
    assert not bot.strategy.line_buy_ready("RETO") and not bot._line_draft_allowed(draft)
    # A concurrent old worker may publish old math, but cannot release this source wait.
    await bot._rebuild_session_line("RETO", ledger)
    assert not bot.strategy.line_buy_ready("RETO") and not bot._line_draft_allowed(draft)
    complete = _bars("RETO", bars[-1].timestamp_ms + 60_000)
    assert complete[-1].timestamp_ms == bars[-1].timestamp_ms + 60_000
    client._authorized_get = lambda url: _payload("RETO", complete)
    await client._anchored_bar_loop_pass(["RETO"], 5)
    await bot._line_restoration_pass()
    assert bot.strategy.line_buy_ready("RETO") and not bot._line_draft_allowed(draft)
    draft.metadata["line_restore_version"] = bot._line_version("RETO")
    assert bot._line_draft_allowed(draft)
    assert not bot.strategy.drain_pending_intents()


@pytest.mark.asyncio
async def test_warning_only_on_symbol_state_transition_and_no_traceback(monkeypatch, caplog):
    bars = _bars("RETO")
    client = _client()
    client._on_session_history = Mock(return_value=True)
    client._on_session_failure = Mock()
    payload = _payload("RETO", bars[:-1])
    client._authorized_get = lambda url: payload
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            await client._anchored_bar_loop_pass(["RETO"], 5)
        assert client._on_session_history.call_args.args[-1] is None
        client._on_chart_bar.assert_not_awaited()
        payload = _payload("RETO", bars)
        for _ in range(2):
            await client._anchored_bar_loop_pass(["RETO"], 5)
        payload["candles"].append(deepcopy(payload["candles"][0]))
        for _ in range(2):
            await client._anchored_bar_loop_pass(["RETO"], 5)
        payload = _payload("RETO", bars[:-1])
        await client._anchored_bar_loop_pass(["RETO"], 5)
        payload = _payload("RETO", bars)
        await client._anchored_bar_loop_pass(["RETO"], 5)
    records = [r for r in caplog.records if "[V2-LINE-SOURCE-STATE]" in r.message]
    assert len(records) == 5
    assert [r.message.split("state=")[1].split()[0] for r in records] == [
        "waiting", "ready", "error", "waiting", "ready",
    ]
    assert all(r.levelno == logging.WARNING and r.exc_info is None for r in records)
    assert client._on_session_failure.call_count == 2


def test_warning_state_is_independent_for_each_symbol(caplog):
    client = _client()
    with caplog.at_level(logging.WARNING):
        for symbol in ("RETO", "RETO", "JAGX", "JAGX"):
            client._set_line_source_state(symbol, ("waiting", "current_closed_candle_pending"))
        client._set_line_source_state("RETO", ("error", "ValueError: foreign or duplicate session candle"))
    assert len(caplog.records) == 3
    assert [row.message.split("sym=")[1].split()[0] for row in caplog.records] == ["RETO", "JAGX", "RETO"]
    assert all(row.exc_info is None for row in caplog.records)


@pytest.mark.asyncio
async def test_changing_error_text_does_not_repeat_warning_or_skip_invalidation(monkeypatch, caplog):
    client = _client()
    client._on_session_failure = Mock()
    failures = iter(("REST429 retry_after=1", "REST429 retry_after=2"))

    def unavailable(url):
        raise RuntimeError(next(failures))

    client._authorized_get = unavailable
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.WARNING):
        await client._anchored_bar_loop_pass(["RETO"], 5)
        await client._anchored_bar_loop_pass(["RETO"], 5)
    records = [row for row in caplog.records if "[V2-LINE-SOURCE-STATE]" in row.message]
    assert len(records) == 1 and "REST429 retry_after=1" in records[0].message
    assert records[0].exc_info is None
    assert client._on_session_failure.call_count == 2


@pytest.mark.asyncio
async def test_duplicate_error_invalidates_previously_admitted_coverage(monkeypatch, caplog):
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    assert await bot._rebuild_session_line("RETO", ledger)
    client = _client(bot)
    payload = _payload("RETO", bars)
    payload["candles"].append(deepcopy(payload["candles"][0]))
    client._authorized_get = lambda url: payload
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.WARNING):
        await client._anchored_bar_loop_pass(["RETO"], 5)
    assert ledger._coverage is None and not bot.strategy.line_buy_ready("RETO")
    client._on_session_failure.assert_called_once_with("RETO", ledger.epoch)
    records = [row for row in caplog.records if "[V2-LINE-SOURCE-STATE]" in row.message]
    assert len(records) == 1 and records[0].exc_info is None


def test_old_epoch_response_cannot_set_or_clear_current_source_wait():
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = bot._line_sessions["RETO"]
    old_epoch = ledger.epoch
    bot._watchlist.clear()
    bot._sync_line_epochs()
    bot._watchlist.add("RETO")
    bot._sync_line_epochs()
    current = bot._line_sessions["RETO"]
    assert current.epoch != old_epoch
    assert not bot._accept_line_source("RETO", old_epoch, bars, None)
    assert "RETO" not in bot._line_source_waiting
    assert not bot._accept_line_source("RETO", current.epoch, bars, None)
    assert bot._line_source_waiting["RETO"] == current.epoch
    assert not bot._accept_line_source("RETO", old_epoch, bars, _proof(ledger, bars))
    assert bot._line_source_waiting["RETO"] == current.epoch


@pytest.mark.asyncio
@pytest.mark.parametrize("row", EMPTY_RESPONSES, ids=lambda row: row["symbol"])
@pytest.mark.parametrize("clock", ["06:21:00", "06:55:00", "06:59:59", "07:00:59"])
async def test_oct7_measured_empty_names_do_not_poll_before_first_0700_close(monkeypatch, caplog, row, clock):
    # Only the 06:21 shape/count is measured; other clocks are boundary controls.
    assert row["empty"] is True and row["candle_count"] == 0
    now = _ms(f"2026-10-07T{clock}-04:00")
    bot = _bot(row["symbol"], now - 61_000)
    before = (bot._line_epoch, dict(bot._line_sessions))
    bot._sync_line_epochs = Mock(side_effect=AssertionError("premature epoch sync"))
    client = _client(bot)
    client._authorized_get = Mock(side_effect=AssertionError("premature provider GET"))
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.INFO):
        await client._anchored_bar_loop_pass([row["symbol"]], 5)
    assert (bot._line_epoch, bot._line_sessions) == before
    client._authorized_get.assert_not_called()
    client._on_session_failure.assert_not_called()
    client._on_chart_bar.assert_not_awaited()
    assert not caplog.records


@pytest.mark.parametrize("row", EMPTY_RESPONSES, ids=lambda row: row["symbol"])
@pytest.mark.parametrize("empty", [True, False])
def test_oct7_measured_empty_shape_has_no_bars_or_proof_in_controlled_session(row, empty):
    from urllib.parse import parse_qs, urlparse

    # Actual 06:39 empty envelopes, replayed at a controlled in-session clock.
    # False is a counterfactual flag control, not an observed live response.
    symbol = row["symbol"]
    anchor = _ms(EMPTY_MEASUREMENT["anchor_et"])
    current = _ms("2026-10-07T07:00:00-04:00")
    client = _client(_bot(symbol, current))
    payload = deepcopy(OWN_EMPTY_ROWS[symbol]["raw_marketdata_response"])
    payload["empty"] = empty
    client._authorized_get = Mock(return_value=payload)
    try:
        bars, proof = client.fetch_session_history(symbol, anchor, current)
    except ValueError as exc:
        pytest.fail(f"valid empty history is no bars yet, not an epoch failure: {exc}")
    assert bars == [] and proof is None
    query = parse_qs(urlparse(client._authorized_get.call_args.args[0]).query)
    assert query["startDate"] == [str(anchor)]
    assert query["endDate"] == [str(current + 59_999)]


@pytest.mark.asyncio
@pytest.mark.parametrize("row", EMPTY_RESPONSES, ids=lambda row: row["symbol"])
async def test_oct7_empty_session_waits_without_epoch_failure_or_warning_flood(monkeypatch, caplog, row):
    symbol = row["symbol"]
    bot = _bot(symbol, _ms("2026-10-07T07:00:00-04:00"))
    ledger = bot._line_sessions[symbol]
    client = _client(bot)
    client._authorized_get = Mock(return_value=deepcopy(OWN_EMPTY_ROWS[symbol]["raw_marketdata_response"]))
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.WARNING):
        await client._anchored_bar_loop_pass([symbol], 5)
        await client._anchored_bar_loop_pass([symbol], 5)
    assert ledger._coverage is None and ledger.revision == 0 and not ledger._bars
    assert bot._line_source_waiting[symbol] == ledger.epoch
    assert not bot.strategy.line_buy_ready(symbol) and not bot._line_published
    assert not bot.strategy.drain_pending_intents()
    client._on_session_failure.assert_not_called()
    client._on_chart_bar.assert_not_awaited()
    records = [r for r in caplog.records if "[V2-LINE-SOURCE-STATE]" in r.message]
    assert len(records) == 1 and "state=waiting" in records[0].message
    assert records[0].levelno == logging.WARNING and records[0].exc_info is None


@pytest.mark.parametrize("damage", ["foreign", "truncated", "unknown_empty", "nonlist", "contradiction"])
def test_empty_is_not_a_bypass_for_foreign_or_malformed_source(damage):
    current = _ms("2026-10-07T07:00:00-04:00")
    anchor = _ms(EMPTY_MEASUREMENT["anchor_et"])
    client = _client(_bot("BIYA", current))
    payload = {"symbol": "BIYA", "empty": True, "candles": []}
    if damage == "foreign":
        payload["symbol"] = "MI"
    elif damage == "truncated":
        payload["truncated"] = True
    elif damage == "unknown_empty":
        payload.pop("empty")
    elif damage == "nonlist":
        payload["candles"] = None
    else:
        bars = _bars("RETO")
        payload = _payload("RETO", bars)
        payload["empty"] = True
        anchor = _ms("2026-10-05T04:00:00-04:00")
        current = bars[-1].timestamp_ms
    client._authorized_get = Mock(return_value=payload)
    with pytest.raises(ValueError):
        client.fetch_session_history(payload["symbol"] if damage == "contradiction" else "BIYA",
                                     anchor, current)


@pytest.mark.asyncio
async def test_client_fences_context_before_first_0700_closed_candle(monkeypatch, caplog):
    client = _client()
    client._session_request = lambda symbol: (7, _ms(EMPTY_MEASUREMENT["anchor_et"]),
                                              _ms("2026-10-07T06:59:00-04:00"))
    client.fetch_session_history = Mock(side_effect=AssertionError("premature fetch"))
    client._on_session_history = Mock()
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    with caplog.at_level(logging.INFO):
        await client._anchored_bar_loop_pass(["BIYA"], 5)
    client.fetch_session_history.assert_not_called()
    client._on_session_history.assert_not_called()
    client._on_session_failure.assert_not_called()
    assert not caplog.records


@pytest.mark.asyncio
async def test_valid_empty_response_retains_prior_recorded_coverage_but_cannot_release_wait(monkeypatch):
    bars = _bars("RETO")
    bot = _bot("RETO", bars[-1].timestamp_ms)
    ledger = _ingest(bot, bars)
    ledger.attest(_proof(ledger, bars))
    assert await bot._rebuild_session_line("RETO", ledger)
    assert bot.strategy.line_buy_ready("RETO")
    before = (ledger.epoch, ledger.revision, ledger._coverage)
    client = _client(bot)
    client._authorized_get = Mock(return_value={"symbol": "RETO", "empty": True, "candles": []})
    monkeypatch.setattr("project_mai_tai.market_data.schwab_v2_rest_client.sleep_or_stop", AsyncMock())
    await client._anchored_bar_loop_pass(["RETO"], 5)
    assert (ledger.epoch, ledger.revision, ledger._coverage) == before
    assert not bot.strategy.line_buy_ready("RETO")
    await bot._rebuild_session_line("RETO", ledger)
    assert not bot.strategy.line_buy_ready("RETO")
    assert not bot.strategy.drain_pending_intents()
    client._on_session_failure.assert_not_called()
    client._on_chart_bar.assert_not_awaited()


@pytest.mark.parametrize("symbol", ["BIYA", "MI", "MTEN", "SXTC"])
def test_own_oct7_live_receipt_is_bounded_readonly_and_matches_empty_shape(symbol):
    receipt = OWN_EMPTY_RECEIPT
    row = OWN_EMPTY_ROWS[symbol]
    assert receipt["official_status"] == "MEASURED" and len(receipt["rows"]) == 4
    assert receipt["max_requests"] == 4 and receipt["retries"] == 0
    assert receipt["token_refresh"] is False and receipt["remote_file_writes"] is False
    assert receipt["anchor_et"] == "2026-10-07T04:00:00-04:00"
    local = datetime.fromisoformat(row["request_et"])
    assert local.date().isoformat() == "2026-10-07" and local.hour < 7
    assert row["status"] == "MEASURED" and row["http_status"] == 200
    assert row["reproduces_empty_true_zero"] is True
    assert row["raw_marketdata_response"] == {"symbol": symbol, "empty": True, "candles": []}
    assert row["other_response_field_names"] == []
    assert len(row["raw_body_sha256"]) == 64


@pytest.mark.parametrize("clock,closed", [("07:00:00", None), ("07:01:00", "07:00:00")])
def test_exact_wallclock_first_0700_closed_minute_availability(clock, closed):
    now = _ms(f"2026-10-07T{clock}-04:00")
    bot = _bot("BIYA", now - 61_000)
    request = bot._line_source_request("BIYA")
    if closed is None:
        assert request is None
    else:
        epoch, anchor, current = request
        assert epoch == bot._line_sessions["BIYA"].epoch
        assert anchor == _ms("2026-10-07T04:00:00-04:00")
        assert current == _ms(f"2026-10-07T{closed}-04:00")
