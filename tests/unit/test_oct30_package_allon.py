"""SOURCE UNRUN: composed package consumers, controlled private scope only."""
from decimal import Decimal
import json
import logging

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import Fill, OmsManagedPosition, VirtualPosition
from project_mai_tai.market_data.schwab_v2_streamer import SchwabV2Streamer
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_wbattach1_riskoff import make_case as make_case, closes
from tests.unit.test_wbexit2_route import close_case
from tests.unit.test_wbexit3_obligation import obligations
from tests.unit.test_five_service_install_evidence import PROOF, native_case
from tests.unit.oct30_allon_control import (
    INSTALL_FLAGS, PM_AT, PM_MS, allon_owned_case, assert_allon, drain, profile, real_bot, settings_for_allon,
)
from tests.unit.wbexit3_narrow_boot_fixture import _physical_join
from project_mai_tai.settings import Settings


@pytest.mark.asyncio
async def test_actual_three_ON_consumers_PM_refusal_flip_logs_and_owned_WB3_exit(
        make_case, monkeypatch, caplog, record_property):
    case, settings = await allon_owned_case(make_case)
    bot = None
    added = []
    try:
        bot, pm_state, phone = real_bot(settings, case[1], monkeypatch)
        # Actual OMS configure_logging(force=True) ran before this capture.
        # Attach the SAME pytest observer to actual diagnostic loggers only.
        for logger in (bot_module.logger, strategy_module.logger):
            if caplog.handler not in logger.handlers:
                logger.addHandler(caplog.handler)
                added.append(logger)
        caplog.set_level(logging.INFO)
        raw = {"0": "NCPL", "1": 1.54, "2": 1.55, "3": 1.55,
            "35": PM_MS - 100, "9": 100, "conditions": [0]}
        ticks = SchwabV2Streamer._extract_level_one_ticks(raw, PM_MS)
        for tick in ticks:
            await bot._handle_stream_tick(tick)  # Real parser + callback + strategy guards.
        assert pm_state.resting_flip_ms == 0 and bot.strategy._entries_held is True
        assert bot._eh_stream_emit_tasks == set() and bot.strategy._pending_webull_fanout_intents == []
        assert len(bot.strategy._flip_print_observer._trades["NCPL"]) == 1
        assert [value.kind for value in ticks] == ["trade", "quote"]
        assert list(bot.tick_writer._buf) == ticks and all(value.raw is raw for value in bot.tick_writer._buf)
        assert bot.tick_writer.stats() == {"buffered": 2, "ticks_written": 0, "dropped": 0}
        pm = [json.loads(row.getMessage().split(" ", 1)[1]) for row in caplog.records
              if row.getMessage().startswith("[V2-PM-SOFTREST-QUOTE]")]
        assert pm and pm[-1]["decision"] == "REFUSED" and pm[-1]["reason"] == "line_not_ready"

        # Existing real ATR update consumes four controlled bars. Entry-emission
        # guards remain installed; this invokes no intent emitter or provider.
        bot.strategy._atr_period, bot.strategy._atr_factor = 1, 1.0
        state = bot.strategy.watchlist_state("AIOK")
        bar = PM_MS // 60000 * 60000
        bars = [OHLCVBar(bar, 2.0, 2.1, 1.9, 2.0, 100),
            OHLCVBar(bar + 60000, 2.0, 2.1, 1.9, 2.0, 100),
            OHLCVBar(bar + 120000, 1.7, 1.8, 1.4, 1.5, 200),
            OHLCVBar(bar + 180000, 2.4, 2.6, 2.3, 2.5, 300)]
        actual = [bot.strategy._update_atr_state(state, value, observation_phase="live") for value in bars]
        assert [value["flip"] if value is not None else None for value in actual] == [None, None, "SELL", "BUY"]
        flips = [json.loads(row.getMessage().split(" ", 1)[1]) for row in caplog.records
                 if row.getMessage().startswith("[V2-FLIP-PRINT-DIAGNOSTIC]")]
        assert [row["flip"] for row in flips] == ["SELL", "BUY"]
        assert all(row["deciding_print_ts"] == "UNKNOWN" and row["observed_last_trade"] == "UNKNOWN" for row in flips)
        assert phone.calls == []

        assert case[0].settings is bot.settings is bot.strategy.settings is settings
        assert_allon(settings)
        case[2].bid = 9.4  # CONTROLLED executable hard-stop quote, not a permission stub.
        await _physical_join(close_case(case, "CW_HARD_STOP"))
        assert [request.quantity for request in closes(case[2])] == [Decimal(10)]
        assert case[2].total == 1000 and obligations(case)[0]["phase"] == "RESOLVED"
        with case[1]() as db:
            row = db.scalar(select(OmsManagedPosition))
            assert row.status == "closed" and row.current_quantity == 0
            assert db.scalar(select(VirtualPosition.quantity)) == 0
            assert sorted((value.side, value.quantity) for value in db.scalars(select(Fill))) == [
                ("buy", Decimal(10)), ("sell", Decimal(10))]
        assert_allon(settings)
        record_property("OCT30_ALLON_actual_consumers", json.dumps({
            "label": "CONTROLLED_PRIVATE_NOT_NATIVE_INSTALL_PASS", "profile": profile(),
            "same_Settings_object": True, "PM": pm, "ATR": flips,
            "PM_controlled_clock": PM_AT.isoformat(), "OMS_controlled_clock": case[4][0].isoformat(),
            "temporal_native_link_between_cases": "NOT_CLAIMED",
            "tick_writer_buffer": bot.tick_writer.stats(), "writer_run_flush_PG": "NOT_EXECUTED",
            "WB3": obligations(case), "own_sell": "10", "manual_left": "1000",
            "control": "REAL_CONTROLLED_SQLITE_SEED_AND_REFRESH", "global_entry_GO": "NOT_CLAIMED"}, default=str))
    finally:
        try:
            await drain(case, bot)
        finally:
            for logger in added:
                logger.removeHandler(caplog.handler)


def test_ALLON_runner_strict_inflight_empty_refuses_five_CONTROLLED_holds(record_property):
    settings = settings_for_allon(Settings(_env_file=None))
    assert_allon(settings)
    value, line, accounts, owner, now = native_case()
    held = ["CONTROLLED:" + symbol for symbol in ("FRGT", "GLE", "IPST", "NCPL", "PCLA")]
    value["inflight"] = held
    assert all(getattr(settings, name) is True for name in INSTALL_FLAGS)
    with pytest.raises(ValueError, match="local unresolved in-flight obligation"):
        PROOF.native_census_evidence(json.dumps(value).encode(), line, accounts, owner, now)
    # Same controlled evidence with a literal empty list proves the failed arm
    # reached the unchanged inflight gate, rather than an unrelated invalid row.
    value["inflight"] = []
    result = PROOF.native_census_evidence(json.dumps(value).encode(), line, accounts, owner, now)
    assert result["generation"] == 7
    record_property("OCT30_ALLON_runner", json.dumps({"label": "CONTROLLED_GUARD_ONLY",
        "held": held, "strict_inflight": "MUST_EQUAL_EMPTY_LIST", "outcome": "REFUSED",
        "actual_processes_native_census_install_watch": "NOT_EXECUTED", "flags": list(INSTALL_FLAGS)}))
