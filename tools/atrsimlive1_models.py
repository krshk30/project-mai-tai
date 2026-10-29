"""TODAY stored-input boundary; existing engines own every trading rule.

Incomplete input is explicit. Source availability is not market completeness;
the C119:54:03 NULL interpretation requires an explicit TODAY-paper opt-in.
"""

from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, time, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from atrsimlive1_collect import digest, plain
from atrsimlive1_runtime import KEYS, SCHEMA


ET = ZoneInfo("America/New_York")
NS = 1000000000
MINUTE = 60 * NS
POLICY_SHA = "e42e5075bd296144b2a632438de01be1c7f1afe8ebaed34cd528d58f34e39cf3"


def nanos(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        raise ValueError("aware_source_event_required")
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * NS + delta.microseconds * 1000


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("positive_source_number_required")
    return number


def integral(value):
    number = positive(value)
    if number != number.to_integral_value():
        raise ValueError("whole_source_quantity_required")
    return int(number)


def empty_table(key, source_id, issues):
    return dict(updated_at=None, source_asof=None, read_status="UNAVAILABLE",
                source_id=source_id + ":" + key, issues=list(issues), rows=[],
                known_pnl_label="UNKNOWN", skipped_count=None, unknown_count=None,
                truncated=False)


def stored_print_execution(trade, conditions, policy, *, column_present=True, null_policy=None):
    from atrsimlive1_null_policy import classify_condition_value

    return classify_condition_value(trade, conditions, policy,
        column_present=column_present, null_policy=null_policy)


def aggregate_clock_issues(closes):
    # The unchanged baseline requires exact completed-minute oracle clocks.
    # A later recorded availability cannot be backdated to satisfy that API.
    return (("UNKNOWN_AGGREGATE_STORED_BAR_AVAILABILITY_CONTRACT",)
            if any(row.available_ns != row.bar_start_ns + MINUTE for row in closes) else ())


def tie_report_touched(report):
    if report is None:
        return None
    counts = [report[key] for key in ('entry_groups_blocked', 'exit_stop_wins', 'limit_pricing_fills')]
    if any(type(value) is not int or value < 0 for value in counts):
        raise ValueError('actual_tie_action_counts_required')
    return any(counts)


def canonical_row(receipt, *, known, amount, coverage_note, sizing=None, percent_known=False):
    from project_mai_tai.atr_simulation.cp_sim_display import _legacy_row

    row = plain(_legacy_row(receipt,
        dict(completed=True, known_pnl=amount) if known else None))
    # PAGE bc763 consumes the canonical inner row, not its identity wrapper.
    # Keep the original summary/exit reason intact and add plain coverage text.
    row["coverage_note"] = coverage_note
    from atrsimlive1_line_route import percent

    pct = percent(receipt.get("fills", []), receipt.get("exits", [])) if percent_known else None
    row.update(pnl_pct=None if pct is None else str(pct), pnl_pct_label_known=pct is not None,
               pnl_pct_label="UNKNOWN" if pct is None else format(pct, "+.2f") + "%",
               sizing_status="UNKNOWN_UNITS" if sizing is None else sizing["sizing_status"],
               percent_basis="UNKNOWN" if sizing is None else sizing["percent_basis"])
    if sizing is not None and not sizing["usd_known"]:
        row.update(quantity="UNKNOWN", pnl=None, pnl_label="UNKNOWN", pnl_label_known=False)
    return row


def produce(data, *, policy_path, producer, route_packet=None, same_sip_policy=None, null_policy=None, incremental=None,
            historical_routes=None):
    # Only pure existing SIM modules. No worker, bootstrap, repository writer,
    # broker adapter package, application construction or provider access.
    from project_mai_tai.atr_simulation import types as t
    from project_mai_tai.atr_simulation.engine import SimulationEngine
    from project_mai_tai.atr_simulation.execution_eligibility import classify_execution, policy_from_payload
    from project_mai_tai.atr_simulation.firstbar import CompletedFlipBar
    from project_mai_tai.atr_simulation.firstbar import plan_firstbar_entry, FirstBarMode
    from project_mai_tai.atr_simulation.firstbar_replay import (
        FirstBarReplayEngine, FirstBarReplayInput, FirstBarFillPolicy, FIRSTBAR_REPLAY_VERSION,
    )
    from project_mai_tai.atr_simulation.firstbar_live_baseline import (
        AggregateBaselineInput, BaselineDay, BaselineBarClose,
        AGGREGATE_ASSUMPTION, simulate_aggregate_cohort, BASELINE_VERSION, BASELINE_POLICY_VERSION,
    )
    from project_mai_tai.atr_simulation.firstbar_study import StudyBuyLeg
    from project_mai_tai.atr_simulation.cohort_provenance import actual_fill_identity
    from project_mai_tai.atr_simulation.read_model import result_rows
    from atrsimlive1_line_route import recompute, route_fact, size, parity
    from atrsimlive1_null_policy import require_null_policy

    require_null_policy(null_policy)
    EpisodeInput = t.EpisodeInput
    window_view = None
    if "window_view" in data:
        from atrsimlive1_window_view import validate_view

        window_view = validate_view(data)
        if window_view.get('null_policy') != null_policy:
            raise ValueError('same_terminal_and_model_null_policy_required')
    if same_sip_policy is not None:
        from project_mai_tai.atr_simulation.same_sip_binding import verify_same_sip_files

        if same_sip_policy != verify_same_sip_files():
            raise ValueError("reviewed_same_sip_policy_required")
        # Additive TODAY-only family. Original nine sealed kernel modules and
        # their loaders are never rebound or overwritten by this producer.
        from project_mai_tai.atr_simulation.same_sip_types import EpisodeInput
        from project_mai_tai.atr_simulation.same_sip_engine import SimulationEngine
        from project_mai_tai.atr_simulation.same_sip_firstbar import (
            CompletedFlipBar, plan_firstbar_entry, FirstBarMode,
        )
        from project_mai_tai.atr_simulation.same_sip_firstbar_replay import (
            FirstBarReplayEngine, FirstBarReplayInput, FirstBarFillPolicy, FIRSTBAR_REPLAY_VERSION,
        )
        from project_mai_tai.atr_simulation.same_sip_firstbar_live_baseline import (
            AggregateBaselineInput, BaselineDay, BaselineBarClose, AGGREGATE_ASSUMPTION,
            simulate_aggregate_cohort, BASELINE_VERSION, BASELINE_POLICY_VERSION,
        )

    day, asof = data["trading_day"], data["source_asof"]
    source_id = "atrsimlive1:" + digest(dict(day=day, asof=asof, hashes=data["source_hashes"]))[:32]
    tables = {key: empty_table(key, source_id, ["UNKNOWN_TYPED_INPUT_BINDING"]) for key in KEYS}
    source = t.SourceRef("postgres:readonly-snapshot:" + data["snapshot_id"],
                         digest(data["source_hashes"]), "$", t.EvidenceKind.RETAINED, nanos(asof))
    policy = None
    if policy_path is not None:
        raw = Path(policy_path).read_bytes()
        if len(raw) > 2 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != POLICY_SHA:
            raise ValueError("retained_execution_policy_hash_mismatch")
        policy_ref = t.SourceRef(str(policy_path), POLICY_SHA, "$", t.EvidenceKind.RETAINED, None)
        policy = policy_from_payload(json.loads(raw), policy_ref)
    cache_models = None
    if incremental is not None:
        from atrsimlive1_incremental_models import IncrementalModels

        if incremental.model_controller is None:
            incremental.model_controller = IncrementalModels(incremental)
        cache_models = incremental.model_controller
        cache_models.require(producer, null_policy=null_policy, same_sip_policy=same_sip_policy,
                             execution_rules_sha256=POLICY_SHA if policy is not None else None,
                             policy_source=None if policy is None else policy.source)
    scope_issues = ["UNKNOWN_ORIGINAL_WATCH_WINDOW_COVERAGE",
                    "UNKNOWN_STORED_TAPE_COMPLETENESS"]
    if policy is None:
        scope_issues.append("UNKNOWN_RETAINED_EXECUTION_POLICY")
    grouped = {kind: defaultdict(list) for kind in data["rows"]}
    for kind, rows in data["rows"].items():
        for row in rows:
            grouped[kind][row["symbol"]].append(row)
    all_episodes, firstbar_inputs, aggregate_inputs = [], [], []
    episode_origins = []
    firstbar_results, core_results = (), ()
    firstbar_reports, core_reports, aggregate_reports = {}, {}, {}
    contexts, engine_receipts = {}, {}
    tie_identities, tie_reports, tie_conversions = set(), {}, []
    parity_reports, route_reports, line_inputs = [], [], []
    observed_flips, input_issues, envelope_metrics = [], [], []
    session_ns = nanos(datetime.combine(datetime.fromisoformat(day).date(), time(20), ET))
    cutoff_ns = nanos(datetime.combine(datetime.fromisoformat(day).date(), time(15, 45), ET))
    session_end = t.Boundary(session_ns, source)
    # Keep the actual 20:00 boundary. An as-of run must never create an early
    # session-end exit by substituting its observation clock.
    for symbol in data["symbols"]:
        bars = sorted(grouped["bars"][symbol], key=lambda row: row["bar_time"])
        trades, quotes, closes, flips, faults, fault_windows = [], [], [], [], [], []
        from atrsimlive1_progress import stage
        stage("ORACLE:" + symbol, "+", len(bars))
        line_rows = recompute(bars, nanos=nanos)
        stage("ORACLE:" + symbol, "-", len(line_rows))
        parity_reports.append(parity(day, symbol, line_rows, asof_ns=nanos(asof), nanos=nanos))
        if historical_routes is None:
            route = route_fact(route_packet, day=day, symbol=symbol, asof_ns=nanos(asof), nanos=nanos)
        else:
            from atrsimlive1_captured_rr import SealedRoutes

            if type(historical_routes) is not SealedRoutes or route_packet is not None:
                raise ValueError('explicit_sealed_historical_routes_only')
            route = historical_routes.resolve(data, symbol)
        route_reports.append(dict(symbol=symbol, **route))
        sizing_ref = (t.SourceRef(route["source"]["artifact"], route["source"]["sha256"],
                                 "ATR-COUNTRY-SELECTED:" + symbol, t.EvidenceKind.RETAINED,
                                 nanos(route["source"]["asof"]))
                      if route["status"] == "RECORDED_DAY_ROUTE" else
                      replace(source, kind=t.EvidenceKind.DERIVED,
                              pointer="NORMALIZED_ONE_UNIT_PRICE_ANALYSIS_NOT_NATIVE_SIZING"))
        previous = None
        for computed in line_rows:
            bar, oracle = computed["original"], computed["oracle"]
            start, available = computed["start_ns"], computed["available_ns"]
            state, raw_line = oracle["state"], oracle["trail"]
            candidate_line = None if raw_line is None else Decimal(str(raw_line))
            line = candidate_line if candidate_line is not None and candidate_line.is_finite() and candidate_line > 0 else None
            derived = replace(source, kind=t.EvidenceKind.DERIVED,
                pointer="line recomputed from stored bars;bar:" + str(bar["id"]) + ";simulation-close-clock")
            closes.append(BaselineBarClose(start, available, positive(bar["close_price"]), line, state, derived))
            line_inputs.append(dict(symbol=symbol, bar_id=str(bar["id"]), bar_time=bar["bar_time"],
                                    created_at=bar["created_at"], source=bar["source"], oracle=oracle,
                                    derived_available_ns=available))
            if previous is not None and start != previous + MINUTE:
                faults.append("UNKNOWN_STORED_BAR_GAP")
                fault_windows.append((previous + MINUTE, start, "UNKNOWN_STORED_BAR_GAP"))
            if oracle["flip"] in ("BUY", "SELL"):
                boundary = t.Boundary(available, derived, start)
                flips.append((oracle["flip"], boundary))
                observed_flips.append(dict(symbol=symbol, side=oracle["flip"], bar_start_ns=start,
                    available_ns=available, bar_id=str(bar["id"]), label="line recomputed from stored bars",
                    actual_live_flip="UNKNOWN"))
            previous = start
        for row in grouped["trades"][symbol]:
            if type(row["size"]) is not int or row["size"] < 0:
                faults.append("UNKNOWN_CAPTURE_PRINT_SIZE")
                at = nanos(row["event_ts"])
                fault_windows.append((at, at + 1, "UNKNOWN_CAPTURE_PRINT_SIZE"))
                continue  # Omission is recorded as a coverage fault, never zero.
            key = t.EventKey(nanos(row["event_ts"]), row["id"],
                             "market_capture_trades:" + str(row["id"]), source,
                             nanos(row["received_at"]))
            trade = t.TradePrint(key, symbol, positive(row["price"]), row["size"])
            # Preserve raw NULL/absence; only the named TODAY successor changes
            # the interpretation of a PRESENT NULL. No payload injection.
            evidence = stored_print_execution(trade, row.get('conditions'), policy,
                column_present='conditions' in row, null_policy=null_policy)
            trades.append(t.TradePrint(key, symbol, trade.price, trade.size, evidence))
        for row in grouped["quotes"][symbol]:
            key = t.EventKey(nanos(row["event_ts"]), row["id"],
                             "market_capture_quotes:" + str(row["id"]), source,
                             nanos(row["received_at"]))
            quotes.append(t.Quote(key, symbol, row["bid_price"], row["ask_price"],
                                  row["bid_size"], row["ask_size"]))
        trades.sort(key=lambda row: (row.key.event_ns, row.key.sequence))
        quotes.sort(key=lambda row: (row.key.event_ns, row.key.sequence))
        trade_clocks = [row.key.event_ns for row in trades]
        quote_clocks = [row.key.event_ns for row in quotes]
        # DB id supplies deterministic serialization only, not provider order.
        if window_view is not None:
            census = window_view["metadata"]["census"]
            for key, reason in (("invalid_size_count", "UNKNOWN_CAPTURE_PRINT_SIZE"),
                                ("same_sip_valid_groups", "UNKNOWN_SAME_SIP_NATIVE_SEQUENCE")):
                for minute in census:
                    if minute[key]:
                        faults.append(reason)
                        clocks = minute['invalid_size_ns' if key == 'invalid_size_count' else 'same_sip_ns']
                        fault_windows.extend((at, at + 1, reason) for at in clocks)
        elif any(a.key.event_ns == b.key.event_ns for a, b in zip(trades, trades[1:])):
            faults.append("UNKNOWN_SAME_SIP_NATIVE_SEQUENCE")
            fault_windows.extend((a.key.event_ns, a.key.event_ns + 1, "UNKNOWN_SAME_SIP_NATIVE_SEQUENCE")
                                 for a, b in zip(trades, trades[1:]) if a.key.event_ns == b.key.event_ns)
        coverage = t.Coverage(nanos(data["start"]), nanos(data["end"]), None,
                              tuple(dict.fromkeys((*scope_issues, *faults))), (source,))
        from atrsimlive1_data_coverage import derive

        if window_view is not None:
            verified_windows = window_view["metadata"]["coverage_windows"]
            coverage_report = window_view["metadata"]["coverage_report"]
        else:
            verified_windows, coverage_report = derive(symbol=symbol, day=day,
                bars=grouped["bars"][symbol], trades=grouped["trades"][symbol],
                read_proof=data["read_proof"], log=data["capture_log"],
                asof_ns=nanos(asof), nanos=nanos)
        envelope_metrics.append(coverage_report)
        input_issues.extend(symbol + ":" + issue for issue in coverage_report["issues"])
        def scoped(at, origin):
            window = next((item for item in verified_windows if item[0] <= at < item[1]), None)
            if window is None:
                return window, trades, coverage, "coverage UNKNOWN: no proved supplied envelope", quotes
            left, right, _, note, _ = window
            gaps = tuple(dict.fromkeys(issue for lo, hi, issue in fault_windows if lo < right and hi > left))
            if window_view is not None:
                from atrsimlive1_window_view import leg_slice

                leg_trades, leg_quotes = leg_slice(window_view['descriptors'], origin,
                    trades, trade_clocks, quotes, quote_clocks, left=left, right=right)
            else:
                leg_trades, leg_quotes = [trade for trade in trades if left <= trade.key.event_ns < right], quotes
            return window, leg_trades, t.Coverage(left, right, True, gaps, (source,)), note, leg_quotes

        def context(identity, window, note, sizing):
            contexts[(identity.account_id, identity.flip_id, identity.segment_id)] = (window, note, sizing)

        # Every recomputed BUY is a SIM opportunity, independent of real fills,
        # original order metadata, or whether a broker submitted an order.
        line_by_start = {item["start_ns"]: item for item in line_rows}
        for side, buy in flips:
            if side != "BUY":
                continue
            computed = line_by_start[buy.bar_start_ns]
            bar, oracle = computed["original"], computed["oracle"]
            sell = next((boundary for kind, boundary in flips
                         if kind == "SELL" and boundary.available_ns > buy.available_ns), None)
            window, leg_trades, leg_coverage, note, leg_quotes = scoped(buy.bar_start_ns,
                'RECOMPUTED_BUY:' + str(computed['original']['id']))
            watched = True if window is not None and buy.available_ns < window[1] else None
            broker = route["broker"] or "UNKNOWN"
            # Explicit modeled slot, not an inferred broker-account identity.
            account = "SIM_ROUTE:" + broker
            identity = t.FlipIdentity(day, symbol, broker, account, "schwab_1m_v2",
                "RECOMPUTED_BUY:" + str(buy.bar_start_ns), "RECOMPUTED_SEGMENT:" + str(buy.bar_start_ns))
            live = t.LiveFacts(None, (), (), coverage, ("UNKNOWN_ACTUAL_LIVE_ALLOCATION",))
            today_coverage = t.Coverage(buy.available_ns, buy.available_ns, None,
                                      ("UNKNOWN_SUPPLIED_TODAY_ENTRY",), (source,))
            provisional = EpisodeInput(identity, buy, watched, source if watched else None,
                positive(bar["high_price"]) + Decimal("0.01"), 1, sizing_ref, sell, session_end,
                tuple(leg_trades), tuple(leg_quotes), leg_coverage, leg_coverage, (), today_coverage, live, policy)
            if same_sip_policy is not None and 'UNKNOWN_SAME_SIP_NATIVE_SEQUENCE' in leg_coverage.gaps:
                revised = replace(leg_coverage, gaps=tuple(issue for issue in leg_coverage.gaps
                                                         if issue != 'UNKNOWN_SAME_SIP_NATIVE_SEQUENCE'))
                provisional = replace(provisional, tape_coverage=revised, flip_coverage=revised,
                                      same_sip_policy=same_sip_policy)
                tie_identities.add(identity)
            completed_bar = CompletedFlipBar(symbol, buy.bar_start_ns, buy.available_ns,
                positive(bar["high_price"]), positive(bar["close_price"]),
                Decimal(str(oracle["trail"])), oracle["state"], True if watched else None, buy.source)
            # Pure planning consumes no volume. The one-unit probe is not an
            # allocated fill; actual shared allocation occurs only below.
            plan = plan_firstbar_entry(provisional, completed_bar, mode=FirstBarMode.FIRSTBAR,
                                       entry_cutoff=t.Boundary(cutoff_ns, source))
            sizing = size(route, plan.candidate.price if plan.candidate is not None else provisional.trigger)
            sizing["planned_entry_price"] = None if plan.candidate is None else str(plan.candidate.price)
            episode = replace(provisional, quantity=sizing["quantity"] if sizing["quantity"] > 0 else None)
            context(identity, window, note, sizing)
            all_episodes.append(episode)
            episode_origins.append('RECOMPUTED_BUY:' + str(computed['original']['id']))
            firstbar_inputs.append(FirstBarReplayInput(episode, completed_bar, t.Boundary(cutoff_ns, source)))

        # REAL keeps actual quantities/prices and strict physical identity.
        # AGG_ENTRY can reuse its recorded entry price under the approved
        # aggregate assumption, but its SIM quantity comes only from route sizing.
        by_order = defaultdict(list)
        for row in grouped["fills"][symbol]:
            if row["side"] == "buy":
                by_order[(row["account_name"], row["order_id"])].append(row)
        for (account, order_id), fills in by_order.items():
            try:
                for row in fills:
                    actual_fill_identity(row, dict(account_id=row["fill_account_id"],
                        strategy_id=row["fill_strategy_id"], account_name=account,
                        broker=row["broker"], strategy_code="schwab_1m_v2"), symbol)
                actual = [dict(fill_id=row["fill_id"], at=dict(event_ns=nanos(row["filled_at"])),
                               price=str(positive(row["price"])), quantity=integral(row["quantity"])) for row in fills]
                ident = t.FlipIdentity(day, symbol, fills[0]["broker"], account, "schwab_1m_v2",
                                      "RECORDED_ORDER:" + order_id, "RECORDED_ORDER:" + order_id)
                receipt = dict(identity=plain(asdict(ident)), mode="LIVE", entries=actual, exits=[],
                               decision="UNKNOWN_ACTUAL_EXIT_ALLOCATION")
                real_row = canonical_row(receipt, known=False, amount=None,
                    coverage_note="recorded LIVE coverage UNKNOWN: exact exit allocation not proven")
                tables["LIVE:ACTUAL"]["rows"].append(dict(identity=plain(asdict(ident)), row=real_row,
                                                           outcome_id=digest(("LIVE", asdict(ident)))))
                if len(fills) != 1:
                    input_issues.append(symbol + ":UNKNOWN_AGGREGATE_PARTIAL_CLOCK_BINDING")
                    continue
                row = fills[0]
                metadata = (row.get("intent_payload") or {}).get("metadata", {})
                if (not isinstance(metadata, dict)
                        or any(key.startswith("operator_manual_close_") or key == "operator_external_fill_repair"
                               for key in metadata)
                        or str(row.get("reason", "")).startswith("operator_")
                        or (row.get("order_payload") or {}).get("source") == "external_execution"):
                    input_issues.append(symbol + ":UNKNOWN_MANUAL_AGGREGATE_ENTRY")
                    continue
                filled, price = nanos(row["filled_at"]), positive(row["price"])
                window, leg_trades, leg_coverage, note, _ = scoped(filled, 'RECORDED_PLACEMENT:fill:' + row['fill_id'])
                sizing = size(route, price)
                if sizing["quantity"] <= 0:
                    input_issues.append(symbol + ":UNKNOWN_ZERO_SIZED_AGGREGATE")
                    continue
                broker = route["broker"]
                if broker is None or broker != row["broker"]:
                    input_issues.append(symbol + ":UNKNOWN_AGGREGATE_ROUTE_BINDING")
                    continue
                modeled = replace(ident, account_id="SIM_ROUTE:" + broker, broker=broker)
                leg = StudyBuyLeg(modeled.account_id, order_id, str(row["recorded_order_type"]), symbol,
                    nanos(row["submitted_at"]), filled, price, sizing["quantity"], "", "", source)
                baseline_day = BaselineDay(symbol, day, tuple(leg_trades),
                    tuple(boundary for kind, boundary in flips if kind == "SELL"), session_end,
                    leg_coverage, leg_coverage.gaps, source, tuple(flips), tuple(closes))
                if same_sip_policy is not None and "UNKNOWN_SAME_SIP_NATIVE_SEQUENCE" in leg_coverage.gaps:
                    revised = replace(leg_coverage, gaps=tuple(issue for issue in leg_coverage.gaps
                                                             if issue != "UNKNOWN_SAME_SIP_NATIVE_SEQUENCE"))
                    baseline_day = replace(baseline_day, coverage=revised,
                        issues=tuple(issue for issue in baseline_day.issues if issue != "UNKNOWN_SAME_SIP_NATIVE_SEQUENCE"),
                        same_sip_policy=same_sip_policy)
                    tie_identities.add(modeled)
                context(modeled, window, note, sizing)
                aggregate_inputs.append(AggregateBaselineInput((symbol, day, str(filled)),
                    modeled, leg, baseline_day, AGGREGATE_ASSUMPTION))
            except (ValueError, KeyError, TypeError):
                input_issues.append(symbol + ":UNKNOWN_ACTUAL_LIVE_IDENTITY_OR_OPERANDS")

    def add(key, identity, receipt, known, amount, *, order_id=None):
        table = tables[key]
        window, note, sizing = contexts[(identity.account_id, identity.flip_id, identity.segment_id)]
        report, touched = None, None
        if identity in tie_identities:
            from project_mai_tai.atr_simulation.same_sip import report_key

            report = tie_reports.get(report_key(identity, key))
            touched = tie_report_touched(report)
            if touched is True:
                note += '; tie → conservative'
        percent_known = known
        if key == "LIVE:ACTUAL":
            note = "recorded LIVE coverage UNKNOWN: exact exit allocation not proven"
            known, amount = False, None
        if known:
            fills, exits = receipt.get("fills", []), receipt.get("exits", [])
            clocks = [fact["at"]["event_ns"] for fact in (*fills, *exits) if fact.get("at")]
            if (window is None or not clocks
                    or any(not window[0] <= clock < window[1] for clock in clocks)):
                known, amount = False, None
                percent_known = False
                note = "coverage UNKNOWN: modeled entry or exit outside proved envelope"
        note += "; line recomputed from stored bars; actual live flip UNKNOWN"
        sizing = dict(sizing)
        allocated = receipt.get("fills", [])
        if known and sizing["usd_known"] and allocated:
            units = sum(item["quantity"] for item in allocated)
            cost = sum((Decimal(str(item["price"])) * item["quantity"] for item in allocated), Decimal(0))
            if units and int(Decimal(sizing["notional"]) / (cost / units)) != sizing["quantity"]:
                sizing.update(usd_known=False, sizing_status="UNKNOWN_POST_ALLOCATION_ENTRY_PRICE_SIZING",
                              percent_basis="normalized modeled price return; exact notional units unresolved")
        if not sizing["usd_known"]:
            known, amount = False, None
        row = canonical_row(receipt, known=known, amount=amount, coverage_note=note,
                            sizing=sizing, percent_known=percent_known)
        outcome_id = digest((key, asdict(identity)))
        if identity in tie_identities:
            # No missing report is converted to zero touched cases or events.
            receipt = dict(receipt, same_sip_report=plain(report) if report is not None else None)
            if touched is True:
                row['same_sip_policy_label'] = 'tie → conservative'
            tie_conversions.append(dict(outcome_id=outcome_id, identity=asdict(identity), world=key,
                previous_issue='UNKNOWN_SAME_SIP_NATIVE_SEQUENCE', policy=same_sip_policy,
                report=report, actual_tie_actions=touched, remaining_issues=receipt.get('issues', [])))
        engine_receipts[outcome_id] = receipt
        table["rows"].append(dict(identity=plain(asdict(identity)), row=row,
                                  outcome_id=outcome_id))

    if firstbar_inputs:
        stage("FIRSTBAR", "+", len(firstbar_inputs))
        firstbar_engine = FirstBarReplayEngine()
        if cache_models is not None and cache_models.closed_components:
            results, firstbar_reports = cache_models.execute('FIRSTBAR', data,
                tuple(firstbar_inputs), episode_origins, route_packet, line_inputs,
                fill_policy=FirstBarFillPolicy.OBSERVED_PRINT_VOLUME)
            firstbar_engine.same_sip_reports = firstbar_reports
        else:
            cache_arguments = ({} if cache_models is None else cache_models.arguments('FIRSTBAR', data,
                tuple(firstbar_inputs), episode_origins, route_packet, line_inputs))
            results = firstbar_engine.simulate_many(tuple(firstbar_inputs),
                fill_policy=FirstBarFillPolicy.OBSERVED_PRINT_VOLUME, **cache_arguments)
        firstbar_results = results
        if tie_identities:
            tie_reports.update(firstbar_engine.same_sip_reports)
            firstbar_reports = firstbar_engine.same_sip_reports
        for result in results:
            receipt = plain(asdict(result))
            known = result.gross_pnl is not None and not result.issues
            add(result.mode.value, result.identity, receipt, known,
                str(result.gross_pnl) if known else None)
        stage("FIRSTBAR", "-")
    if all_episodes:
        stage("CORE", "+", len(all_episodes))
        core_engine = SimulationEngine()
        if cache_models is not None and cache_models.closed_components:
            results, core_reports = cache_models.execute('CORE', data,
                tuple(all_episodes), episode_origins, route_packet, line_inputs)
            core_engine.same_sip_reports = core_reports
        else:
            cache_arguments = ({} if cache_models is None else cache_models.arguments('CORE', data,
                tuple(all_episodes), episode_origins, route_packet, line_inputs))
            results = core_engine.simulate_many(tuple(all_episodes), **cache_arguments)
        core_results = results
        if tie_identities:
            tie_reports.update(core_engine.same_sip_reports)
            core_reports = core_engine.same_sip_reports
        for result in results:
            if window_view is not None:
                from atrsimlive1_window_census import condition_totals

                window, _, _ = contexts[(result.identity.account_id, result.identity.flip_id, result.identity.segment_id)]
                left, right = (nanos(data["start"]), nanos(data["end"])) if window is None else window[:2]
                result = replace(result, condition_coverage=condition_totals(
                    window_view["metadata"]["conditions"], start_ns=left, end_ns=right,
                    policy=policy, source=source, null_policy=null_policy))
            for receipt in result_rows(result):
                receipt = plain(receipt)
                if receipt["mode"] == "LIVE":
                    continue  # REAL rows above preserve actual fills, never these modeled slots.
                key = receipt["mode"] + ":" + receipt["model"]
                if key not in tables:
                    raise ValueError("existing_engine_returned_unknown_world")
                known = receipt["gross_pnl"] is not None and not receipt.get("issues")
                add(key, result.identity, receipt, known, receipt["gross_pnl"] if known else None)
        stage("CORE", "-")
    if aggregate_inputs:
        stage("AGG", "+", len(aggregate_inputs))
        aggregate_reports = {}
        # Legacy default calls retain the exact previous signature/path.
        results = (simulate_aggregate_cohort(tuple(aggregate_inputs), same_sip_reports=aggregate_reports)
                   if tie_identities else simulate_aggregate_cohort(tuple(aggregate_inputs)))
        tie_reports.update(aggregate_reports)
        for result in results:
            receipt = plain(asdict(result))
            receipt.update(mode="AGG_ENTRY", fills=receipt["position"]["fills"])
            known = result.modeled_gross_pnl is not None and not result.issues
            add("AGG_ENTRY", result.identity, receipt, known,
                str(result.modeled_gross_pnl) if known else None, order_id=result.order_id)
        stage("AGG", "-")
    completed = datetime.now(timezone.utc).isoformat()
    for key, table in tables.items():
        table.update(read_status="UNKNOWN", source_asof=asof.isoformat(),
                     issues=list(dict.fromkeys((*scope_issues, *input_issues))),
                     updated_at=completed if table["rows"] else None,
                     unknown_count=None,  # Unbound watched-flip population is not zero.
                     skipped_count=None)
        if not table["rows"]:
            table["issues"].append("UNKNOWN_NO_COMPUTABLE_WORLD_INPUT")
    producer = dict(producer, source_counts=data["source_counts"],
                    input_refs=data["source_hashes"], snapshot_id=data["snapshot_id"],
                    observed_symbols=data["symbols"], observed_flips=observed_flips,
                    engine_receipts=engine_receipts,
                    supplied_envelopes=envelope_metrics,
                    recomputed_line_inputs=line_inputs, c1_buy_parity=parity_reports,
                    day_routes=route_reports,
                    policy_sha256=POLICY_SHA if policy is not None else None,
                    query_sha256=data["query_sha256"],
                    engine_versions=dict(core=t.CONTRACT_VERSION, firstbar=FIRSTBAR_REPLAY_VERSION,
                        aggregate=BASELINE_VERSION, aggregate_policy=BASELINE_POLICY_VERSION),
                    policy_label="ex-post SIM policy, snapshot retrieved ~10-05, retrieval time UNKNOWN",
                    native_clock="UNKNOWN", source_order="DB serialization; native sequence UNKNOWN")
    producer["input_assumptions"] = ["SIM opportunities from unchanged ATR oracle; no real-entry metadata requirement",
        "SIM units floor recorded-route notional / modeled entry price; REAL quantities unchanged",
        "Missing route uses explicit one-unit normalized price model; units/USD UNKNOWN, not sized gate proof",
        "Modeled account slot SIM_ROUTE is not a native-account ownership claim",
        "Line recomputed from stored bars; DERIVED minute-close clock, original created_at retained; native arrival UNKNOWN",
        "C1 14:46 per-run bar/print/log crosscheck grants only scoped supplied coverage; native UNKNOWN",
        "Missing/zero no-print minute bridges only between immediate independent positive-volume covered neighbors"]
    if window_view is not None:
        producer["working_selection"] = {key: window_view[key] for key in (
            "schema", "purpose", "artifact", "artifact_sha256", "selected_counts", "selected_hashes", "descriptors",
            "full_counts", "metadata_sha256") if key in window_view}
    if same_sip_policy is not None:
        producer['same_sip_policy'] = same_sip_policy
        producer['same_sip_conversions'] = plain(tie_conversions)
    if null_policy is not None:
        producer['stored_null_policy'] = null_policy
    document = dict(schema=SCHEMA, trading_day=day, timezone="America/New_York",
                generated_at=datetime.now(timezone.utc).isoformat(), source_asof=asof.isoformat(),
                producer=plain(producer), native_execution_qualified=False, tables=tables)
    if cache_models is not None:
        if firstbar_inputs:
            cache_models.record('FIRSTBAR', data, firstbar_results, firstbar_reports, document)
        if all_episodes:
            cache_models.record('CORE', data, core_results, core_reports, document)
        document['producer']['incremental'] = plain(dict(version=incremental.binding['version'],
            input_scope=incremental.binding['input_scope'],
            source_seal=incremental.binding['source_seal'],
            current_snapshot_id=data['snapshot_id'], current_source_asof=data['source_asof'],
            current_input_verification=incremental.inputs[data['symbols'][0]],
            operations=[item for item in incremental.audit if item.get('symbol') == data['symbols'][0]],
            cache_is_not_native_or_full_market_qualification=True))
    return document


def combine(documents):
    """One RR snapshot; disjoint symbols, never separate account allocations."""
    if not documents:
        raise ValueError("explicit_today_population_required")
    from copy import deepcopy

    result = deepcopy(documents[0])
    for document in documents[1:]:
        if (document["trading_day"] != result["trading_day"]
                or document["source_asof"] != result["source_asof"]
                or document["producer"]["snapshot_id"] != result["producer"]["snapshot_id"]):
            raise ValueError("same_snapshot_today_components_required")
        if document['producer'].get('stored_null_policy') != result['producer'].get('stored_null_policy'):
            raise ValueError('same_TODAY_null_policy_population_required')
        for key in KEYS:
            left, right = result["tables"][key], document["tables"][key]
            left["rows"].extend(right["rows"])
            left["issues"] = list(dict.fromkeys((*left["issues"], *right["issues"])))
            times = [value for value in (left["updated_at"], right["updated_at"]) if value is not None]
            left["updated_at"] = min(times) if times else None
        for field in ("observed_symbols", "observed_flips", "supplied_envelopes",
                      "recomputed_line_inputs", "c1_buy_parity", "day_routes"):
            result["producer"][field].extend(document["producer"][field])
        result["producer"]["engine_receipts"].update(document["producer"]["engine_receipts"])
        if 'same_sip_conversions' in result['producer']:
            if document['producer'].get('same_sip_policy') != result['producer']['same_sip_policy']:
                raise ValueError('mixed_same_sip_policy_population')
            result['producer']['same_sip_conversions'].extend(document['producer']['same_sip_conversions'])
    result["producer"]["per_symbol_inputs"] = [dict(symbols=doc["producer"]["observed_symbols"],
        source_counts=doc["producer"]["source_counts"], input_refs=doc["producer"]["input_refs"])
        for doc in documents]
    if any('working_selection' in doc['producer'] for doc in documents):
        if not all('working_selection' in doc['producer'] for doc in documents):
            raise ValueError('mixed_window_selection_population')
        result['producer']['per_symbol_working_selection'] = [doc['producer']['working_selection'] for doc in documents]
    if any('incremental' in doc['producer'] for doc in documents):
        if not all('incremental' in doc['producer'] for doc in documents):
            raise ValueError('same_incremental_version_whole_roster_required')
        result['producer']['per_symbol_incremental'] = [doc['producer']['incremental'] for doc in documents]
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    from atrsimlive1_line_route import parity_summary

    summary = parity_summary(result["trading_day"], result["producer"]["c1_buy_parity"])
    result["producer"]["c1_buy_parity_summary"] = summary
    for table in result["tables"].values():
        table["issues"].append("C1_REPORTED_BUY_PARITY_" + summary["status"]
                               + ":mismatches=" + str(summary["mismatches"])
                               + ":unresolved=" + str(summary["unresolved"]))
    return result
