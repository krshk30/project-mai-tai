"""Original JZ legacy UNKNOWN is not a synthetic no-dispatch/order positive."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter, SchwabV2Strategy, TradeIntentDraft
from project_mai_tai.v2_removed_wait import RemovedWait, assess_removed_wait
from tests.unit.test_clearwait1_session_rollover import strategy

RAW = json.loads((Path(__file__).parents[1] / "fixtures/clearwait1_jz_20261009.json").read_text())


def row(raw):
    return SimpleNamespace(**{key: datetime.fromisoformat(value)
        if key in {"created_at", "updated_at"} else value for key, value in raw.items()})


@pytest.mark.asyncio
@pytest.mark.parametrize("archived,reason", [(False, "dispatch_history_unknown"), (True, "removal_not_durable")])
async def test_original_jz_readd_fresh_sell_cannot_invent_legacy_clear(archived, reason, monkeypatch):
    payload = RAW["request"]["payload"]
    req = RemovedWait("JZ", int(payload["opportunity_id"]), payload["token"],
                      int(payload["requested_at_ms"]), tuple(payload["account_names"]))
    snapshot = row(RAW["request"])
    if not archived:
        # Replay the original type before its recorded manual archive; identity is unchanged.
        snapshot.snapshot_type = "v2_removed_wait"
    clock = [int(datetime.fromisoformat(RAW["readded_at"]).timestamp() * 1000)]
    class ReplayClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(clock[0] / 1000, tz or UTC)
    monkeypatch.setattr("project_mai_tai.services.schwab_1m_v2_bot.datetime", ReplayClock)
    proof = assess_removed_wait(req, now=datetime.fromtimestamp(clock[0] / 1000, UTC),
        accounts=RAW["accounts"], intents=[row(r) for r in RAW["receipts"]], orders=[],
        filled_order_ids=set(), order_events=[], snapshots=[snapshot], has_position=False)
    assert not proof.clear and proof.reason == reason
    assert req.opportunity_id == 0 and not RAW["dispatch_journal_retained"]
    assert all("target_client_order_id" not in r["payload"]["metadata"] for r in RAW["receipts"])
    assert RAW["broker_inventory"] == "UNMEASURED"
    strat = SchwabV2Strategy(strategy(req).settings.model_copy(update={
        "strategy_schwab_1m_v2_confirmed_window_enabled": True}))
    strat._removed_wait_requests = {"JZ": req}
    strat._now_ms = lambda: clock[0]
    strat.scanner_readded("JZ")
    strat.apply_removed_wait_proofs([proof])
    assert not strat.line_buy_ready("JZ")
    sell = RAW["sell"]
    clock[0] = sell["observed_at_ms"]
    state = strat.watchlist_state("JZ")
    state.bars.append(SimpleNamespace(timestamp_ms=sell["bar_time_ms"], close=Decimal(sell["close"])))
    observation = strat._observe_atr_sell(state, {"flip": "SELL", "trail": Decimal(sell["trail"])})
    assert observation is not None and observation.decision_id == sell["decision_id"]
    transport = SimpleNamespace(xadd=AsyncMock(return_value="controlled-observation-publication"))
    bot = SchwabV2BotService(strat.settings)
    bot.strategy = strat
    bot.intent_emitter = SchwabV2IntentEmitter(strat.settings, transport, broker_account_name=req.account_names[0])
    await bot._drain_atr_sell_observations()
    assert transport.xadd.await_count == 1
    assert json.loads(transport.xadd.await_args.args[1]["data"])["event_type"] == "v2_atr_sell_observation"
    assert bot._removed_wait_assessment_triggers[req][0] == "fresh_sell"
    for name in req.account_names:
        emitter = SimpleNamespace(broker_account_name=name, emit=AsyncMock())
        draft = TradeIntentDraft("JZ", "buy", "open", Decimal(1), "controlled waiting BUY probe", {})
        assert not await bot._emit_removal_tracked(emitter, draft)
        emitter.emit.assert_not_awaited()
    assert strat._removed_wait_requests == {"JZ": req}
    assert not strat.line_buy_ready("JZ")
