import asyncio
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
import json
from datetime import UTC, datetime

import pytest

from project_mai_tai.momentum_live.handoff import LiveHandoff
from project_mai_tai.momentum_paper.models import MomentumTapeRecord
from project_mai_tai.settings import Settings
from project_mai_tai.momentum_live_sec import CountryRoute


def row(**fields):
    # CONTROLLED prior close, not historical SCNI evidence. Payload overrides stay exact.
    return replace(MomentumTapeRecord("event", "logical30", "momentum_30s_warn", "DETECTED",
        date(2026, 10, 14), "SCNI", None, {"prior_close": "2.50", "observed_decision_ns": 100,
            "reference": {"end_ms": 1}, "detect": {"end_ms": 3, "price": "3"}}), **fields)


def handoff():
    target = LiveHandoff(SimpleNamespace(xadd=AsyncMock()), Settings(_env_file=None, momentum_live_enabled=True),
        route_for=lambda symbol, now: SimpleNamespace(broker="webull", reason="sec_unknown_at_decision"))
    target.ledger.ready = True
    return target


def test_offer_pure_bounded_queue_and_cross_method_dedup():
    target = handoff()
    assert target.offer(row(), day_volume=50000, now_ns=100) == "offered"
    assert target.offer(row(logical_id="logical60", strategy_code="momentum_60s_warn"),
                        day_volume=50000, now_ns=100) == "duplicate_move"
    assert target.queue.maxsize == 64 and target.queue.qsize() == 2
    assert target.queue._queue[0][0] == target.queue._queue[1][0]
    assert target.redis.xadd.await_count == 0


@pytest.mark.parametrize("event_type,strategy", [("FILLED", "momentum_30s_warn"), ("DETECTED", "momentum_30s")])
def test_only_warning_sealed_detection(event_type, strategy):
    target = handoff()
    assert target.offer(row(event_type=event_type, strategy_code=strategy), day_volume=50000, now_ns=100) == "not_live_signal"
    assert target.queue.empty()


def test_manual_off_and_missing_volume_do_not_open():
    target = handoff()
    assert target.offer(row(), day_volume=None, now_ns=100) == "day_volume_unknown"
    target.settings.momentum_live_enabled = False
    assert target.offer(row(), day_volume=50000, now_ns=100) == "disabled"
    assert target.queue.empty()


@pytest.mark.asyncio
async def test_worker_emits_deterministic_identity_independently_of_offer():
    target = handoff()
    release = asyncio.Event()
    async def write(*args, **kwargs):
        await release.wait()
        return "1-0"
    target.redis.xadd.side_effect = write
    target.offer(row(), day_volume=50000, now_ns=100)
    episode = next(iter(target.ledger.episodes))
    task = asyncio.create_task(target._emit())
    await asyncio.sleep(0)
    assert target.offer(row(logical_id="logical60"), day_volume=50000, now_ns=100) == "duplicate_move"
    release.set()
    await target.queue.join()
    payload = json.loads(target.redis.xadd.call_args.args[1]["data"])
    assert payload["event_id"] == episode and payload["payload"]["strategy_code"] == "momentum_warn_live"
    assert json.loads(payload["payload"]["metadata"]["momlive_signal"])["day_volume"] == 50000
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unknown_handoff_ack_no_retry_and_slot_retained():
    target = handoff()
    target.redis.xadd.side_effect = TimeoutError
    target.offer(row(), day_volume=50000, now_ns=100)
    task = asyncio.create_task(target._emit())
    await target.queue.join()
    assert target.redis.xadd.await_count == 1 and target.counts["handoff_ack_unknown"] == 1
    assert len(target.ledger.episodes) == 1
    assert target.offer(row(), day_volume=50000, now_ns=100) == "duplicate_move"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_no_owner_proof_startup_refuses():
    target = handoff()
    target.ledger.ready = False
    assert target.offer(row(), day_volume=50000, now_ns=100) == "ownership_restore_pending"


def test_actual_country_route_reason_and_evidence_sealed_before_any_worker_await():
    target = handoff()
    stamp = datetime(2026, 10, 14, 14, tzinfo=UTC)
    observed = int(stamp.timestamp()*1e9)
    target.route_for = lambda symbol, now: CountryRoute("schwab", "sec_us", "READY", True,
                                                        "0000012345", stamp)
    record = row(payload={"prior_close": "2.50", "observed_decision_ns": observed, "reference": {"end_ms": 1},
                          "detect": {"end_ms": 3, "price": "3"}})
    assert target.offer(record, day_volume=50000, now_ns=observed) == "offered"
    _, signal = target.queue._queue[0]
    assert signal.country_route == "schwab" and signal.country_category == "sec_us"
    assert json.loads(signal.country_evidence_json)["is_us"] is True
    target.route_for = lambda symbol, now: CountryRoute("webull", "sec_unknown", "UNKNOWN", None, None, None)
    assert signal.country_route == "schwab"
    assert target.redis.xadd.await_count == 0


@pytest.mark.parametrize("prior,expected", [
    (None, "momneg1_prior_close_unknown"),
    ("3.01", "momneg1_down_day"),
    ("3", "offered"),
    ("2.50", "offered"),
], ids=["missing", "down", "equal", "up"])
def test_controlled_fixture_prior_close_keeps_real_live_guard(prior, expected, caplog):
    target = handoff()
    record = row()
    if prior is None:
        del record.payload["prior_close"]
    else:
        record.payload["prior_close"] = prior
    before = json.dumps(record.payload, sort_keys=True)
    routing = []
    original_route = target.route_for

    def route(symbol, now):
        routing.append(symbol)
        return original_route(symbol, now)

    target.route_for = route
    with caplog.at_level("INFO", logger="momentum-live"):
        assert target.offer(record, day_volume=50000, now_ns=100) == expected
    assert json.dumps(record.payload, sort_keys=True) == before
    assert target.counts[expected] == 1
    if expected == "offered":
        assert routing == [record.symbol]
        assert target.queue.qsize() == 1 and len(target.ledger.episodes) == 1
        assert "[MOMNEG1-REFUSED]" not in caplog.text
    else:
        assert routing == [] and target.queue.empty() and not target.ledger.episodes
        assert "[MOMNEG1-REFUSED]" in caplog.text
        assert f"reason={expected}" in caplog.text
    target.redis.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_warning_prefetch_unique_bounded_and_new_prepare_day_reloads_before_reset():
    target = handoff()
    target.country_lookup = AsyncMock()
    target.reload_country = AsyncMock()
    assert target.offer_warning("SCNI") and not target.offer_warning("SCNI")
    assert target.country_lookup.await_count == 0
    worker = asyncio.create_task(target._lookup())
    await target.lookup_queue.join()
    target.country_lookup.assert_awaited_once_with("SCNI")
    target.country_prepared_day = date(2026, 10, 14)
    await target.prepare_country_day(date(2026, 10, 15))
    target.reload_country.assert_awaited_once_with(date(2026, 10, 15))
    assert target.lookup_seen == set() and target.offer_warning("SCNI")
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)
