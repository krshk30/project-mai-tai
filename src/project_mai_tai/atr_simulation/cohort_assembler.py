"""Retained-row -> canonical EpisodeInput adapter; no ATR/body/fill/exit policy.

Flip/trigger/sizing and causal identity links are explicit evidence operands.
Empty indicators, row counts, DB UUID order and receive lag cannot supply them.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
import time

from .engine_binding import ENGINE_HEAD, IO_COMPONENT_HEAD
from .cohort_sources import scanner_symbols
from .cohort_provenance import account_membership, actual_fill_identity, physical_identity, proven_no_buy
from .repository import digest
from .service_contract import SealedCohort


VERSION = "ATRSIM1_CAUSAL_COHORT_V2"
MINUTE = 60_000_000_000


def integer(value, *, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        raise ValueError("exact_integer_operand_required")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("exact_integer_operand_required") from error
    if (not number.is_finite() or number != number.to_integral_value()
            or number < minimum or number > 2**63 - 1):
        raise ValueError("exact_integer_operand_required")
    return int(number)


def price(value):
    if isinstance(value, (bool, float)):
        raise ValueError("lossless_decimal_operand_required")
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError) as error:
        raise ValueError("lossless_decimal_operand_required") from error
    if not number.is_finite() or number <= 0:
        raise ValueError("positive_finite_price_required")
    return number


def timestamp_ns(value):
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?(?:Z|[+-]\d\d:\d\d)", value):
        raise ValueError("explicit_zoned_source_timestamp_required")
    whole, fraction = value, 0
    match = re.search(r"\.(\d{1,9})", value)
    if match:
        fraction = int(match[1].ljust(9, "0"))
        whole = value[:match.start()] + value[match.end():]
    instant = datetime.fromisoformat(whole.replace("Z", "+00:00"))
    delta = instant.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + fraction


def _ref(t, value):
    if (not isinstance(value, dict) or not value.get("artifact") or not value.get("pointer")
            or not re.fullmatch(r"[0-9a-f]{64}", value.get("sha256", ""))):
        raise ValueError("explicit_source_reference_required")
    asof = value.get("asof_ns")
    return t.SourceRef(value["artifact"], value["sha256"], value["pointer"],
        t.EvidenceKind(value["kind"]), None if asof is None else integer(asof))


def _boundary(t, value):
    if value is None:
        return None
    available = integer(value["available_ns"])
    bar = value.get("bar_start_ns")
    if bar is not None and available < integer(bar) + MINUTE:
        raise ValueError("flip_available_before_bar_close")
    source = _ref(t, value["source"])
    if source.asof_ns is not None and source.asof_ns > available:
        raise ValueError("future_flip_evidence_cannot_be_used_earlier")
    return t.Boundary(available, source, None if bar is None else integer(bar))


def _available_ref(t, value, at_ns):
    if value is None:
        return None
    source = _ref(t, value)
    return source if source.asof_ns is not None and source.asof_ns <= at_ns else None


def _coverage(t, value, extra=()):
    complete = value.get("complete")
    if complete is not None and not isinstance(complete, bool):
        raise ValueError("three_state_coverage_required")
    refs = tuple(_ref(t, item) for item in value.get("sources", []))
    raw_gaps = value.get("gaps", [])
    if not isinstance(raw_gaps, list) or any(not isinstance(item, str) for item in raw_gaps):
        raise ValueError("explicit_coverage_gap_list_required")
    gaps = tuple(dict.fromkeys((*raw_gaps, *extra)))
    start, end = integer(value["start_ns"]), integer(value["end_ns"])
    if end < start:
        raise ValueError("coverage_window_reversed")
    if complete is True and (gaps or not refs):
        complete = None
        gaps = tuple(dict.fromkeys((*gaps, "UNSUPPORTED_COMPLETENESS_CLAIM")))
    return t.Coverage(start, end, complete, gaps, refs)


def _event(t, packet, link):
    row = packet["row"]
    stamp = row["filled_at"] if packet["kind"] == "live_fill" else row["event_ts"]
    event_ns = timestamp_ns(stamp)
    if integer(link["event_ns"]) != event_ns or not link.get("raw_id"):
        raise ValueError("causal_link_conflicts_with_actual_row_clock_or_id")
    # sequence is an externally evidenced common serialization, NEVER DB id order.
    sequence = integer(link["sequence"])
    source = _ref(t, link["source"])
    received = row.get("received_at")
    return t.EventKey(event_ns, sequence, link["raw_id"], source,
        None if received is None else timestamp_ns(received))


def _membership(t, spec, indexed, buy, physical, observed_at_ns):
    coverage = _coverage(t, spec.get("watched_coverage", {
        "start_ns": buy.available_ns, "end_ns": buy.available_ns, "complete": None}))
    watched, proof = account_membership(spec, physical, indexed, buy.available_ns,
        coverage, observed_at_ns, timestamp_ns)
    return watched, None if proof is None else _ref(t, proof["source"])


def assemble_cohort(plan, packets, t, *, observed_at_ns=None, execution_policy=None):
    if observed_at_ns is not None:
        try:
            observed_at_ns = integer(observed_at_ns, minimum=1)
        except ValueError:
            observed_at_ns = None
        if observed_at_ns is not None and observed_at_ns > time.time_ns():
            observed_at_ns = None
    if plan.get("version") != VERSION or not 1 <= len(plan.get("episodes", [])) <= 4096:
        raise ValueError("bounded_versioned_cohort_plan_required")
    if not plan.get("source_id") or len(plan["source_id"]) > 96:
        raise ValueError("bounded_source_scope_required")
    indexed = {}
    for packet in packets:
        if packet["key"] in indexed:
            raise ValueError("duplicate_raw_packet_key")
        indexed[packet["key"]] = packet
    event_links = plan.get("event_links", {})
    if set(event_links) - set(indexed):
        raise ValueError("event_link_references_missing_actual_row")
    source_refs = tuple(_ref(t, item) for item in plan["source_refs"])
    if not source_refs:
        raise ValueError("population_provenance_required")
    episodes, diagnostics, allocations = [], [], set()
    declared_allocations = [link["packet_key"] for spec in plan["episodes"] for link in spec.get("live_allocations", [])]
    if len(set(declared_allocations)) != len(declared_allocations):
        raise ValueError("one_actual_fill_cannot_be_allocated_to_two_episodes")
    account_set = {tuple(account[name] for name in ("broker", "account_id", "strategy"))
                   for account in plan["accounts"]}
    if len(account_set) != len(plan["accounts"]):
        raise ValueError("duplicate_account_scope")
    for spec in plan["episodes"]:
        physical = physical_identity(spec, plan, indexed)
        identity = t.FlipIdentity(**spec["identity"])
        if identity.strategy != "schwab_1m_v2" or (
                identity.broker, identity.account_id, identity.strategy) not in account_set:
            raise ValueError("episode_outside_declared_account_scope")
        buy, sell = _boundary(t, spec["buy_flip"]), _boundary(t, spec.get("sell_flip"))
        session_end = _boundary(t, spec["session_end"])
        if buy is None or session_end is None or session_end.available_ns <= buy.available_ns:
            raise ValueError("actual_buy_and_session_boundaries_required")
        watched, watched_ref = _membership(t, spec, indexed, buy, physical, observed_at_ns)
        trigger_source = _available_ref(t, spec.get("trigger_source"), buy.available_ns)
        sizing_source = _available_ref(t, spec.get("sizing_source"), buy.available_ns)
        trades, quotes, gaps, unusable, raw_seen, order_seen = [], [], [], [], {}, set()
        prefix = buy.available_ns // MINUTE * MINUTE
        provider = spec.get("market_provider")
        if not provider:
            raise ValueError("explicit_single_market_provider_required")
        for key, packet in indexed.items():
            row = packet["row"]
            if packet["kind"] not in {"trade", "quote"} or row.get("symbol") != identity.symbol:
                continue
            if row.get("provider") != (spec.get("quote_provider", provider) if packet["kind"] == "quote" else provider):
                continue
            try:
                at = timestamp_ns(row["event_ts"])
                if at < prefix or at > session_end.available_ns + MINUTE:
                    continue
                if key not in event_links:
                    raise ValueError("MISSING_CAUSAL_EVENT_LINK")
                event = _event(t, packet, event_links[key])
                raw = (packet["kind"], event.raw_id)
                signature = digest({"kind": packet["kind"], "row": row})
                if raw in raw_seen:
                    if raw_seen[raw] != signature:
                        raise ValueError("CONFLICTING_PROVIDER_EVENT_IDENTITY")
                    continue
                raw_seen[raw] = signature
                order = (packet["kind"], event.event_ns, event.sequence)
                if order in order_seen:
                    raise ValueError("AMBIGUOUS_SHARED_EVENT_SERIALIZATION")
                order_seen.add(order)
                if packet["kind"] == "trade":
                    from .execution_eligibility import classify_execution

                    trade = t.TradePrint(event, identity.symbol, price(row["price"]), integer(row["size"]))
                    trades.append(t.TradePrint(event, identity.symbol, trade.price, trade.size,
                        execution=classify_execution(trade, row.get("conditions"), execution_policy,
                            conditions_present="conditions" in row)))
                else:
                    quotes.append(t.Quote(event, identity.symbol,
                        None if row.get("bid_price") is None else price(row["bid_price"]),
                        None if row.get("ask_price") is None else price(row["ask_price"]),
                        None if row.get("bid_size") is None else integer(row["bid_size"]),
                        None if row.get("ask_size") is None else integer(row["ask_size"])))
            except (ValueError, KeyError) as error:
                unusable.append({"packet_key": key, "issue": str(error)})
                gaps.append("UNKNOWN_CAUSAL_MARKET_INPUT:" + key)
        tape_value = spec["coverage"]["tape"]
        if integer(tape_value["start_ns"]) > prefix:
            gaps.append("UNKNOWN_PRE_TOUCH_CURRENT_MINUTE_PREFIX")
        tape = _coverage(t, tape_value, gaps)
        entries, exits = [], []
        for packet in indexed.values():
            raw = packet["row"]
            if packet["kind"] == "live_fill" and raw.get("symbol") == identity.symbol and (
                    raw.get("account_name") == physical["account_name"]
                    or (raw.get("fill_account_id") == physical["account_id"]
                        and raw.get("fill_strategy_id") == physical["strategy_id"])):
                actual_fill_identity(raw, physical, identity.symbol)
        for link in spec.get("live_allocations", []):
            key = link["packet_key"]
            packet = indexed[key]
            row = packet["row"]
            if (packet["kind"] != "live_fill" or row["symbol"] != identity.symbol
                    or row["account_name"] != spec["account_name"] or row["broker"] != identity.broker):
                raise ValueError("actual_fill_account_symbol_or_broker_mismatch")
            actual_fill_identity(row, physical, identity.symbol)
            if key in allocations or link.get("allocation") != "FULL_FILL":
                raise ValueError("explicit_unique_full_fill_allocation_required_no_FIFO")
            allocations.add(key)
            _ref(t, link["source"])
            event = _event(t, packet, event_links[key])
            fact_source = t.SourceRef("atrsim-input:" + plan["source_id"], digest(packet),
                key, t.EvidenceKind.RETAINED)
            quantity = integer(row["quantity"], minimum=1)
            if row["side"].lower() == "buy":
                entries.append(t.EntryFact(str(row["fill_id"]), event, price(row["price"]), quantity, fact_source))
            elif row["side"].lower() == "sell":
                mapped = link.get("mapped_reason")
                exits.append(t.LiveExitFact(str(row["fill_id"]), event, price(row["price"]), quantity,
                    row.get("intent_reason_raw"), None if mapped is None else t.LiveReason(mapped), fact_source))
            else:
                raise ValueError("unrecognized_actual_fill_side")
        entries.sort(key=lambda item: (item.at.event_ns, item.at.sequence))
        exits.sort(key=lambda item: (item.at.event_ns, item.at.sequence))
        unallocated = [key for key, packet in indexed.items() if packet["kind"] == "live_fill"
            and packet["row"].get("symbol") == identity.symbol
            and packet["row"].get("fill_account_id") == physical["account_id"]
            and packet["row"].get("fill_strategy_id") == physical["strategy_id"] and key not in declared_allocations]
        live_coverage = _coverage(t, spec["coverage"]["live"],
            ("UNKNOWN_ACTUAL_EPISODE_FILL_ALLOCATION",) if unallocated else ())
        bought = True if entries else spec.get("live_bought")
        if bought is not None and not isinstance(bought, bool):
            raise ValueError("three_state_live_buy_status_required")
        no_buy_source = None if spec.get("no_buy_source") is None else _ref(t, spec["no_buy_source"])
        episode_end = min(session_end.available_ns, sell.available_ns if sell is not None else session_end.available_ns)
        if bought is False and not proven_no_buy(spec, physical, indexed, buy.available_ns,
                episode_end, live_coverage, no_buy_source, observed_at_ns):
            bought = None
        live = t.LiveFacts(bought, tuple(entries), tuple(exits), live_coverage,
            () if bought is not None else ("UNKNOWN_LIVE_BUY_STATUS",))
        episode = t.EpisodeInput(identity, buy, watched, watched_ref,
            None if spec.get("trigger") is None or trigger_source is None else price(spec["trigger"]),
            None if spec.get("quantity") is None or sizing_source is None else integer(spec["quantity"], minimum=1),
            sizing_source, sell, session_end,
            tuple(sorted(trades, key=lambda item: (item.key.event_ns, item.key.sequence))),
            tuple(sorted(quotes, key=lambda item: (item.key.event_ns, item.key.sequence))), tape,
            _coverage(t, spec["coverage"]["flip"]), (), _coverage(t, spec["coverage"]["today"]), live,
            execution_policy=execution_policy)
        episodes.append(episode)
        diagnostics.append({"identity": spec["identity"], "unusable_raw_records": unusable,
            "trade_count": len(trades), "quote_count": len(quotes), "tape_gaps": tape.gaps,
            "unallocated_live_fact_keys": unallocated, "watched_membership": watched,
            "account_identity_packet_key": spec["account_identity_packet_key"],
            "trigger_source": trigger_source, "sizing_source": sizing_source,
            "native_asof": "UNKNOWN", "bar_original_asof": "UNKNOWN"})
    seal = plan.get("seal", {})
    seal_refs = tuple(_ref(t, item) for item in seal.get("sources", []))
    actual_keys = [digest(spec["identity"]) for spec in plan["episodes"]]
    if len(set(actual_keys)) != len(actual_keys):
        raise ValueError("duplicate_episode_identity")
    expected_keys = seal.get("expected_episode_keys", [])
    exact_population = (bool(expected_keys) and len(set(expected_keys)) == len(expected_keys)
        and set(actual_keys) == set(expected_keys))
    observed = {symbol for packet in packets if packet["kind"] == "snapshot"
                for symbol in scanner_symbols(packet["row"]["event"])}
    represented = {episode.identity.symbol for episode in episodes}
    no_flip_proofs = set()
    for proof in plan.get("no_buy_flip_proofs", []):
        coverage = _coverage(t, proof["coverage"])
        if (coverage.complete is True and coverage.start_ns <= integer(plan["scope_start_ns"])
                and coverage.end_ns >= integer(plan["scope_end_ns"])):
            no_flip_proofs.add(proof["symbol"])
    unbound_symbols = sorted(set(plan.get("unbound_watched_symbols", []))
        | (observed - represented - no_flip_proofs))
    sealed = (seal.get("all_overlapping_accounts_included") is True and bool(seal_refs)
        and exact_population and not unbound_symbols)
    coverage = {"engine_dependency_head": ENGINE_HEAD, "io_component_head": IO_COMPONENT_HEAD,
        "complete": None, "all_overlapping_accounts_included": sealed,
        "native_asof": "UNKNOWN", "bar_original_asof": "UNKNOWN", "episode_diagnostics": diagnostics,
        "observed_scanner_symbols": sorted(observed), "unbound_watched_symbols": unbound_symbols,
        "missing_episode_keys": sorted(set(expected_keys) - set(actual_keys)),
        "unexpected_episode_keys": sorted(set(actual_keys) - set(expected_keys)),
        "raw_packet_count": len(packets), "raw_packet_sha256": digest(packets)}
    coverage["evidence_cutoff_ns"] = observed_at_ns
    return SealedCohort(plan["source_id"], plan["policy_version"], tuple(episodes),
        (*source_refs, *seal_refs), sealed, coverage)
