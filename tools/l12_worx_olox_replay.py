"""Read-only case inventory and offline L12 replay boundaries.

No provider/client/connection construction. Historical order/fill rows are not
native-flat proof. Main supplies a separately claimed private factory for the
market/OMS replay; missing operands return UNKNOWN, never a synthetic BUY.
"""

import argparse
import csv
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo


EXPORT_SHA256 = "31147bd20bb1f0c3e9a997bc45f9dbf41ef5fb71a6a58bec4b9c0a9e30c6c5f6"
CASES = {
    "WORX-20261020-1027": (103, "WORX", "2026-10-20", "10:27"),
    "OLOX-20261021-1329": (110, "OLOX", "2026-10-21", "13:29"),
}

ARM = re.compile(r"\[V2-CW-ARM\] (?P<symbol>[A-Z]+) armed bar_ts=(?P<bar>[0-9]+) trig=(?P<trigger>[0-9.]+) flip_level=(?P<line>[0-9.]+)")


def load_e_packet(root, case_id, manifest):
    """Pinned immutable source rows; no order-status-asof/native-book inference."""
    if manifest.get("schema") != "L12_E_PACKET_V1" or case_id not in CASES:
        raise ValueError("e_packet_identity_unknown")
    sources = {}
    texts = {}
    for section in ("v2_log.log", "orders.csv", "fills.csv"):
        ref = manifest["cases"][case_id][section]
        if Path(ref["name"]).name != ref["name"]:
            raise ValueError("e_packet_filename_required")
        raw = (Path(root) / ref["name"]).read_bytes()
        if sha256(raw).hexdigest() != ref["sha256"]:
            raise ValueError(f"e_packet_sha256_mismatch:{section}")
        texts[section] = raw.decode("utf-8")
        sources[section] = dict(ref)
    _, symbol, day, flip = CASES[case_id]
    bar_ms = int(datetime.fromisoformat(f"{day}T{flip}:00")
        .replace(tzinfo=ZoneInfo("America/New_York")).timestamp() * 1000)
    arms = []
    lines = texts["v2_log.log"].splitlines()
    for index, line in enumerate(lines, 1):
        match = ARM.search(line)
        if match is not None and match["symbol"] == symbol and int(match["bar"]) == bar_ms:
            arms.append({
                "bar_ms": int(match["bar"]), "trigger": match["trigger"],
                "flip_level": match["line"], "line_number": index,
                "received_utc_text": line[:23], "verbatim": line,
            })
    tables = {}
    for section, required in (
        ("orders.csv", {"id", "client_order_id", "intent_id", "reason", "side", "order_type", "status", "quantity", "submitted_at", "updated_at", "account"}),
        ("fills.csv", {"id", "order_id", "side", "quantity", "price", "filled_at", "account"}),
    ):
        reader = csv.DictReader(io.StringIO(texts[section]))
        if set(reader.fieldnames or ()) != required or len(reader.fieldnames or ()) != len(required):
            raise ValueError(f"e_packet_csv_schema_mismatch:{section}")
        rows = list(reader)
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError(f"e_packet_duplicate_id:{section}")
        for row in rows:
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"e_packet_csv_truncated:{section}")
            if not row["account"].startswith("live:"):
                raise ValueError("e_packet_account_identity_unknown")
            quantity = Decimal(row["quantity"])
            if not quantity.is_finite() or quantity <= 0:
                raise ValueError("e_packet_quantity_unknown")
        tables[section] = rows
    orders = {row["id"]: row for row in tables["orders.csv"]}
    links = []
    issues = [] if len(arms) == 1 else ["UNKNOWN_MISSING_OR_AMBIGUOUS_ORIGINAL_ARM"]
    for fill in tables["fills.csv"]:
        order = orders.get(fill["order_id"])
        if order is None:
            issues.append(f"UNKNOWN_FILL_ORDER_LINK:{fill['id']}")
        elif order["account"] != fill["account"] or order["side"] != fill["side"]:
            raise ValueError("e_packet_fill_order_identity_mismatch")
        links.append({"fill_raw": fill, "order_raw": order})
    return {
        "case_id": case_id, "sources": sources, "arms": arms,
        "orders_raw": tables["orders.csv"], "fills_raw": tables["fills.csv"],
        "fill_order_intent_links": links,
        "cross_account_lines_verbatim": [line for line in lines if "CROSS-ACCOUNT" in line],
        "recovery_lines_verbatim": [line for line in lines if "[V2-FLIP-OWNER-RECOVERY]" in line],
        "native_book": "UNKNOWN_NEVER_STORED",
        "asof_working_or_position": "UNKNOWN_NOT_DERIVED_FROM_FINAL_STATUSES",
        "issues": issues,
    }


def select_available_bar_rows(numbered_rows, *, target_bar_time):
    """Causal created-at proxy cutoff, not a native callback ordering claim.

    Earlier-minute backfills can become available AFTER the target. Retain their
    source ordinals/reasons but never feed them into the earlier decision state.
    Equal created clocks serialize by source ordinal, explicitly not native order.
    """
    target_time = datetime.fromisoformat(target_bar_time)
    if target_time.tzinfo is None:
        raise ValueError("target_bar_timezone_required")
    parsed = []
    for line_number, row in numbered_rows:
        bar = datetime.fromisoformat(row["bar_time"])
        created = datetime.fromisoformat(row["created_at"])
        if bar.tzinfo is None or created.tzinfo is None:
            raise ValueError("olox_partial_timezone_required")
        parsed.append((line_number, row, bar, created))
    targets = [item for item in parsed if item[2] == target_time]
    if len(targets) != 1:
        raise ValueError("original_1329_unique_target_bar_required")
    target_line, _, _, target_created = targets[0]
    cutoff = (target_created, target_line)
    included, excluded = [], []
    for line_number, row, bar, created in parsed:
        reason = None
        if bar > target_time:
            reason = "BAR_AFTER_TARGET"
        elif (created, line_number) > cutoff:
            reason = "HISTORY_AVAILABLE_AFTER_TARGET_PROXY"
        if reason is not None:
            excluded.append({"line_number": line_number, "bar_time": row["bar_time"],
                "created_at": row["created_at"], "reason": reason})
            continue
        if created < bar + timedelta(seconds=60):
            raise ValueError("olox_partial_forming_bar")
        included.append((line_number, row, created))
    included.sort(key=lambda item: (item[2], item[0]))
    if not included or included[-1][0] != target_line:
        raise ValueError("original_1329_terminal_bar_required")
    return included, excluded, {"line_number": target_line,
        "bar_time": target_bar_time, "created_at": target_created.isoformat(),
        "clock_basis": "CREATED_AT_PROXY_WITH_SOURCE_ORDINAL_NOT_NATIVE_ARRIVAL"}


def load_olox_partial_market(path):
    """Actual retained OHLC only. created_at is a labeled delivery proxy.

    No full04:00 prefix, native quote, boot proof or empty minutes are minted.
    Original 13:29 bar is the terminal event; 13:25 is never substituted.
    """
    raw = Path(path).read_bytes()
    digest = "b0febb8bb4482ac3b95dc0822ffe0b289a6153d39488714e4e9f00f45f99e6d4"
    if sha256(raw).hexdigest() != digest:
        raise ValueError("olox_partial_bar_sha256_mismatch")
    numbered_rows = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        row = json.loads(line, object_pairs_hook=unique_object)
        if row["symbol"] != "OLOX" or row["interval_secs"] != 60:
            raise ValueError("olox_partial_bar_identity_unknown")
        numbered_rows.append((line_number, row))
    included, excluded, cutoff = select_available_bar_rows(numbered_rows,
        target_bar_time="2026-10-21T17:29:00+00:00")
    rows = []
    for sequence, (line_number, row, created) in enumerate(included, 1):
        rows.append({
            "kind": "bar", "row": row,
            "phase": "live" if row["bar_time"] == "2026-10-21T17:29:00+00:00" else "replay",
            "received_ms": int(created.timestamp() * 1000), "sequence": sequence,
            "available_at": created.isoformat(), "source_ordinal": line_number,
            "source_pointer": f"{digest}:line:{line_number}",
            "clock_basis": "CONTROLLED_DELIVERY_AT_RETAINED_CREATED_AT_NOT_NATIVE_CALLBACK_TIME",
        })
    missing = ["04:00..07:20_ET_PREFIX", "ORIGINAL_QUOTES", "ORIGINAL_BOOT_AND_OWNER_PREIMAGES"]
    if any(row["reason"] == "HISTORY_AVAILABLE_AFTER_TARGET_PROXY" for row in excluded):
        missing.append("EARLIER_BARS_AVAILABLE_ONLY_AFTER_TARGET")
    return {
        "case_id": "OLOX-20261021-1329", "source_ref": {"path": str(path), "sha256": digest},
        "settings_source": "EXPLICIT_CONTROLLED_TEST_SETTINGS_NOT_HISTORICAL_CONFIG",
        "purpose": "PARTIAL_CALLBACK_DIAGNOSTIC",
        "events": rows,
        "availability_cutoff": cutoff,
        "excluded_source_rows": excluded,
        "source_row_count": len(numbered_rows),
        "coverage": {"complete": False, "missing": missing},
    }


def controlled_callback_book(case, mode, observed_ms):
    """Typed external responses only; actual application permission stays real."""
    from project_mai_tai.brokerdecides1_sync import account_names

    modes = {"flat", "working", "position", "stale", "LOSTACK", "CANCELACK"}
    if mode not in modes:
        raise ValueError("controlled_native_mode_unknown")
    accounts = account_names(case.settings)
    if accounts != ("live:schwab_1m_v2", "live:orb"):
        raise ValueError("explicit_controlled_account_bindings_required")
    started = observed_ms - (20_001 if mode == "stale" else 100)
    return {
        "evidence_kind": "CONTROLLED", "source_ref": f"CONTROLLED_EXTERNAL_{mode}_NOT_HISTORICAL_NATIVE",
        "epoch": "L12_ORIGINAL_CALLBACK_CONTROLLED", "started_ms": started,
        "completed_ms": started + 10,
        "books": [
            {"account": accounts[0], "provider": "schwab", "wire_started_ms": started,
             "wire_completed_ms": started + 5, "working": [], "positions": [],
             "complete": True, "reason": "CONTROLLED_EXTERNAL_RESPONSE"},
            {"account": accounts[1], "provider": "webull", "wire_started_ms": started,
             "wire_completed_ms": started + 5,
             "working": [case.symbol] if mode == "working" else [],
             "positions": [case.symbol] if mode == "position" else [],
             "complete": True, "reason": "CONTROLLED_EXTERNAL_RESPONSE"},
        ],
        "inflight": [case.symbol] if mode == "CANCELACK" else [],
        "filled_segments": [], "local_complete": True,
        "reason": f"CONTROLLED_{mode}_NOT_HISTORICAL_NATIVE",
    }


def make_causal_market_case(packet):
    """Initialize at first available event, never at a future census BUY time.

    Watch membership/config are explicitly CONTROLLED from this first event.
    No historical watch/SHORT/cap/owner preimage is manufactured. Actual shadow
    ATR math may later derive a SHORT from the supplied causal partial bars.
    """
    from tests.line_restore_acceptance_factory import make_line_restore_case

    case_id = packet.get("case_id")
    if case_id not in CASES or not packet.get("events"):
        raise ValueError("causal_initial_market_event_required")
    first = packet["events"][0]
    observed_ms = first["received_ms"]
    if type(observed_ms) is not int or observed_ms <= 0 or not first.get("source_pointer"):
        raise ValueError("causal_initial_asof_required")
    case = make_line_restore_case(symbol=CASES[case_id][1], now_ms=observed_ms, settings_overrides={
        "strategy_schwab_1m_v2_enabled": True,
        "strategy_schwab_1m_v2_brokerdecides1_enabled": True,
        "strategy_schwab_1m_v2_brokerdecides1_max_age_ms": 20_000,
        "strategy_schwab_1m_v2_account_name": "live:schwab_1m_v2",
        "strategy_schwab_1m_v2_webull_account_name": "live:orb",
        "strategy_schwab_1m_v2_broker_provider": "simulated", "oms_adapter": "simulated",
    })
    state = case.strategy.watchlist_state(case.symbol)
    if state.atr_short_flip_bar_ts or state.bars:
        raise ValueError("unexpected_initial_indicator_preimage")
    case._l12_causal_initialization = {
        "asof_ms": observed_ms, "source_pointer": first["source_pointer"],
        "watch_basis": "CONTROLLED_MEMBERSHIP_FROM_FIRST_AVAILABLE_EVENT_NOT_HISTORICAL_WATCH",
        "settings_basis": packet["settings_source"],
        "initial_short_ms": state.atr_short_flip_bar_ts,
        "initial_bar_count": len(state.bars),
        "initial_cap_notes": {"resting_taken": state.cw_resting_taken,
            "reclaim_taken": state.cw_reclaim_taken,
            "watch_start_ms": state.cw_seed_cap_watch_start_ms},
    }
    return case


async def replay_original_callbacks(case, packet, *, native_mode):
    """Same callable for positive/negative branches, no outcome override.

    Complete reviewed packets may produce actual strategy drafts/real guarded
    publications; partial packets always retain UNKNOWN historical qualification,
    even with a controlled flat native branch. No stores/attestations are invented.
    """
    if packet.get("case_id") not in CASES:
        raise ValueError("original_market_case_identity_unknown")
    if not packet.get("events"):
        return {"status": "UNKNOWN", "missing": ["ORIGINAL_MARKET_CALLBACK_EVENTS"], "steps": []}
    if case is None or CASES[packet["case_id"]][1] != case.symbol:
        raise ValueError("original_market_case_identity_unknown")
    initialization = getattr(case, "_l12_causal_initialization", None)
    first_ms = packet["events"][0]["received_ms"]
    if initialization is None or initialization["asof_ms"] != first_ms or case.now_ms != first_ms:
        raise ValueError("causal_case_initialization_required_no_clock_rewind")
    transport = install_recording_emitters(case)
    prepared = deepcopy(packet)
    for item in prepared["events"]:
        item["native_packet"] = controlled_callback_book(case, native_mode, item["received_ms"])
    if native_mode == "LOSTACK":
        first = prepared["events"][0]
        with case.clock(first["received_ms"]):
            await consume_book(case, first["native_packet"])
            case.strategy._brokerdecides1_memory.reserve(case.symbol, "CONTROLLED_LOSTACK_CALLBACK")
    result = await replay_market_packet(case, prepared)
    result["native_mode"] = native_mode
    result["native_evidence"] = "CONTROLLED_NOT_HISTORICAL"
    result["publications"] = deepcopy(transport.publications)
    result["original_market_coverage"] = deepcopy(prepared.get("coverage"))
    result["initialization"] = deepcopy(initialization)
    return result


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_input_key:{key}")
        result[key] = value
    return result


def load_evidence(path):
    raw = Path(path).read_bytes()
    if sha256(raw).hexdigest() != EXPORT_SHA256:
        raise ValueError("retained_export_sha256_mismatch")
    rows = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(rows, list) or len(rows) != 122:
        raise ValueError("retained_case_universe_mismatch")
    result = {}
    for case_id, (index, symbol, day, flip) in CASES.items():
        row = rows[index]
        if (row["sym"], row["day"], row["flip"]) != (symbol, day, flip):
            raise ValueError("retained_case_identity_mismatch")
        result[case_id] = row
    return result


def evidence_summary(case_id, row):
    index, symbol, day, flip = CASES[case_id]
    if (row["sym"], row["day"], row["flip"]) != (symbol, day, flip):
        raise ValueError("case_summary_identity_mismatch")
    return {
        "case_id": case_id, "source_sha256": EXPORT_SHA256, "source_pointer": f"/{index}",
        "day": day, "symbol": symbol, "flip_bar_et": flip,
        "census_P_not_arm_override": row["P"], "previous_sell_et": row["prev_sell"],
        "source_group": row["group"], "reported_taken": row["taken"],
        "watched_prev": row["watched_prev"], "allowed_prev": row["allowed_prev"],
        "allowed_flip": row["allowed_flip"],
        "retained_log_count": len(row["bot_log_lines"]),
        "retained_order_count": len(row["orders_symbol_day"]),
        "retained_fill_count": len(row["fills_symbol_day"]),
        "arm_lines_verbatim": [line for line in row["bot_log_lines"] if "[V2-CW-ARM]" in line],
        "historical_native_book": "UNKNOWN_NOT_STORED_IN_THIS_EXPORT",
        "market_replay": "UNKNOWN_BARS_QUOTES_AND_ASOF_SETTINGS_NOT_IN_THIS_EXPORT",
        "historical_buy_acceptance": "UNKNOWN_NOT_EXECUTED",
    }


class PacketTransport:
    """Private packet delivery only; not Redis or a broker connection."""

    def __init__(self, expected_channel):
        self.expected_channel = expected_channel
        self.raw = None
        self.publications = []

    async def get(self, key):
        if key != self.expected_channel:
            raise ValueError("unexpected_packet_channel")
        return self.raw

    async def xadd(self, stream, data, **kwargs):
        self.publications.append({"stream": stream, "data": dict(data)})
        return f"{len(self.publications)}-0"


def install_recording_emitters(case):
    """Same final hook and primary submission callback as actual service boot.

    This replaces external Redis delivery only. It does not replace a strategy,
    owner, line, country, cap or journal decision and cannot create a draft.
    """
    from project_mai_tai.brokerdecides1_sync import account_names, channel
    from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2IntentEmitter

    transport = getattr(case, "_l12_case_transport", None)
    if transport is None:
        transport = PacketTransport(channel(case.settings))
        case._l12_case_transport = transport
    case.bot.redis = transport

    class RecordingEmitter(SchwabV2IntentEmitter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.intents = []
            self.atr_sell_observations = []
            self.confirmation_exits = []

        async def emit(self, draft, **kwargs):
            result = await super().emit(draft, **kwargs)
            self.intents.append(deepcopy(draft))
            return result

    primary, secondary = account_names(case.settings)
    case.schwab = RecordingEmitter(case.settings, transport, primary,
        on_submitted=case.strategy.note_primary_resting_submission,
        before_entry_publish=case.bot._brokerdecides1_before_entry_publish)
    case.webull = RecordingEmitter(case.settings, transport, secondary,
        before_entry_publish=case.bot._brokerdecides1_before_entry_publish)
    case.bot.intent_emitter = case.schwab
    case.bot.webull_intent_emitter = case.webull
    return transport


async def consume_publications(transport, oms):
    """Pass actual serialized emitter events to the actual supplied OMS.

    The caller supplies its separately reviewed private service/clock/venue.
    No event rewrite, accepted status fabrication, adapter injection, risk
    callback replacement or control-generation override occurs here.
    """
    from project_mai_tai.events import TradeIntentEvent

    receipts = []
    for publication in transport.publications:
        raw = publication["data"].get("data")
        envelope = json.loads(raw, object_pairs_hook=unique_object)
        if envelope.get("event_type") != "trade_intent":
            receipts.append({"event_type": envelope.get("event_type"), "status": "NOT_AN_ORDER_INTENT"})
            continue
        event = TradeIntentEvent.model_validate(envelope)
        await oms._handle_stream_message({"data": raw})
        receipts.append({
            "event_id": str(event.event_id), "symbol": event.payload.symbol,
            "account": event.payload.broker_account_name,
            "intent_type": event.payload.intent_type,
            "status": "CONSUMER_RETURNED_NOT_AN_ACCEPTANCE_ASSERTION",
        })
    return receipts


async def consume_book(case, packet):
    """Use the real publisher/codec/consumer, with no default empty accounts.

    A packet must explicitly label CONTROLLED or RETAINED source evidence.
    Historical flat cannot be reconstructed from final local order statuses.
    """
    from project_mai_tai.brokerdecides1 import NativeAccountBook, NativeBookPublisher, encode
    from project_mai_tai.brokerdecides1_sync import account_names, channel, refresh_consumer

    transport = getattr(case, "_l12_case_transport", None)
    if transport is None:
        transport = PacketTransport(channel(case.settings))
        case._l12_case_transport = transport
    case.bot.redis = transport
    if packet is None:
        transport.raw = None
        await refresh_consumer(case.bot)
        return
    if packet.get("evidence_kind") not in {"CONTROLLED", "RETAINED"} or not packet.get("source_ref"):
        raise ValueError("native_packet_provenance_required")
    publisher = getattr(case, "_l12_case_publisher", None)
    if publisher is None:
        publisher = NativeBookPublisher(epoch=packet["epoch"], accounts=account_names(case.settings))
        case._l12_case_publisher = publisher
    if publisher.epoch != packet["epoch"]:
        raise ValueError("unreviewed_case_epoch_change")
    generation = publisher.begin(packet["started_ms"])
    books = []
    for source in packet["books"]:
        data = dict(source)
        data["working"] = frozenset(data["working"])
        data["positions"] = frozenset(data["positions"])
        books.append(NativeAccountBook(**data))
    value = publisher.finish(
        generation, completed_ms=packet["completed_ms"], books=tuple(books),
        inflight=packet["inflight"],
        filled_segments=tuple(tuple(pair) for pair in packet["filled_segments"]),
        local_complete=packet["local_complete"], reason=packet["reason"],
    )
    transport.raw = encode(value)
    await refresh_consumer(case.bot)


def guard_receipt(case):
    from project_mai_tai.brokerdecides1_entry import entry_decision, line_segment

    state = case.strategy.watchlist_state(case.symbol)
    decision = entry_decision(case.strategy, case.symbol)
    return {
        "native_card": asdict(decision),
        "owner_phase": state.flip_owner_phase,
        "opportunity_id": state.flip_owner_opportunity_id,
        "fanout_segment_id": state.fanout_segment_id,
        "resting_taken": state.cw_resting_taken, "reclaim_taken": state.cw_reclaim_taken,
        "line_ready": case.strategy.line_buy_ready(case.symbol),
        "line_incomplete_reason": case.snapshot()["incomplete_reason"],
        "observed_ms": case.now_ms,
        "atr_state_from_callback": state.atr_state,
        "short_ms_from_callback": state.atr_short_flip_bar_ts,
        "line_segment_from_callback": line_segment(state),
        "owner_counters": dict(getattr(case.strategy, "_flip_owner_counts", {})),
        "draft_count": sum(d.side == "buy" and d.intent_type == "open"
            for sink in (case.schwab, case.webull) for d in sink.intents),
        "scope": "NATIVE_CARD_IS_NOT_FULL_ENTRY_OR_OMS_ACCEPTANCE",
    }


async def replay_market_packet(case, packet):
    """Feed actual bot bar/quote paths. Caller configures real private stores.

    Packet producer supplies closed-bar receipts and explicit coverage; this
    helper never attests full history from bar count or changes owner/cap notes.
    Missing operands stop before feeding anything. No permission method mocks.
    """
    required = ("source_ref", "settings_source", "events", "coverage")
    missing = [key for key in required if key not in packet]
    if missing:
        return {"status": "UNKNOWN", "missing": missing, "steps": []}
    partial = packet["coverage"].get("complete") is not True
    if partial and packet.get("purpose") != "PARTIAL_CALLBACK_DIAGNOSTIC":
        return {"status": "UNKNOWN", "missing": ["complete_market_coverage"], "steps": []}
    if getattr(case, "_l12_case_transport", None) is None:
        raise ValueError("canonical_recording_emitters_must_be_installed_first")
    steps = []
    prior = None
    if not isinstance(packet["events"], list):
        raise ValueError("market_events_array_required")
    # Validate source identity/clock order before any application mutation.
    for item in packet["events"]:
        key = (item["received_ms"], item["sequence"])
        if prior is not None and key <= prior:
            raise ValueError("market_event_causal_order_required")
        prior = key
        if item["kind"] not in {"bar", "quote"}:
            raise ValueError("unsupported_market_event_kind")
        if item["row"]["symbol"] != case.symbol or not item["source_pointer"]:
            raise ValueError("market_event_identity_or_provenance_required")
        field = "bar_time" if item["kind"] == "bar" else "event_ts"
        stamp = datetime.fromisoformat(item["row"][field])
        if stamp.tzinfo is None:
            raise ValueError("market_event_timezone_required")
        available = int(stamp.timestamp() * 1000) + (60_000 if item["kind"] == "bar" else 0)
        if available > item["received_ms"]:
            raise ValueError("forming_bar_or_future_quote")
    if packet["events"] and case.now_ms > packet["events"][0]["received_ms"]:
        raise ValueError("replay_clock_rewind_before_first_event")
    prior = None
    for item in packet["events"]:
        key = (item["received_ms"], item["sequence"])
        if prior is not None and key <= prior:
            raise ValueError("market_event_causal_order_required")
        prior = key
        with case.clock(item["received_ms"]):
            await consume_book(case, item.get("native_packet"))
            if item["kind"] == "bar":
                await case.feed_bar(item["row"], now_ms=item["received_ms"], phase=item["phase"])
                await case.rebuild()
            elif item["kind"] == "quote":
                from project_mai_tai.market_data.schwab_v2_rest_client import Quote
                row = item["row"]
                stamp = int(datetime.fromisoformat(row["event_ts"]).timestamp() * 1000)
                if stamp > item["received_ms"]:
                    raise ValueError("future_quote")
                await case.bot._handle_quote(case.symbol, Quote(case.symbol,
                    float(row["bid_price"]), float(row["ask_price"]),
                    float(row["last_price"]), stamp))
            else:
                raise ValueError("unsupported_market_event_kind")
            steps.append({"raw_pointer": item["source_pointer"], **guard_receipt(case)})
    return {
        "status": "UNKNOWN_PARTIAL_MARKET_CALLBACK_DIAGNOSTIC" if partial else "REPLAYED_NOT_HISTORICAL_NATIVE_ACCEPTANCE",
        "steps": steps,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retained-export", required=True, type=Path)
    parser.add_argument("--case", choices=tuple(CASES), required=True)
    args = parser.parse_args()
    rows = load_evidence(args.retained_export)
    print(json.dumps(evidence_summary(args.case, rows[args.case]), sort_keys=True))
    return 2  # Inventory alone cannot qualify either historical BUY.


if __name__ == "__main__":
    raise SystemExit(main())
