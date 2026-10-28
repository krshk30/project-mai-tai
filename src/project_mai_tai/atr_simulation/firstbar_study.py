"""Causal in-sample study reconstruction from supplied bytes; no retrieval.

Reference fill-minute membership is an explicit counterfactual, not a native
watch/flip arrival claim. Original account order IDs survive leg decomposition.
No source script is executed; future-neighbor filtering is never applied.
"""

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from ..backtest import atr_oracle
from .execution_eligibility import classify_execution, execution_status
from .firstbar import CompletedFlipBar
from .firstbar_reference import ReferenceSeed
from .firstbar_replay import FirstBarReplayInput
from .types import (
    Boundary, Coverage, EpisodeInput, EventKey, EvidenceKind, ExecutionPolicy,
    FlipIdentity, LiveFacts, SourceRef, TradePrint,
)


NS = 1_000_000_000
MINUTE_NS = 60 * NS
ET = ZoneInfo("America/New_York")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ORACLE_SHA256 = "22ead27b69dc89cafc75e6dc1c97238a9cd8f82d48b4e4c2665bdb4889fa60d7"


@dataclass(frozen=True)
class StudyTapePacket:
    symbol: str
    day: str
    raw: bytes
    source: SourceRef
    coverage: Coverage


@dataclass(frozen=True)
class StudyBuyLeg:
    account: str
    order_id: str
    order_type: str
    symbol: str
    submitted_ns: int
    filled_ns: int
    price: Decimal
    quantity: int
    stop: str
    limit: str
    source: SourceRef


@dataclass(frozen=True)
class ReconstructedStudyCase:
    reference_key: tuple[str, str, str]
    legs: tuple[StudyBuyLeg, ...]
    inputs: tuple[FirstBarReplayInput, ...]
    issues: tuple[str, ...]
    assumptions: tuple[str, ...]


def _ns(value: str) -> int:
    raw = Decimal(value) * NS
    if not raw.is_finite() or raw != raw.to_integral_value():
        raise ValueError("exact_nanosecond_operand_required")
    return int(raw)


def _quantity(value: str) -> int:
    raw = Decimal(value)
    if not raw.is_finite() or raw < 0 or raw != raw.to_integral_value():
        raise ValueError("nonnegative_integral_quantity_required")
    return int(raw)


def _source_bytes(raw: bytes, source: SourceRef) -> None:
    if not source.artifact or hashlib.sha256(raw).hexdigest() != source.sha256:
        raise ValueError("study_input_source_sha256_mismatch")


def _local(ns: int) -> datetime:
    return (EPOCH + timedelta(seconds=ns // NS)).astimezone(ET)


def _boundary(day: str, hour: int, minute: int = 0) -> int:
    delta = datetime.combine(date.fromisoformat(day), time(hour, minute), ET).astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * NS


def parse_study_buy_legs(raw: bytes, source: SourceRef) -> tuple[StudyBuyLeg, ...]:
    _source_bytes(raw, source)
    result, ids = [], set()
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        fields = line.split("|")
        if len(fields) != 10:
            raise ValueError("study_buy_leg_ten_fields_required")
        account, order_id, order_type, symbol, submitted, filled, price, qty, stop, limit = fields
        if not all((account, order_id, symbol)) or (account, order_id) in ids:
            raise ValueError("unique_account_order_identity_required")
        ids.add((account, order_id))
        price = Decimal(price)
        quantity = _quantity(qty)
        if not price.is_finite() or price <= 0 or quantity <= 0:
            raise ValueError("positive_filled_order_price_quantity_required")
        result.append(StudyBuyLeg(account, order_id, order_type, symbol,
            _ns(submitted), _ns(filled), price, quantity, stop, limit,
            replace(source, pointer=f"line:{line_number}")))
    return tuple(result)


def parse_study_tape(packet: StudyTapePacket, policy: ExecutionPolicy) -> tuple[TradePrint, ...]:
    _source_bytes(packet.raw, packet.source)
    result, prior = [], None
    begin, end = _boundary(packet.day, 4), _boundary(packet.day, 20)
    for line_number, line in enumerate(packet.raw.decode("utf-8").splitlines(), start=1):
        fields = line.split("|")
        if len(fields) != 4:
            raise ValueError("study_tape_four_fields_required")
        event_ns, price, size = _ns(fields[0]), Decimal(fields[1]), _quantity(fields[2])
        if not price.is_finite() or price <= 0 or (prior is not None and event_ns < prior):
            raise ValueError("valid_ascending_study_tape_required")
        if _local(event_ns).date().isoformat() != packet.day:
            raise ValueError("study_tape_day_mismatch")
        if not begin <= event_ns < end:
            raise ValueError("study_tape_outside_0400_2000_session")
        prior = event_ns
        source = replace(packet.source, pointer=f"line:{line_number}")
        key = EventKey(event_ns, line_number,
                       f"REFERENCE_TAPE:{packet.source.sha256}:{line_number}", source)
        trade = TradePrint(key, packet.symbol, price, size)
        conditions = fields[3]
        if conditions.startswith("{") and conditions.endswith("}"):
            conditions = conditions[1:-1]
        result.append(replace(trade, execution=classify_execution(trade, conditions, policy)))
    return tuple(result)


def _bars(trades):
    bars, unknown = [], False
    for trade in trades:
        eligible, _ = execution_status(trade)
        if eligible is None:
            unknown = True
        if eligible is not True:
            continue
        start = trade.key.event_ns // MINUTE_NS * MINUTE_NS
        if not bars or bars[-1][0] != start:
            bars.append([start, trade.price, trade.price, trade.price, trade.price, trade.size])
        else:
            current = bars[-1]
            current[2] = max(current[2], trade.price)
            current[3] = min(current[3], trade.price)
            current[4] = trade.price
            current[5] += trade.size
    return bars, unknown


def reconstruct_study_cases(
    seed: ReferenceSeed, buy_bytes: bytes, buy_source: SourceRef,
    packets: tuple[StudyTapePacket, ...], *, execution_policy: ExecutionPolicy,
    account_brokers: Mapping[str, str], strategy_code: str,
    live_by_order: Mapping[tuple[str, str], LiveFacts],
) -> tuple[ReconstructedStudyCase, ...]:
    """Build explicit per-leg proxy episodes; unresolved operands return issues.

    Coverage is supplied evidence, never inferred from a file's length/first/last
    print. Bar feed is causal typed-eligible prints, unlike the study's hardcoded
    exclusion/neighbor filter. This difference must survive comparison reports.
    """
    if not strategy_code:
        raise ValueError("explicit_original_strategy_code_required")
    oracle_path = Path(atr_oracle.__file__)
    if hashlib.sha256(oracle_path.read_bytes()).hexdigest() != ORACLE_SHA256:
        raise ValueError("pinned_pure_atr_oracle_required")
    legs = sorted(parse_study_buy_legs(buy_bytes, buy_source), key=lambda leg: leg.filled_ns)
    groups = []
    for leg in legs:
        for group in groups:
            if (leg.symbol == group[0].symbol and abs(leg.filled_ns - group[0].filled_ns) < 180 * NS
                    and leg.account not in {prior.account for prior in group}):
                group.append(leg)
                break
        else:
            groups.append([leg])
    indexed = {}
    for group in groups:
        first = group[0]
        local = _local(first.filled_ns)
        key = (first.symbol, local.date().isoformat(), local.strftime("%H:%M:%S"))
        if key in indexed:
            raise ValueError("ambiguous_reference_180second_group_identity")
        indexed[key] = tuple(group)
    packet_index, prepared = {}, {}
    for packet in packets:
        key = packet.symbol, packet.day
        if key in packet_index:
            raise ValueError("duplicate_study_tape_packet")
        packet_index[key] = packet
    output = []
    for case in seed.cases:
        symbol, day, _ = case.reference_key
        group = indexed.get(case.reference_key, ())
        issues, inputs = [], []
        if not group or sum(leg.quantity for leg in group) != Decimal(dict(case.csv_fields)["qty"]):
            issues.append("UNKNOWN_EXACT_REFERENCE_ACCOUNT_LEGS")
        packet = packet_index.get((symbol, day))
        if packet is None:
            issues.append("UNKNOWN_RETAINED_STUDY_TAPE")
        if issues:
            output.append(ReconstructedStudyCase(case.reference_key, group, (), tuple(issues), ()))
            continue
        if (symbol, day) not in prepared:
            trades = parse_study_tape(packet, execution_policy)
            bars, uncertain_bars = _bars(trades)
            oracle_bars = [atr_oracle.Bar(start // 1_000_000, float(o), float(h), float(l), float(c), volume)
                           for start, o, h, l, c, volume in bars]
            rows = atr_oracle.compute_atr_trail(oracle_bars, seed="sma5", period=5, factor=3.5)
            prepared[symbol, day] = trades, bars, rows, uncertain_bars
        trades, bars, rows, uncertain_bars = prepared[symbol, day]
        proxy_start = group[0].filled_ns // MINUTE_NS * MINUTE_NS
        bar_index = next((index for index, bar in enumerate(bars) if bar[0] == proxy_start), None)
        if bar_index is None:
            issues.append("UNKNOWN_CAUSAL_PROXY_FLIP_BAR")
        if uncertain_bars:
            issues.append("UNKNOWN_CAUSAL_BAR_CONDITION_ELIGIBILITY")
        if any(leg.account not in account_brokers for leg in group):
            issues.append("UNKNOWN_ORIGINAL_ACCOUNT_BROKER_BINDING")
        if not issues:
            start, _, high, _, close, _ = bars[bar_index]
            row = rows[bar_index]
            source = replace(packet.source, kind=EvidenceKind.DERIVED,
                pointer=f"causal-typed-eligible-bar:{start};oracle:{ORACLE_SHA256};fill-minute-proxy")
            coverage = packet.coverage
            if coverage.start_ns > _boundary(day, 4) or coverage.end_ns < _boundary(day, 20):
                coverage = replace(coverage, complete=None,
                    gaps=(*coverage.gaps, "UNKNOWN_STUDY_0400_2000_CAPTURE_ENVELOPE"))
            next_sell = next((Boundary(bar[0] + MINUTE_NS, replace(source,
                pointer=f"oracle-SELL-bar:{bar[0]};available-at-close"), bar_start_ns=bar[0])
                for bar, result in zip(bars[bar_index + 1:], rows[bar_index + 1:])
                if result["flip"] == "SELL"), None)
            flip_bar = CompletedFlipBar(symbol, start, start + MINUTE_NS, high, close,
                Decimal(str(row["trail"])) if row["trail"] is not None else None,
                row["state"], coverage.complete, source)
            for leg in group:
                membership = replace(leg.source, kind=EvidenceKind.DERIVED,
                    pointer=leg.source.pointer + ";REFERENCE_SEED_MEMBERSHIP_NOT_NATIVE_WATCH")
                live = live_by_order.get((leg.account, leg.order_id), LiveFacts(None, (), (),
                    replace(coverage, complete=None, gaps=("UNKNOWN_ORIGINAL_INDIVIDUAL_LIVE_FACTS",))))
                identity = FlipIdentity(day, symbol, account_brokers[leg.account], leg.account,
                    strategy_code, "REFERENCE_FILL_MINUTE:" + leg.order_id,
                    "REFERENCE_SEGMENT_PROXY:" + leg.order_id)
                episode = EpisodeInput(identity, Boundary(start + MINUTE_NS, source, start),
                    True, membership, high + Decimal("0.01"), leg.quantity, leg.source,
                    next_sell, Boundary(_boundary(day, 20), source), trades, (), coverage,
                    coverage, (), replace(coverage, complete=None), live, execution_policy)
                inputs.append(FirstBarReplayInput(episode, flip_bar,
                    Boundary(_boundary(day, 15, 45), source)))
        output.append(ReconstructedStudyCase(case.reference_key, group, tuple(inputs), tuple(issues), (
            "REPLAY_IN_SAMPLE_REFERENCE_180S_GROUPING_ONLY; original account/order IDs retained",
            "FILL_MINUTE_PROXY_NOT_NATIVE_BUY_FLIP_OR_WATCH_ARRIVAL",
            "RECONSTRUCTED_SIP_AS_EXPORTED; same-clock sequence is file order, not native queue",
            "CAUSAL_TYPED_ELIGIBLE_BAR_FEED_NO_NEIGHBOR_FILTER; no flat bars invented",
            "MTAPE_EXPORT_MAY_PRESELECT_ROUNDLOTS_AND_QUANTIZE_SIP_TO_MILLISECONDS",
        )))
    return tuple(output)

