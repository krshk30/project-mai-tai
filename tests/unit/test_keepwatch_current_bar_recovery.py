"""C11630 proposal SOURCE controls; not replay, admission or native proof."""

from dataclasses import replace

import pytest

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.keepwatch_bar_owner import (
    BarInbox, CurrentBarRecovery, OwnedBar,
)


ANCHOR = 1_792_612_800_000


def receipt(index, *, observed_delay=61_000):
    stamp = ANCHOR + index * 60_000
    return OwnedBar(ChartBar("BOLT", 6.3, 6.4, 6.2, 6.35, 100,
                            stamp, producer="schwab"),
                    "rest", ANCHOR, 1, stamp + observed_delay)


def sequence(period=5):
    return CurrentBarRecovery("BOLT", ANCHOR, 1, 7, object(), object(), period)


@pytest.mark.parametrize("count,ready", [(9, False), (10, True)])
def test_current_receipts_nine_hold_ten_threshold_without_consuming_work(count, ready):
    proof = sequence()
    inbox = BarInbox(20)
    for index in range(count):
        work = receipt(index)
        assert inbox.offer(work)
        assert proof.observe(work, canonical=work.bar, now_ms=work.observed_ms)
    assert proof.threshold_observed() is ready
    assert proof.contiguous == count
    assert inbox.accepted == len(inbox.pending) == count
    assert inbox.consumed == inbox.covered == inbox.expired == 0


def test_formula_follows_actual_period_not_hardcoded_ten():
    proof = sequence(period=3)
    for index in range(6):
        work = receipt(index)
        assert proof.observe(work, canonical=work.bar, now_ms=work.observed_ms)
        assert proof.threshold_observed() is (index == 5)


def test_same_minute_retry_does_not_count_and_correction_revokes_sequence():
    proof = sequence()
    work = receipt(0)
    assert proof.observe(work, canonical=work.bar, now_ms=work.observed_ms)
    assert proof.observe(work, canonical=work.bar, now_ms=work.observed_ms + 5_000)
    assert proof.contiguous == 1
    revised = replace(work, bar=replace(work.bar, close=6.36))
    assert not proof.observe(revised, canonical=revised.bar, now_ms=revised.observed_ms)
    assert proof.invalidated and not proof.threshold_observed()


def test_another_gap_restarts_the_contiguous_count():
    proof = sequence()
    for index in (*range(9), 11):
        work = receipt(index)
        assert proof.observe(work, canonical=work.bar, now_ms=work.observed_ms)
    assert proof.contiguous == 1 and not proof.threshold_observed()


@pytest.mark.parametrize("mutation", ["unobserved", "historical", "open", "foreign",
                                     "epoch", "anchor", "producer", "future"])
def test_pending_is_not_current_proof(mutation):
    work = receipt(0)
    now = work.observed_ms
    work = {
        "unobserved": replace(work, observed_ms=0),
        "historical": replace(work, observed_ms=work.bar.timestamp_ms + 180_001),
        "open": replace(work, observed_ms=work.bar.timestamp_ms + 59_999),
        "foreign": replace(work, bar=replace(work.bar, symbol="SAIQ")),
        "epoch": replace(work, epoch=2),
        "anchor": replace(work, anchor_ms=ANCHOR + 86_400_000),
        "producer": replace(work, bar=replace(work.bar, producer=None)),
        "future": replace(work, observed_ms=now + 1),
    }[mutation]
    proof = sequence()
    assert not proof.observe(work, canonical=work.bar, now_ms=now)
    assert proof.last_work is None and proof.contiguous == 0


def test_parked_receipt_keeps_actual_ingress_time_but_new_boot_has_no_proof():
    proof = sequence()
    for index in range(10):
        work = receipt(index)
        assert proof.observe(work, canonical=work.bar,
                             now_ms=work.observed_ms + 600_000)
    assert proof.threshold_observed()
    assert not sequence().threshold_observed()
    binding = dict(symbol=proof.symbol, anchor_ms=proof.anchor_ms,
                   epoch=proof.epoch, source_identity=proof.source_identity,
                   ledger=proof.ledger, state=proof.state)
    assert proof.bound_to(**binding)
    for key, value in (("anchor_ms", ANCHOR + 86_400_000), ("epoch", 2),
                       ("source_identity", 8), ("ledger", object()),
                       ("state", object()), ("symbol", "SAIQ")):
        assert not proof.bound_to(**(binding | {key: value}))


@pytest.mark.parametrize("count,held", [(9, True), (10, False)])
@pytest.mark.parametrize("state_only", [True, False])
def test_real_core_current_recovery_retains_old_nine_vs_ten_rule(count, held, state_only):
    from tests.unit.test_gapline1 import _hold, _seed
    from tests.unit.test_line_chart_restoration_integration import _bars, _ms
    from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms

    strategy, state = _seed(line=True)
    _hold(strategy)
    after = [bar for bar in _bars("RETO")
             if bar.timestamp_ms >= _ms("2026-10-05T11:06:00-04:00")]
    anchor = session_start_ts_ms(after[0].timestamp_ms)
    proof = CurrentBarRecovery("RETO", anchor, 1, 7, object(), state,
                               strategy._atr_period)
    publications = []
    for bar in after[:count]:
        bar = replace(bar, producer="massive")
        observed = bar.timestamp_ms + 61_000
        strategy._now_ms = lambda observed=observed: observed
        work = OwnedBar(bar, "rest", anchor, 1, observed)
        assert proof.observe(work, canonical=bar, now_ms=observed)
        draft = strategy.on_current_recovery_bar(
            "RETO", bar, state_only=state_only,
            publish=lambda actual: publications.append((actual, actual.bars[-1].timestamp_ms)))
        if state_only:
            assert draft is None
    assert state.gap_hold_active is held
    assert proof.threshold_observed() is not held
    assert state.gap_hold_contiguous_bars == count
    assert strategy._current_bar_recovery is None
    if state_only or held:
        assert publications == []
    else:
        assert publications == [(state, after[count - 1].timestamp_ms)]


def test_retained_historical_math_is_not_a_current_receipt_or_entry():
    from tests.unit.test_gapline1 import _hold, _seed
    from tests.unit.test_line_chart_restoration_integration import _bars, _ms

    strategy, state = _seed(line=True)
    _hold(strategy)
    rows = [bar for bar in _bars("RETO")
            if bar.timestamp_ms >= _ms("2026-10-05T11:06:00-04:00")]
    for bar in rows[:2]:
        assert strategy.on_current_recovery_bar("RETO", bar, state_only=True,
            publish=None, observation_phase="replay") is None
    assert state.gap_hold_active and state.gap_hold_contiguous_bars == 0
    assert state.atr_prev_bar.timestamp_ms == rows[1].timestamp_ms
    with pytest.raises(ValueError, match="historical recovery"):
        strategy.on_current_recovery_bar("RETO", rows[2], state_only=False,
            publish=None, observation_phase="replay")


@pytest.mark.asyncio
@pytest.mark.parametrize("count,ready", [(9, False), (10, True)])
@pytest.mark.parametrize("start", [180, 330, 715], ids=["PM-0700", "RTH-0930", "AH-1605"])
@pytest.mark.parametrize("began_hold", [True, False], ids=["gap-hold", "no-gap-hold"])
async def test_real_dispatch_traded_gap_current_scope_does_not_attest_history(
        count, ready, start, began_hold, monkeypatch):
    from unittest.mock import AsyncMock

    from project_mai_tai.market_data.keepwatch_history import history_coverage
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
    from project_mai_tai.settings import Settings
    from tests.unit.test_keepwatch_session_producer_pin import candle, response

    bars = tuple(candle(index, close=10 - offset * .1)
                 for offset, index in enumerate(range(start - 42, start - 2)))
    bot = SchwabV2BotService(Settings(_env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True,
        strategy_schwab_1m_v2_gap_hold_enabled=True,
        strategy_schwab_1m_v2_gap_line_carry_enabled=True))
    assert bot.strategy._cw_armed_segment_safety_enabled
    assert bot.strategy._entries_held
    assert not bot._boot_state_restoration_complete
    assert not bot._boot_exclusion_sources_readable
    clock = [bars[-1].timestamp_ms + 61_000]
    bot.strategy._now_ms = lambda: clock[0]
    bot._watchlist = {"IZM"}  # Observation membership only; native remains UNKNOWN.
    bot._sync_line_epochs()
    ledger = bot._line_sessions["IZM"]
    for bar in bars:
        bot._observe_line_bar("IZM", bar)
        bot.strategy.on_observed_bar("IZM", bar, observation_phase="replay")
    history = response(bars, ())
    ledger.attest(history_coverage(history, tuple(ledger._bars.values()), canonical=True))
    bot._keepwatch_proven["IZM"] = history
    assert await bot._rebuild_session_line("IZM", ledger)
    original_publication = bot._line_published["IZM"]
    state = bot.strategy.watchlist_state("IZM")
    first = candle(start, producer="schwab", close=6.4)
    clock[0] = first.timestamp_ms + 61_000
    if began_hold:
        assert bot.strategy.begin_gap_hold("IZM", detected_at_ms=clock[0],
                                          last_bar_age_s=181, last_print_age_s=1)
    ledger.observe_trade(candle(start - 1).timestamp_ms + 1_000)
    read = AsyncMock(return_value=response(bars, (first,), now=clock[0]))
    bot._read_keepwatch_history_owned = read  # Controlled provider input, not a permission stub.
    inbox = bot._keepwatch_bar_inboxes["IZM"] = BarInbox(20)

    async def consume(symbol, bar):
        bot._keepwatch_fence_strategy_consumption(symbol)
        bot._strategy_on_bar(symbol, bar, observation_phase="live")

    for index in range(count):
        bar = candle(start + index, producer="schwab", close=6.4)
        clock[0] = bar.timestamp_ms + 61_000
        canonical = bot._keepwatch_pins_for("IZM", ledger.anchor_ms).accept(
            bar, now_ms=clock[0])
        ledger.observe(canonical)
        work = OwnedBar(canonical, "rest", ledger.anchor_ms, ledger.epoch, clock[0])
        assert inbox.offer(work)
        if start == 715 and index == 9:
            state.cw_armed = True  # Controlled pre-close arm; the real post-close guard must retire it.
        assert await bot._keepwatch_dispatch_bar("IZM", canonical, consume, expected_work=work)
        inbox.complete(work)
    assert read.await_count == 1  # Repair first; subsequent recovery checks are memory-only.
    assert (bot._keepwatch_current_publication("IZM") is not None) is ready
    assert state.gap_hold_active is (began_hold and not ready)
    assert "IZM" in bot._keepwatch_unknown
    assert ledger.has_unrecovered_traded_gap() and ledger.prepare() is None
    assert bot._line_published["IZM"] is original_publication
    assert bot.strategy._entries_held and not bot._entry_membership_allowed("IZM")
    assert inbox.accepted == inbox.consumed == count and not inbox.pending
    assert inbox.covered == inbox.expired == 0
    if start == 715 and ready:
        assert bot.strategy._entry_window_closed_for_session()
        assert not state.cw_armed
    if ready:
        def forbidden_scan(*args, **kwargs):
            raise AssertionError("a current admission lookup must not construct subscriptions or scan gaps")

        with monkeypatch.context() as patch:
            patch.setattr(bot, "_subscription_symbols", forbidden_scan)
            patch.setattr(ledger, "has_unrecovered_traded_gap", forbidden_scan)
            assert bot._keepwatch_current_recovery_scope("IZM") is not None
            assert bot._keepwatch_current_publication("IZM") is not None
            assert bot._line_version("IZM").startswith("current:")
            assert not bot._line_buy_ready("IZM")  # Native/boot/membership are not invented.
        bot._watchlist.clear()
        bot._exit_coverage.add("IZM")
        assert "IZM" in bot._subscription_symbols()
        assert bot._keepwatch_current_publication("IZM") is None
        assert not bot._keepwatch_entry_allowed("IZM")
        bot._watchlist.add("IZM")
    bot._stop_event.set()
    assert bot._keepwatch_current_publication("IZM") is None
    bot._stop_event.clear()
    clock[0] = ledger.anchor_ms + 86_400_000
    assert bot._keepwatch_current_publication("IZM") is None


@pytest.mark.parametrize("enabled", [False, True], ids=["CW-safety-OFF", "CW-safety-ON"])
def test_real_safety_configuration_does_not_invent_boot_exclusion_readability(enabled):
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
    from project_mai_tai.settings import Settings

    bot = SchwabV2BotService(Settings(_env_file=None,
        strategy_schwab_1m_v2_keepwatch1_enabled=True,
        strategy_schwab_1m_v2_line_chart_restoration_enabled=True,
        strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=enabled))
    assert bot.strategy._cw_armed_segment_safety_enabled is enabled
    assert bot.strategy._entries_held is enabled
    assert not bot._boot_state_restoration_complete
    assert not bot._boot_exclusion_sources_readable
    assert not bot._entry_membership_allowed("IZM")
    assert bot._keepwatch_current_publication("IZM") is None
