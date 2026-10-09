"""[codex] Strict provider boundary and real one-shot repair; no live I/O."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from project_mai_tai.market_data.massive_atr_seed import MassiveAtrSeedClient
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, SchwabV2RestClient
from tests.line_restore_acceptance_factory import make_line_restore_case
from tests.unit.test_line_chart_restoration_integration import _bot, _ms

ANCHOR = _ms("2026-10-09T04:00:00-04:00")
BOUNDARY = ANCHOR + 3 * 3_600_000
POPULATION = json.loads((Path(__file__).parents[1] / "fixtures" /
                         "line_repair_preopen_20261005_09.json").read_text())


def _row(stamp, price=2):
    return {"t": stamp, "o": price, "h": price, "l": price, "c": price, "v": 10}


def _provider(rows, **fields):
    payload = {"status": "OK", "ticker": "MI", "results": rows, **fields}
    rest = Mock(get_aggs=Mock(return_value=SimpleNamespace(data=json.dumps(payload).encode())))
    return MassiveAtrSeedClient("controlled-key", rest_client=rest), rest


def test_exact_one_request_and_no_massive_bar_at_or_after_0700():
    client, rest = _provider([_row(ANCHOR), _row(BOUNDARY - 60_000),
                              _row(BOUNDARY, 999), _row(BOUNDARY + 60_000, 999)])
    bars = client.fetch_preopen("MI", ANCHOR)
    assert [bar.timestamp_ms for bar in bars] == [ANCHOR, BOUNDARY - 60_000]
    rest.get_aggs.assert_called_once_with(
        "MI", 1, "minute", from_=ANCHOR, to=BOUNDARY - 1,
        adjusted=True, sort="asc", limit=50_000, raw=True,
    )


@pytest.mark.parametrize("damage", ["duplicate", "foreign", "nan", "range", "ticker",
                                    "pagination", "status", "oversize"])
def test_preopen_completeness_and_value_guards(damage):
    rows = [_row(ANCHOR)]
    fields = {}
    if damage == "duplicate":
        rows *= 2
    elif damage == "foreign":
        rows[0]["t"] -= 60_000
    elif damage == "nan":
        rows[0]["c"] = float("nan")
    elif damage == "range":
        rows[0]["l"] = 3
    elif damage == "ticker":
        fields["ticker"] = "OTHER"
    elif damage == "pagination":
        fields["next_url"] = "more"
    elif damage == "status":
        fields["status"] = "ERROR"
    else:
        rows *= 181
    client, _ = _provider(rows, **fields)
    with pytest.raises(ValueError):
        client.fetch_preopen("MI", ANCHOR)


def test_complete_empty_preopen_is_valid_sparse_history():
    client, rest = _provider([])
    assert client.fetch_preopen("MI", ANCHOR) == ()
    assert rest.get_aggs.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["MI", "VEEA", "AIXI", "DKI"])
async def test_named_join_rebuild_preserves_math_and_never_emits_historical_buy(symbol):
    from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail

    fixture = json.loads((Path(__file__).parents[1] / "fixtures" /
                          "line_repair_cutoff_1009_fresh.json").read_text())
    row = next(row for row in fixture["rows"] if row["symbol"] == symbol)
    current = row["cutoff_ms"] - 11 * 60_000
    boundary = row["anchor_ms"] + 3 * 3_600_000
    case = make_line_restore_case(symbol=symbol, now_ms=current + 61_000)
    source = SchwabV2RestClient(case.settings, on_chart_bar=None, on_quote=None)
    bars, proof = source._parse_session_history(symbol, row["anchor_ms"], current, row["schwab"])
    seed = tuple(ChartBar(symbol, *(float(bar[key]) for key in ("open", "high", "low", "close")),
                          int(bar["volume"]), bar["datetime"])
                 for bar in row["massive"] if bar["datetime"] < boundary)
    case.bot._atr_massive_seed_client = Mock(fetch_preopen=Mock(return_value=seed))
    before = deepcopy(row)
    with case.clock():
        joined, receipt = await case.bot._join_line_preopen(symbol, row["anchor_ms"], bars, proof)
        ledger = case.bot._line_sessions[symbol]
        assert case.bot._accept_line_source(symbol, ledger.epoch, joined, receipt)
        assert await case.rebuild()
        again, _ = await case.bot._join_line_preopen(symbol, row["anchor_ms"], bars, proof)
    assert joined == again and row == before
    assert case.bot._atr_massive_seed_client.fetch_preopen.call_count == 1
    expected = compute_atr_trail([
        Bar(bar["datetime"], *(bar[key] for key in ("open", "high", "low", "close", "volume")))
        for bar in row["massive"] if bar["datetime"] <= current
    ])[-1]
    snapshot = case.snapshot()
    assert (snapshot["state"], round(snapshot["trail"], 4)) == (expected["state"], expected["trail"])
    assert snapshot["buy_count"] == 0
    assert all(bar.timestamp_ms < boundary for bar in joined if bar in seed)


@pytest.mark.asyncio
async def test_seed_failure_is_one_attempt_and_preserves_existing_live_fallback_deadline():
    bot = _bot("MI", BOUNDARY)
    source = SchwabV2RestClient(bot.settings, on_chart_bar=None, on_quote=None)
    bars, proof = source._parse_session_history("MI", ANCHOR, BOUNDARY, {
        "symbol": "MI", "empty": False, "candles": [
            {"datetime": BOUNDARY, "open": 2, "high": 2, "low": 2, "close": 2, "volume": 10},
        ],
    })
    bot._atr_massive_seed_client.fetch_preopen.side_effect = TimeoutError("controlled timeout")
    for _ in range(5):
        with pytest.raises(RuntimeError, match="seed unavailable"):
            await bot._join_line_preopen("MI", ANCHOR, bars, proof)
    assert bot._atr_massive_seed_client.fetch_preopen.call_count == 1
    repair = bot._line_repair_for("MI")
    repair.first_closed_at_ms = BOUNDARY + 61_000
    bot.strategy._now_ms = lambda: BOUNDARY + 121_000
    bot._line_repair_maintenance()
    assert bot.strategy.watchlist_state("MI").line_live_fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("off,clock", [(True, BOUNDARY + 61_000), (False, ANCHOR + 17 * 3_600_000)])
async def test_off_or_afterhours_makes_no_massive_request(off, clock):
    bot = _bot("MI", BOUNDARY, strategy_schwab_1m_v2_line_chart_restoration_enabled=not off)
    bot.strategy._now_ms = lambda: clock
    bars = [ChartBar("MI", 2, 2, 2, 2, 10, BOUNDARY)]
    proof = SimpleNamespace(closed_ids=(BOUNDARY,), end_ms=BOUNDARY + 60_000, complete=True)
    assert await bot._join_line_preopen("MI", ANCHOR, bars, proof) == (bars, proof)
    bot._atr_massive_seed_client.fetch_preopen.assert_not_called()


@pytest.mark.asyncio
async def test_seed_worker_yields_and_cannot_publish_after_membership_changes():
    import threading

    bot = _bot("MI", BOUNDARY)
    started, release = threading.Event(), threading.Event()

    def held(*_):
        started.set()
        assert release.wait(2)
        return ()

    bot._atr_massive_seed_client.fetch_preopen.side_effect = held
    bot.rest_client = Mock(fetch_session_history=Mock(return_value=([ChartBar("MI", 2, 2, 2, 2, 10, BOUNDARY)],
        SimpleNamespace(closed_ids=(BOUNDARY,), end_ms=BOUNDARY + 60_000, complete=True))))
    assert bot._queue_line_source_event("MI", "first_0700_closed")
    task = asyncio.create_task(bot._line_source_events_pass())
    try:
        assert await asyncio.to_thread(started.wait, 1)
        await asyncio.sleep(0)
        bot._watchlist.clear()
        bot._sync_line_epochs()
    finally:
        release.set()
        await asyncio.wait_for(task, 2)
    assert "MI" not in bot._line_published


@pytest.mark.asyncio
@pytest.mark.parametrize("row", POPULATION["rows"], ids=lambda row: row["day"] + "-" + row["symbol"])
async def test_every_recorded_open_joins_only_pre0700_massive_and_never_replays_a_buy(row):
    from scripts.line_repair_preopen_replay import repair, setup
    from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail

    current = row["boundary_ms"] + 9 * 60_000
    case, provider = setup(row, current)
    expected = compute_atr_trail([Bar(b["t"], *(b[k] for k in ("o", "h", "l", "c", "v")))
                                  for b in row["chart"].get("results", []) if b["t"] <= current])
    current_chart = next((b for b in expected if b["ts"] == current), None)
    admitted = await repair(row, case, current)
    if current_chart is None:
        assert not admitted  # Missing comparison is not an acceptance PASS.
    else:
        assert admitted
        state = case.snapshot()
        assert (state["state"], round(state["trail"], 4), state["age"]) == (
            current_chart["state"], current_chart["trail"], current_chart["state_age"],
        )
    assert case.snapshot()["buy_count"] == 0
    assert provider.get_aggs.call_count <= 1


@pytest.mark.asyncio
async def test_mi_matching_line_does_not_waive_missing_owner_and_budget_evidence():
    from scripts.line_repair_preopen_replay import replay

    row = next(r for r in POPULATION["rows"] if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    result = await replay(row, admission_control=False)
    assert result["line_result"] == "PASS"
    assert result["result"] == "FAIL"  # Acceptance is not green on math alone.
    assert result["waiting_buy_bar_ms"] is None
    assert result["first_flip_until_0800"] == ("07:17", "BUY")
    assert result["runtime_seed_requests"] == 1


@pytest.mark.asyncio
async def test_mi_readable_flat_control_seeds_restored_sell_without_emitting_historical_buy(monkeypatch):
    from scripts import line_repair_preopen_replay as replay
    from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook

    original = replay.setup
    cases = []
    def setup(row, current, **kwargs):
        case, provider = original(row, current, **kwargs)
        strategy = case.strategy
        strategy.configure_fanout_identity_persistence(lambda *_: None)
        strategy.configure_flip_entry_ownership(lambda *_a, **_k: None,
            retry_budget_persist=lambda *_: None)
        strategy.configure_removed_wait(lambda *_: None, restored={}, readable=True)
        strategy.apply_flip_position_book(FlipPositionBook(case.now_ms, True, {}))
        feed = case.feed_bar
        async def fresh_flat_feed(*args, **kwargs):
            strategy.apply_flip_position_book(FlipPositionBook(case.now_ms, True, {}))
            return await feed(*args, **kwargs)
        case.feed_bar = fresh_flat_feed
        cases.append(case)
        return case, provider
    monkeypatch.setattr(replay, "setup", setup)
    row = next(r for r in POPULATION["rows"] if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    result = await replay.replay(row)
    restored = cases[0].strategy.watchlist_state("MI")
    assert restored.atr_short_flip_bar_ts == 1791543240000  # Historical SELL at06:54, not a live flip.
    assert restored.retry_one_segment_id == 1791543240000
    assert result["line_result"] == "PASS"
    assert result["waiting_buy_bar_ms"] is not None
    assert result["waiting_buy_bar_ms"] < row["boundary_ms"] + 17 * 60000
    assert result["rebuild_buys"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("retry_one_segment_id", 1791543240000), ("retry_one_closes_in_segment", 1),
    ("retry_one_budget_readable", False), ("flip_owner_phase", "bound"),
    ("flip_owner_opportunity_id", 123), ("flip_owner_fill_accounts", {"schwab"}),
    ("cw_resting_taken", True),
])
async def test_restored_sell_identity_never_resets_a_budget_or_owner(field, value):
    from scripts.line_repair_preopen_replay import setup
    row = next(r for r in POPULATION["rows"] if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    case, _ = setup(row, row["boundary_ms"] + 9 * 60000, admission_control=True)
    state = case.strategy.watchlist_state("MI")
    setattr(state, field, value)
    persist = Mock()
    case.strategy._retry_one_budget_persist = persist
    indicator = {"atr_state": "short", "atr_short_flip_bar_ts": 1791543240000}
    assert await case.bot._persist_line_retry_seed(state, indicator) == 0
    persist.assert_not_called()
    assert getattr(state, field) == value


@pytest.mark.asyncio
async def test_restored_sell_seed_persistence_is_offloop_and_failure_holds_entries():
    import threading
    from scripts.line_repair_preopen_replay import setup
    row = next(r for r in POPULATION["rows"] if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    case, _ = setup(row, row["boundary_ms"] + 9 * 60000, admission_control=True)
    state = case.strategy.watchlist_state("MI")
    loop_thread = threading.get_ident()
    worker_threads = []
    def fail(*_):
        worker_threads.append(threading.get_ident())
        raise RuntimeError("journal unavailable")
    case.strategy._retry_one_budget_persist = fail
    assert await case.bot._persist_line_retry_seed(state, {
        "atr_state": "short", "atr_short_flip_bar_ts": 1791543240000,
    }) == 0
    assert state.retry_one_segment_id == 0 and not state.retry_one_budget_readable
    assert len(worker_threads) == 1 and worker_threads[0] != loop_thread


@pytest.mark.asyncio
async def test_restored_sell_seed_rechecks_concurrent_close_after_worker():
    from scripts.line_repair_preopen_replay import setup
    row = next(r for r in POPULATION["rows"] if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    case, _ = setup(row, row["boundary_ms"] + 9 * 60000, admission_control=True)
    state = case.strategy.watchlist_state("MI")
    def persist(*_):
        state.retry_one_closes_in_segment = 1
    case.strategy._retry_one_budget_persist = persist
    assert await case.bot._persist_line_retry_seed(state, {
        "atr_state": "short", "atr_short_flip_bar_ts": 1791543240000,
    }) == 0
    assert state.retry_one_closes_in_segment == 1 and state.retry_one_segment_id == 0


@pytest.mark.asyncio
async def test_line_off_does_not_seed_or_write_a_restored_retry_segment():
    from scripts.line_repair_preopen_replay import setup
    row = next(r for r in POPULATION["rows"] if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    case, _ = setup(row, row["boundary_ms"] + 9 * 60000, admission_control=True)
    case.settings.strategy_schwab_1m_v2_line_chart_restoration_enabled = False
    persist = Mock()
    case.strategy._retry_one_budget_persist = persist
    state = case.strategy.watchlist_state("MI")
    assert await case.bot._persist_line_retry_seed(state, {
        "atr_state": "short", "atr_short_flip_bar_ts": 1791543240000,
    }) == 0
    persist.assert_not_called()
    assert state.retry_one_segment_id == 0


@pytest.mark.asyncio
async def test_mi_real_close_still_exhausts_the_restored_sell_segment():
    from scripts.line_repair_preopen_replay import repair, setup
    row = next(r for r in POPULATION["rows"] if r["day"] == "2026-10-09" and r["symbol"] == "MI")
    current = row["boundary_ms"] + 9 * 60000
    case, _ = setup(row, current, admission_control=True)
    assert await repair(row, case, current)
    state = case.strategy.watchlist_state("MI")
    assert state.retry_one_segment_id == 1791543240000
    state.retry_one_closes_in_segment = 1
    assert not case.strategy._strict_first_rest_admitted(state, slot="first")
    assert await repair(row, case, current + 60000)
    assert state.retry_one_segment_id == 1791543240000 and state.retry_one_closes_in_segment == 1
    assert not case.strategy._strict_first_rest_admitted(state, slot="first")


@pytest.mark.asyncio
@pytest.mark.parametrize("day,symbol", [
    ("2026-10-06", "IPDN"), ("2026-10-07", "NXTS"),
    ("2026-10-08", "FLYE"), ("2026-10-08", "HKIT"), ("2026-10-06", "LGHL"),
])
async def test_late_watch_and_sparse_tail_are_measured_without_foreign_provider_bars(day, symbol):
    from scripts.line_repair_preopen_replay import replay
    row = next(r for r in POPULATION["rows"] if r["day"] == day and r["symbol"] == symbol)
    result = await replay(row)
    assert result["result"] == "PASS"
    assert result["runtime_chart_flip_parity"]
    assert result["rebuild_buys"] == 0
    assert result["runtime_seed_requests"] == 1
    if symbol == "NXTS":
        assert result["reading_status"] == "HELD_UNSEEDED"
    if symbol == "LGHL":
        assert result["runtime_flips_ms"] == [
            (1791284460000, "SELL"), (1791286260000, "BUY"), (1791287580000, "SELL"),
        ]


def test_retained_watch_receipts_keep_extra_diagnostic_names_out_of_the_live_population():
    from scripts.line_repair_preopen_replay import REST_RECEIPTS
    absent = {(day, symbol) for (day, symbol), r in REST_RECEIPTS.items()
              if not r["watch_intervals_ms"]}
    assert absent == {("2026-10-06", "LGHL"), ("2026-10-08", "IPW"), ("2026-10-08", "MEDS")}
    assert sum(bool(r["watch_intervals_ms"]) for r in REST_RECEIPTS.values()) == 22
    assert sum(r["first_rest"] is not None for r in REST_RECEIPTS.values()) == 4
