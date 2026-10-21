"""MOMOLD1 MUST1/2. Actual retained call-boundary inputs; SYNTH owner transport.

Missing actual AMOD input is an explicit gate failure, never a skipped pass.
Nothing here claims native order acceptance or current installed-box ownership.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.momold1_live_offer_proof import (
    ET, EXPORT, compare, load_records, offer_method, record_calls, sha, source_pair,
)
from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.momentum_live.admission import EpisodeLedger, Route, Signal
from project_mai_tai.momentum_live.owner_proof import OwnerProofPublisher, replace_quote_owner
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momwarn1_gateway import event, service as gateway_service


ROOT = Path(__file__).resolve().parents[2]


def test_actual_1020_warning_detected_offer_bytes_match_pre_post_and_bound_method():
    path = EXPORT / "momentum_paper_events_1020.csv"
    assert sha(path.read_bytes()) == "30ab970778f89bcd1dcaff783f04a30a1c61735081658c3bce3db7d8d572cb1c"
    records = load_records(path)
    assert all(str(record.session_date) == "2026-10-20" for record in records)
    pre, post = source_pair(ROOT)
    report = compare(pre, post, records)
    assert report["equal"] and report["records"] > 0
    method, metadata = offer_method(post)
    for case in report["cache_cases"]:
        extracted = record_calls(method, records, cache_case=case,
                                 stale_seconds=metadata["snapshot_stale_seconds"])
        bound = record_calls(MomentumPaperService._offer_live_signals, records, cache_case=case,
                             stale_seconds=metadata["snapshot_stale_seconds"])
        assert extracted == bound
        assert len(report["cache_cases"][case]["post"]) == len(records)
        expected_volume = 123456 if case in {"fresh", "at_boundary"} else None
        assert all(row["day_volume"] == expected_volume for row in report["cache_cases"][case]["post"])


def test_actual_amod_1021_0815_offer_bytes_require_exact_retained_payload():
    path = Path(os.environ.get("MAI_TAI_MOMOLD1_AMOD_EXPORT",
        str(EXPORT / "amod_warning_detected_1021_0815.json")))
    assert path.is_file(), f"MUST1 BLOCKED: exact retained AMOD payload not delivered: {path}"
    records = [record for record in load_records(path) if record.symbol == "AMOD"
        and datetime.fromtimestamp(record.payload["observed_decision_ns"] / 1e9, UTC)
        .astimezone(ET).strftime("%Y-%m-%d %H:%M") == "2026-10-21 08:15"]
    assert records, "MUST1 BLOCKED: delivered file lacks exact AMOD 10-21 08:15 DETECTED"
    assert compare(*source_pair(ROOT), records)["equal"]


def test_synth_offer_mutation_is_detected_without_modifying_source():
    pre, post = source_pair(ROOT)
    needle = b"day_volume=volume, now_ns=now_ns"
    assert needle in post
    changed = post.replace(needle, b"day_volume=None, now_ns=now_ns", 1)
    report = compare(pre, changed, load_records(EXPORT / "momentum_paper_events_1020.csv"))
    assert report["equal"] is False
    assert report["cache_cases"]["fresh"]["equal"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["working", "held"])
async def test_synth_live_owner_keeps_provider_TQ_when_dormant_paper_and_warning_clear(state):
    from datetime import date

    stamp = int(datetime(2026, 10, 21, 12, 15, tzinfo=UTC).timestamp() * 1e9)
    book = EpisodeLedger.restore([])
    episode, fresh = book.claim(Signal("2026-10-21", "AMOD", "SYNTH-owner",
        stamp // 1_000_000 - 1000, stamp // 1_000_000, Decimal("2"), stamp, 100000), now_ns=stamp)
    assert fresh
    book.begin_submit(episode.episode_id, Route.SCHWAB, now_ns=stamp)
    if state == "held":
        book.reconcile(episode.episode_id, cumulative_filled=Decimal(1), cumulative_exited=Decimal(0),
                       now_ns=stamp, terminal_entry=True, first_fill_ns=stamp)
    else:
        episode.state = "working"  # SYNTH accepted working-order observation, no wire call.
    gateway, provider, _, _ = gateway_service()
    gateway.settings.market_data_warmup_enabled = False
    redis = AsyncMock()

    async def replace_owner(symbols):
        await replace_quote_owner(redis, "test", symbols, maxlen=64)
        request = MarketDataSubscriptionEvent.model_validate_json(redis.xadd.await_args.args[1]["data"])
        await gateway.apply_subscription_event(request)

    publisher = OwnerProofPublisher(redis, "test", book, producer_instance_id="123:456",
        account_names=("live:orb", "live:schwab_1m_v2"),
        bindings={episode.episode_id: "live:schwab_1m_v2"}, enabled=lambda: True,
        replace_owner=replace_owner, state_lock=asyncio.Lock(), clock_ns=lambda: stamp)
    await publisher.publish_once()
    paper = MomentumPaperService(Settings(_env_file=None, momentum_live_enabled=False), redis_client=AsyncMock())
    paper.settings.momentum_live_enabled = True
    paper._engine = MomentumPaperEngine(prior_closes={"AMOD": Decimal(2)},
        condition_version="SYNTH", coverage_started_ms=stamp // 1_000_000, strategy_windows={})
    paper._warning = MomentumWarningConsumer(prior_closes={"AMOD": Decimal(2)},
        condition_version="SYNTH", coverage_started_ms=stamp // 1_000_000)
    paper._session_date = date(2026, 10, 21)
    await gateway.apply_subscription_event(event("momentum-warning", ["AMOD"]))
    await gateway.apply_subscription_event(event("momentum-paper", ["AMOD"]))
    provider._ws.removed.clear()
    await paper._sync_gateway_subscriptions(force=True)
    for call in paper.redis.xadd.await_args_list:
        request = MarketDataSubscriptionEvent.model_validate_json(call.args[1]["data"])
        assert request.payload.consumer_name != "momentum-live"
        await gateway.apply_subscription_event(request)
    assert paper._warning.desired_symbols(set()) == set()
    assert paper._engine.active_symbols == set()
    assert provider._subscriptions == {"AMOD"} and provider._warning_trade_symbols == set()
    assert "T.AMOD" not in provider._ws.removed and "Q.AMOD" not in provider._ws.removed
    assert gateway._desired_symbols_by_consumer["momentum-live"] == {"AMOD"}
    # Positive release control: only the actual LIVE owner's terminal state releases T/Q.
    book.reconcile(episode.episode_id, cumulative_filled=episode.filled,
        cumulative_exited=episode.filled, now_ns=stamp + 1, terminal_entry=True)
    await publisher.publish_once()
    assert provider._subscriptions == set()
    assert "T.AMOD" in provider._ws.removed and "Q.AMOD" in provider._ws.removed
