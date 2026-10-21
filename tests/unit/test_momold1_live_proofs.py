"""Portable SYNTH offer contracts and MUST2 owner transport controls.

Mandatory actual MUST1 inputs live in tests/acceptance/test_momold1_retained_acceptance.py.
These unit controls cannot satisfy retained-input or installed-box acceptance.
"""

import asyncio
import csv
from datetime import UTC, date, datetime
from decimal import Decimal
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts.momold1_live_offer_proof import (
    APP, CSV_MAX_FIELD_BYTES, compare, load_records, offer_method, record_calls,
)
from project_mai_tai.events import MarketDataSubscriptionEvent
from project_mai_tai.momentum_live.admission import EpisodeLedger, Route, Signal
from project_mai_tai.momentum_live.owner_proof import OwnerProofPublisher, replace_quote_owner
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MomentumTapeRecord
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.services.momentum_paper_app import MomentumPaperService
from project_mai_tai.settings import Settings
from tests.unit.test_momwarn1_gateway import event, service as gateway_service


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def synth_records():
    now = datetime(2026, 10, 21, 14, tzinfo=UTC)
    stamp = int(now.timestamp() * 1e9)
    return [MomentumTapeRecord(event_key=f"SYNTH:{code}:DETECTED", logical_id=f"SYNTH:{code}",
        strategy_code=code, event_type="DETECTED", session_date=date(2026, 10, 21),
        symbol="SYNTH", observed_at=now, payload={"observed_decision_ns": stamp,
            "detect": {"price": "2.125", "sip_ts_ms": stamp // 1_000_000},
            "reference": {"price": "1.50", "sip_ts_ms": stamp // 1_000_000 - 1000}})
        for code in ("momentum_30s_warn", "momentum_60s_warn")]


def test_synth_offer_copy_equality_and_extracted_method_match_bound_method(synth_records):
    records = synth_records
    post = (ROOT / APP).read_bytes()
    # Controlled identical source copies test the proof machinery, not 99fc parity.
    report = compare(post, post, records)
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


def test_synth_offer_mutation_is_detected_without_modifying_source(synth_records):
    pre = post = (ROOT / APP).read_bytes()
    needle = b"day_volume=volume, now_ns=now_ns"
    assert needle in post
    changed = post.replace(needle, b"day_volume=None, now_ns=now_ns", 1)
    report = compare(pre, changed, synth_records)
    assert report["equal"] is False
    assert report["cache_cases"]["fresh"]["equal"] is False


def test_missing_retained_input_is_refused_without_synthetic_fallback(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_records(tmp_path / "missing-retained.json")


def test_empty_retained_input_is_refused_without_vacuous_pass(tmp_path):
    path = tmp_path / "SYNTH-empty.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="no retained warning DETECTED records"):
        load_records(path)


@pytest.mark.parametrize("sealed_ns", [None, "1792591200000000000", -1])
def test_unusable_sealed_clock_is_refused_without_current_clock_fallback(tmp_path, sealed_ns):
    path = tmp_path / "SYNTH-invalid-clock.json"
    path.write_text(json.dumps([{"event_type": "DETECTED", "strategy_code": "momentum_30s_warn",
        "payload": {"observed_decision_ns": sealed_ns, "detect": {"price": "2"}}}]))
    with pytest.raises(ValueError, match="retained observed_decision_ns required"):
        load_records(path)


def write_synth_csv(path, records, session_payload):
    with path.open("w", encoding="utf-8", newline="") as handle:
        fields = ["event_key", "logical_id", "strategy_code", "event_type", "session_date",
                  "symbol", "observed_at", "payload"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"event_key": "SYNTH:SESSION_READY", "logical_id": "SYNTH:session",
            "strategy_code": "momentum_paper", "event_type": "SESSION_READY", "session_date": "2026-10-21",
            "symbol": "*", "observed_at": "2026-10-21T14:00:00+00:00", "payload": session_payload})
        for record in records:
            writer.writerow({**{field: str(getattr(record, field)) for field in fields if field != "payload"},
                             "payload": json.dumps(record.payload)})


def test_synth_large_session_field_does_not_prevent_detected_records_loading(tmp_path, synth_records):
    path = tmp_path / "SYNTH-large-session.csv"
    write_synth_csv(path, synth_records, json.dumps({"SYNTH_SESSION_READY": "x" * 200_000}))
    original = csv.field_size_limit(131072)
    try:
        records = load_records(path)
        assert [record.logical_id for record in records] == [record.logical_id for record in synth_records]
        assert [record.payload for record in records] == [record.payload for record in synth_records]
        assert csv.field_size_limit() == 131072
    finally:
        csv.field_size_limit(original)


@pytest.mark.parametrize("character", ["x", "\u00e9"])
def test_synth_oversized_session_field_is_refused_even_though_not_a_detected_row(tmp_path, synth_records, character):
    assert CSV_MAX_FIELD_BYTES == 8 * 1024 * 1024
    repetitions = CSV_MAX_FIELD_BYTES // len(character.encode("utf-8")) + 1
    path = tmp_path / "SYNTH-over-limit.csv"
    write_synth_csv(path, synth_records, character * repetitions)
    original = csv.field_size_limit(131072)
    try:
        with pytest.raises(csv.Error):
            load_records(path)
        assert csv.field_size_limit() == 131072
    finally:
        csv.field_size_limit(original)


def test_synth_malformed_csv_is_not_skipped(tmp_path):
    path = tmp_path / "SYNTH-malformed.csv"
    path.write_text('event_type,strategy_code,payload\nDETECTED,momentum_30s_warn,"unterminated', encoding="utf-8")
    with pytest.raises(csv.Error):
        load_records(path)


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
