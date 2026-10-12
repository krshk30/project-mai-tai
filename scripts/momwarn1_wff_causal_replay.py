"""Offline causal MODEL replay. No provider/client factory or recorded ACK claim."""

from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal
import hashlib
import heapq
import json
from pathlib import Path

from project_mai_tai.momentum_paper.conditions import condition_snapshot_from_payload
from project_mai_tai.momentum_paper.models import TradePrint
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from project_mai_tai.momentum_paper.warning_window import AggregateObservation


def run(trades: dict, session: dict, *, aggregate_delay_ns=0, owner_delay_ns=0,
        trade_delay_ns=0, start_ms=None) -> dict:
    if any(type(value) is not int or value < 0 for value in (
        aggregate_delay_ns, owner_delay_ns, trade_delay_ns,
    )):
        raise ValueError("explicit nonnegative CONTROLLED delays required")
    conditions = condition_snapshot_from_payload(session["condition_snapshot"])
    rows = sorted(trades["rows"], key=lambda row: row["sip_timestamp"])
    if start_ms is not None:
        rows = [row for row in rows if row["sip_timestamp"] // 1_000_000 >= start_ms]
    if not rows:
        raise ValueError("empty retained replay input")
    model = MomentumWarningConsumer(
        prior_closes={"WFF": Decimal(session["prior_closes"]["WFF"])},
        condition_version=conditions.version, coverage_started_ms=rows[0]["sip_timestamp"]//1_000_000,
    )
    buckets, events = defaultdict(list), []
    for index, row in enumerate(rows):
        eligible, reason = conditions.classify(row["conditions"])
        stamp = row["sip_timestamp"]
        trade = TradePrint("WFF", stamp//1_000_000, Decimal(row["price"]), row["size"],
                           trade_id=str(row.get("id", "")), exchange=row.get("exchange"),
                           conditions=tuple(row["conditions"]), eligible=eligible,
                           exclusion_reason=reason)
        events.append((stamp+trade_delay_ns, 2, index, (trade, stamp)))
        if eligible:
            buckets[stamp//1_000_000_000].append(trade)
    for sequence, (second, prints) in enumerate(sorted(buckets.items()), 1):
        end = (second+1)*1_000_000_000
        prices = [trade.price for trade in prints]
        bar = AggregateObservation(
            "WFF", second*1000, (second+1)*1000, prices[0], max(prices), min(prices), prices[-1],
            sum(trade.size for trade in prints), end+aggregate_delay_ns, end+aggregate_delay_ns,
            "CONTROLLED-WFF-MODEL", 1, sequence, 0,
        )
        events.append((end+aggregate_delay_ns, 0, sequence, bar))
    heapq.heapify(events)
    owner_applied, request_pending = False, False
    records, owner_events, target_reference = [], [], []
    while events:
        observed_ns, kind, sequence, payload = heapq.heappop(events)
        if kind == 0:
            bar = payload
            assert observed_ns >= bar.end_ms*1_000_000
            outcome, emitted = model.observe_aggregate(bar, observed_ns=observed_ns, legacy_active=set())
            model.complete_read(f"{bar.end_ms}-0", observed_ns=observed_ns)
            if outcome.status == "ADMITTED" and not owner_applied and not request_pending:
                request_pending = True
                applied = observed_ns+owner_delay_ns
                owner_events.append(dict(kind="CONTROLLED_OWNER_REQUEST", observed_ns=observed_ns))
                heapq.heappush(events, (applied, 1, sequence, None))
        elif kind == 1:
            owner_applied = True
            owner_events.append(dict(kind="CONTROLLED_OWNER_APPLICATION_NOT_PROVIDER_ACK",
                                     observed_ns=observed_ns))
            continue
        elif owner_applied:
            trade, sip = payload
            if sip == 1791809480927834426:
                for seconds in (30, 60):
                    whole = model.windows["WFF"].whole_buckets(
                        observed_ms=trade.sip_ts_ms, seconds=seconds,
                    )
                    ref = min(whole, key=lambda bar: (bar.low, bar.start_ms)) if whole else None
                    target_reference.append(dict(
                        seconds=seconds, window_start_ms=trade.sip_ts_ms-seconds*1000,
                        window_end_ms=trade.sip_ts_ms,
                        input_covers_time_bounds=(rows[0]["sip_timestamp"] <=
                                                 (trade.sip_ts_ms-seconds*1000)*1_000_000),
                        qualifying_whole_buckets=len(whole),
                        reference=(dict(low=str(ref.low), start_ms=ref.start_ms, end_ms=ref.end_ms)
                                   if ref else None),
                        coverage_claim="retained_time_bounds_not_provider_completeness",
                    ))
            emitted = model.observe_trade(trade, sip_timestamp_ns=sip, observed_ns=observed_ns)
        else:
            continue
        emitted = model.engine.seal_warning_decisions(emitted, observed_decision_ns=observed_ns)
        for record in emitted:
            if record.event_type in {"DETECTED", "FILLED", "EXITED", "FINALIZED", "NO_FILL"}:
                records.append(dict(logical_id=record.logical_id, strategy_code=record.strategy_code,
                                    kind=record.event_type, observed_ns=observed_ns, payload=record.payload,
                                    callback_sip_ns=sip if kind == 2 else None,
                                    callback_trade_id=trade.trade_id if kind == 2 else None,
                                    callback_price=str(trade.price) if kind == 2 else None,
                                    cause=("retained_print_CONTROLLED_receive" if kind == 2
                                           else "CONTROLLED_aggregate_END_receive")))
    expected_signal_ms, expected_fill_ms = 1791809480927, 1791809480928
    reached = []
    for record in records:
        detected = record["payload"].get("detect", {})
        if (record["kind"] != "DETECTED" or detected.get("sip_ts_ms") != expected_signal_ms
                or detected.get("price") != "13.35"):
            continue
        fill = next((item for item in records if item["logical_id"] == record["logical_id"]
                     and item["kind"] == "FILLED"), None)
        if (fill and fill["payload"]["fill"]["sip_ts_ms"] == expected_fill_ms
                and Decimal(fill["payload"]["fill"]["price"]) == Decimal("13.40")):
            reached.append(dict(strategy_code=record["strategy_code"], signal_ms=expected_signal_ms,
                                signal_price=detected["price"], fill_ms=expected_fill_ms,
                                fill_price=fill["payload"]["fill"]["price"],
                                logical_id=record["logical_id"]))
    return dict(evidence_kind="CONTROLLED_CAUSAL_MODEL_WITH_RETAINED_SIP_PRICES",
                transport_latency="UNMEASURED", provider_ACK="NOT_RECORDED_OR_INVENTED",
                bucket_source="model_eligible_SIP_rows_NOT_recorded_Massive_WS_A",
                availability="aggregate END plus declared delay; no future bucket low",
                assumptions=dict(aggregate_delay_ns=aggregate_delay_ns, owner_delay_ns=owner_delay_ns,
                                 trade_delay_ns=trade_delay_ns, start_ms=start_ms,
                                 universe="WFF only; no other symbols consume cap"),
                input_rows=len(rows), input_first_sip_ns=rows[0]["sip_timestamp"],
                input_last_sip_ns=rows[-1]["sip_timestamp"],
                input_filter="all rows in retained input; optional start_ms only; WFF source file",
                retained_source=trades.get("source"), retained_fetched_utc=trades.get("fetched_utc"),
                eligible_buckets=len(buckets), owner_events=owner_events,
                target_reference_windows=target_reference,
                target_reached=reached, records=records)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trades", type=Path)
    parser.add_argument("sessions", type=Path)
    parser.add_argument("--aggregate-delay-ns", type=int, default=0)
    parser.add_argument("--owner-delay-ns", type=int, default=0)
    parser.add_argument("--trade-delay-ns", type=int, default=0)
    parser.add_argument("--start-ms", type=int)
    parser.add_argument("--tested-head", help="Explicit local code SHA, not an install pin")
    args = parser.parse_args()
    trade_bytes, session_bytes = args.trades.read_bytes(), args.sessions.read_bytes()
    sessions = json.loads(session_bytes)["rows"]
    ready = next(row["payload"] for row in sessions if row["event_type"] == "SESSION_READY"
                 and row["session_date"] == "2026-10-12")
    result = run(json.loads(trade_bytes), ready, aggregate_delay_ns=args.aggregate_delay_ns,
                 owner_delay_ns=args.owner_delay_ns, trade_delay_ns=args.trade_delay_ns,
                 start_ms=args.start_ms)
    result["input_sha256"] = dict(trades=hashlib.sha256(trade_bytes).hexdigest(),
                                  sessions=hashlib.sha256(session_bytes).hexdigest())
    result["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["tested_head"] = args.tested_head or "UNKNOWN"
    print(json.dumps(result, separators=(",", ":"), allow_nan=False))


if __name__ == "__main__":
    main()
