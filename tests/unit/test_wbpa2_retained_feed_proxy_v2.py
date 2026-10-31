"""ADDITIVE UNRUN SOURCE; FIRST package is unchanged.

Retained capture prices/clocks are actual C1-exported data. Mapping capture
received_at to OMS publication/receipt and ordering is a CONTROLLED proxy, not
the actual Redis/cache feed. Stops at first proxy resubmit; no venue-fill claim.
"""
import asyncio
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path

import pytest

from project_mai_tai.events import QuoteTickEvent, QuoteTickPayload
from tests.unit.wbpa2_controlled_transport import ControlledSubmitReply
from tests.unit.wbpa2_episode_port import close_case, drain_ticks, make_case, observe_original, queued, serial, state

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA2_RETAINED_QUOTES_SOURCE"),
    reason="Requires the exact reviewed private retained quotes and CHOW export")

CSV_SHA = "cad47bcbdddc8f68a096e87edcdbc1847e876225d7caf906156e6aad49073fcf"
THRESHOLD = Decimal("5.60") * Decimal(".92")
REPORTED_CROSS = datetime(2026, 10, 30, 16, 0, 56, tzinfo=UTC)
FEEDS = {
    "massive": ("capture_quote", 10210, 0, 3222,
        "2026-10-30T15:49:55.120799+00:00", "2026-10-30T15:49:56.960076+00:00", "5.17"),
    "schwab": ("quote_tick:LEVELONE_EQUITIES", 767, 395, 3236,
        "2026-10-30T15:49:55.496000+00:00", "2026-10-30T15:49:55.694560+00:00", "5.16"),
}


@dataclass(frozen=True)
class Captured:
    ordinal: int
    source: str
    provider: str
    event_at: datetime
    received_at: datetime
    bid: Decimal | None
    ask: Decimal | None
    bid_size: int | None
    ask_size: int | None

    @property
    def complete_quote(self):
        return self.bid is not None and self.ask is not None and self.bid > 0 and self.ask > 0


def parse_clock(raw):
    # Retain raw bytes; explicit lexical normalization supports exported PG+00
    # timestamps and variable fractional widths without inventing event clocks.
    text = raw + ":00" if raw.endswith("+00") else raw
    for pattern in ("%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            result = datetime.strptime(text, pattern)
            assert result.tzinfo is not None
            return result.astimezone(UTC)
        except ValueError:
            pass
    raise AssertionError("unsupported retained quote clock")


def optional_decimal(raw):
    if raw == "":
        return None
    try:
        result = Decimal(raw)
    except InvalidOperation:
        raise AssertionError("unsupported retained numeric quote") from None
    assert result.is_finite()
    return result


def load_capture():
    raw = Path(os.environ["WBPA2_RETAINED_QUOTES_SOURCE"]).read_bytes()
    assert len(raw) == 1190194 and hashlib.sha256(raw).hexdigest() == CSV_SHA
    rows = []
    for ordinal, fields in enumerate(csv.reader(raw.decode("utf-8").splitlines()), 1):
        assert len(fields) == 8
        source, provider, event_at, received_at, bid, ask, bid_size, ask_size = fields
        assert provider in FEEDS and source == FEEDS[provider][0]
        row = Captured(ordinal, source, provider, parse_clock(event_at), parse_clock(received_at),
            optional_decimal(bid), optional_decimal(ask), int(bid_size) if bid_size else None,
            int(ask_size) if ask_size else None)
        assert row.received_at >= row.event_at
        rows.append(row)
    assert len(rows) == 10977
    # Original export order is event time. OMS cache receipt order is not given.
    assert all(a.event_at <= b.event_at for a, b in zip(rows, rows[1:]))
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["massive", "schwab"])
async def test_retained_feed_final560_first_present_eligible_quote_CONTROLLED_OMS_receipt_proxy(
    tmp_path, monkeypatch, record_property, provider,
):
    rows = load_capture()
    source, count, incomplete_count, expected_ordinal, event_at, received_at, ask = FEEDS[provider]
    feed = [row for row in rows if row.provider == provider]
    assert len(feed) == count
    incomplete = [row.ordinal for row in feed if not row.complete_quote]
    assert len(incomplete) == incomplete_count
    # Actual first PRESENT ask >=threshold, not a completeness claim about
    # missing asks, in-memory quote merging, Redis or actual OMS eligibility.
    present_eligible = [row for row in feed if row.ask is not None and row.ask >= THRESHOLD]
    known_first = min(present_eligible, key=lambda row: (row.received_at, row.ordinal))
    assert known_first.ordinal == expected_ordinal and known_first.complete_quote
    assert known_first.event_at == datetime.fromisoformat(event_at)
    assert known_first.received_at == datetime.fromisoformat(received_at)
    assert known_first.ask == Decimal(ask) and THRESHOLD == Decimal("5.152")

    case = await make_case(tmp_path, monkeypatch, final_slot=True, replies=[ControlledSubmitReply(
        "rejected", "CONTROLLED first retained-feed proxy retry PRICE_AGGRESSIVE417",
        error_code="ORDER_RISK_RULE_PRICE_AGGRESSIVE")])
    initial_cutoff = case.clock[0]
    delivered = unsupported = pre_cutoff = 0
    first_proxy = None
    try:
        assert [len(case.seeded_ids[key]) for key in ("intents", "orders", "order_events")] == [7, 4, 4]
        assert initial_cutoff > datetime.fromisoformat("2026-10-30T15:45:05.878586+00:00")
        assert case.event.payload.metadata["stop_price"] == "5.6000"
        observe_original(case)
        assert state(case) is not None
        # Separate feed alternatives. Stable capture receipt+fileordinal is a
        # CONTROLLED event delivery model, never asserted as actual OMS order.
        for row in sorted(feed, key=lambda row: (row.received_at, row.ordinal)):
            if row.received_at < initial_cutoff:
                pre_cutoff += 1
                continue
            if not row.complete_quote:
                unsupported += 1
                continue  # retain/count unsupported rows; no carry/fill/zero quote invented
            case.clock[0] = row.received_at
            event = QuoteTickEvent(source_service="CONTROLLED_CAPTURE_TO_OMS_PROXY",
                produced_at=row.received_at,
                payload=QuoteTickPayload(symbol="CHOW", bid_price=row.bid, ask_price=row.ask,
                                        bid_size=row.bid_size, ask_size=row.ask_size))
            case.provider.market_tasks.add(asyncio.current_task())
            await case.service._handle_quote_tick_event(event)
            await drain_ticks(case)
            delivered += 1
            if queued(case):
                first_proxy = row
                assert row.ordinal == expected_ordinal
                assert case.provider.entries == []
                retry, = queued(case)
                assert retry.produced_at == row.received_at
                await serial(case, retry)
                assert len(case.provider.requests) == 1
                assert [entry["operation"] for entry in case.provider.entries] == ["submit", "positions"]
                assert case.provider.entries[1]["result"] == "REFUSED_UNKNOWN_NEVER_FLAT"
                assert case.provider.requests[0].quantity == Decimal(53)
                assert case.provider.entries[0]["operation"] == "submit"
                assert not case.provider.entries[0]["market_task"]
                break  # later counterfactual venue state is unknown; do not invent fills/outcomes
            assert row.ask < THRESHOLD
            assert case.provider.entries == [] and state(case) is not None
        assert first_proxy is known_first
        assert first_proxy.received_at < REPORTED_CROSS
        delta = REPORTED_CROSS - first_proxy.received_at
        lead = Decimal(delta.days * 86400 + delta.seconds) + Decimal(delta.microseconds) / Decimal(1000000)
        receipt = dict(scope="RETAINED_VALUES_WITH_CONTROLLED_CAPTURE_RECEIPT_TO_OMS_PUBLISH_PROXY",
            provider=provider, source=source, csv_sha256=CSV_SHA,
            capture_rows=count, capture_incomplete_bid_ask_rows=incomplete_count,
            delivered_complete_prefix_rows=delivered, unsupported_prefix_rows=unsupported,
            before_completed_final_cohort_rows=pre_cutoff, threshold=str(THRESHOLD),
            actual_first_present_eligible_capture_ordinal=first_proxy.ordinal,
            actual_capture_event_at=first_proxy.event_at.isoformat(),
            actual_capture_received_at=first_proxy.received_at.isoformat(),
            actual_capture_ask=str(first_proxy.ask),
            CONTROLLED_OMS_produced_and_receipt_at=first_proxy.received_at.isoformat(),
            CONTROLLED_retry_event_at=retry.produced_at.isoformat(),
            C1_reported_cross_at=REPORTED_CROSS.isoformat(), CONTROLLED_lead_seconds=str(lead),
            actual_OMS_source_cache_order="UNKNOWN_NO_PUBLISH_OR_REDIS_BINDING",
            actual_counterfactual_retry_at="UNKNOWN", native_execution_claim=False,
            partial_quote_policy="COUNT_AND_RETAIN_UNSUPPORTED_NO_SYNTHETIC_MERGE",
            replay_stop="FIRST_PROXY_RETRY; remaining captured data retained but no invented venue continuation")
        print("WBPA2_RETAINED_FEED_PROXY_EVIDENCE=" + json.dumps(receipt, sort_keys=True))
        for key, value in receipt.items():
            record_property(key, value)
    finally:
        await close_case(case)
