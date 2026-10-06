"""Acceptance-adapter controls; recorded candles, not new historical PASS claims."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from project_mai_tai.confirmation_exit import ConfirmationEntry
from project_mai_tai.strategy_core.schwab_1m_v2 import TradeIntentDraft
from project_mai_tai.strategy_core.session_line_restore import SessionCoverage, history_fingerprint
from tests.line_restore_acceptance_factory import make_line_restore_case
from tests.unit.test_all_on_pm import ALL_ON

FIXTURES = Path(__file__).parents[1] / "fixtures"
RETO = [row for row in json.loads((FIXTURES / "line_chart_restoration_bars.json").read_text())["bars"]
        if row["symbol"] == "RETO" and datetime.fromisoformat(row["bar_time"])
        <= datetime(2026, 10, 5, 15, 18, tzinfo=UTC)]
CURRENT = int(datetime.fromisoformat(RETO[-1]["bar_time"]).timestamp() * 1000)


async def populated_case(**overrides):
    case = make_line_restore_case(symbol="RETO", now_ms=CURRENT + 61_000,
                                  settings_overrides=overrides)
    for row in RETO:
        await case.feed_bar(row)
    return case


def controlled_proof(case):
    """Synthetic coverage ONLY for adapter controls, not historical coverage."""
    bars = [case.bar(row) for row in RETO]
    ledger = case.bot._line_sessions["RETO"]
    return SessionCoverage("schwab_rest_full_session", ledger.anchor_ms,
                           CURRENT + 60_000, tuple(bar.timestamp_ms for bar in bars),
                           True, history_fingerprint(bars), prefix_complete=True)


@pytest.mark.asyncio
async def test_factory_feeds_real_service_and_never_infers_coverage_from_bar_count():
    case = await populated_case()
    assert case.bot.session_factory is None and case.bot.redis is None
    assert case.bot.rest_client is None and case.bot.streamer is None
    assert case.bot.strategy is case.strategy
    assert len(case.bot._line_sessions["RETO"]._bars) == len(RETO)
    assert not await case.rebuild()
    snapshot = case.snapshot()
    assert snapshot["incomplete_reason"] == "coverage_unproven"
    assert not snapshot["entry_allowed"] and snapshot["buy_count"] == 0


@pytest.mark.asyncio
async def test_factory_recorded_reto_service_publication_matches_known_trail():
    case = await populated_case()
    case.attest(controlled_proof(case))
    assert await case.rebuild()
    snapshot = case.snapshot()
    assert snapshot["state"] == "long"
    assert round(snapshot["trail"], 4) == 2.0639
    assert snapshot["entry_allowed"] and snapshot["buy_count"] == 0


@pytest.mark.asyncio
async def test_factory_missing_coverage_blocks_real_direct_drain_for_both_legs_but_not_close():
    case = await populated_case()
    primary = TradeIntentDraft("RETO", "buy", "open", Decimal(1), "ATR Flip")
    mirror = TradeIntentDraft("RETO", "buy", "open", Decimal(1), "ATR Flip")
    close = TradeIntentDraft("RETO", "sell", "close", Decimal(1), "protective exit")
    case.strategy._pending_intents.extend([primary, close])
    case.strategy._pending_webull_direct_intents.append(mirror)
    with case.clock():
        await case.bot._drain_direct_strategy_intents()
    assert case.schwab.intents == [close]
    assert case.webull.intents == []
    assert case.snapshot()["buy_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("complete", [False, True])
async def test_all_eight_on_real_factory_requires_provider_proof_preserves_protective_sell(complete):
    case = await populated_case(**ALL_ON)
    assert all(getattr(case.settings, key) is True for key in ALL_ON)
    assert case.strategy._line_readiness == case.bot._line_buy_ready
    if complete:
        case.attest(controlled_proof(case))
    assert bool(await case.rebuild()) is complete
    assert case.snapshot()["entry_allowed"] is complete
    metadata = {"line_restore_version": case.bot._line_version("RETO")}
    primary = TradeIntentDraft("RETO", "buy", "open", Decimal(1), "ATR Flip", dict(metadata))
    mirror = TradeIntentDraft("RETO", "buy", "open", Decimal(1), "ATR Flip", dict(metadata))
    close = TradeIntentDraft("RETO", "sell", "close", Decimal(1), "protective exit")
    case.strategy._pending_intents.extend([primary, close])
    case.strategy._pending_webull_direct_intents.append(mirror)
    with case.clock():
        await case.bot._drain_direct_strategy_intents()
    assert close in case.schwab.intents
    assert case.snapshot()["buy_count"] == (2 if complete else 0)
    assert case.schwab.intents == ([primary, close] if complete else [close])
    assert case.webull.intents == ([mirror] if complete else [])


@pytest.mark.asyncio
async def test_factory_recording_emitters_capture_permitted_buys_not_missing_emitter_false_pass():
    case = await populated_case()
    case.attest(controlled_proof(case))
    assert await case.rebuild()
    metadata = {"line_restore_version": case.bot._line_version("RETO")}
    primary = TradeIntentDraft("RETO", "buy", "open", Decimal(1), "ATR Flip", dict(metadata))
    mirror = TradeIntentDraft("RETO", "buy", "open", Decimal(1), "ATR Flip", dict(metadata))
    case.strategy._pending_intents.append(primary)
    case.strategy._pending_webull_direct_intents.append(mirror)
    with case.clock():
        await case.bot._drain_direct_strategy_intents()
    assert case.snapshot()["buy_count"] == 2
    assert case.schwab.intents == [primary] and case.webull.intents == [mirror]


@pytest.mark.asyncio
async def test_factory_retains_real_confirmation_evaluation_and_one_shot_publication():
    case = await populated_case(strategy_schwab_1m_v2_confirmation_exit_enabled=True)
    entry = ConfirmationEntry(uuid4(), uuid4(), "fill", "order", "live:schwab_1m_v2", "RETO",
                              datetime.fromtimestamp((CURRENT - 60_000) / 1000, UTC), CURRENT,
                              1, None, datetime(1970, 1, 1, tzinfo=UTC))
    case.bot._confirmation_exit.add(entry)
    case.bot._line_live_bars["RETO"] = {CURRENT}
    case.attest(controlled_proof(case))
    assert await case.rebuild()
    decisions = case.snapshot()["confirmation_decisions"]
    assert len(decisions) == 1
    assert decisions[0].atr_state == "long" and not decisions[0].should_exit
    assert case.schwab.confirmation_exits == decisions
    assert entry.fill_id in case.confirmation_published
    assert await case.rebuild()
    assert len(case.schwab.confirmation_exits) == 1


@pytest.mark.asyncio
async def test_factory_readd_replaces_epoch_without_replaying_old_permission():
    case = await populated_case()
    case.attest(controlled_proof(case))
    assert await case.rebuild()
    epoch = case.snapshot()["epoch"]
    case.remove()
    case.add()
    assert case.snapshot()["epoch"] > epoch
    assert not case.snapshot()["entry_allowed"]
    assert not await case.rebuild()


def test_factory_clock_restores_module_bindings_and_uses_recorded_event_time():
    import project_mai_tai.services.schwab_1m_v2_bot as module

    original = module.datetime
    case = make_line_restore_case(symbol="RETO", now_ms=CURRENT + 61_000)
    with case.clock():
        assert int(module.datetime.now(UTC).timestamp() * 1000) == CURRENT + 61_000
        assert case.strategy._now_ms() == CURRENT + 61_000
    assert module.datetime is original


@pytest.mark.asyncio
async def test_factory_refuses_lookahead_and_foreign_symbol():
    case = make_line_restore_case(symbol="RETO", now_ms=CURRENT)
    with pytest.raises(ValueError, match="not closed"):
        await case.feed_bar(RETO[-1])
    with pytest.raises(ValueError, match="foreign symbol"):
        await case.feed_bar(dict(RETO[-1], symbol="JAGX"))


@pytest.mark.parametrize("overrides", [
    {"strategy_schwab_1m_v2_line_chart_restoration_enabled": False},
    {"misspelled_enabled": True},
])
def test_factory_refuses_dark_or_unknown_acceptance_settings(overrides):
    with pytest.raises(ValueError):
        make_line_restore_case(symbol="RETO", now_ms=CURRENT, settings_overrides=overrides)
