"""Existing boot-helper timeline arm; no fresh stores or database factory.

Reviewed L11 FILE envelopes and owner cadence, C1 16:39 boot-only retiming.
No provider requests, native header rewriting or readiness assignments.
"""

import asyncio
from copy import deepcopy
from contextlib import ExitStack
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tools.l12_recorded_boot_timeline import (
    FILE_PORT_SHA, OLD_LOG_SHA, SCOPE, C11810SeedTracker, RecordedSessionMap)


FILE_PORT = Path("/private/tmp/oct23-l11-trim-replay-e396-20261023/tools/keepwatch_file_history_reader.py")
PUBLISH_CONTROL_SCOPE = "ACTUAL_HOLD_CHECK_AT_MODELED_PUBLISH_BOUNDARY"
PUBLISH_INTERVAL_NS = 5_000_000_000


def publish_control_steps(steps, *, start_ns, maximum_boundaries, ruling_sha256):
    """Insert phase-zero lifecycle steps; never repair/reorder recorded inputs."""
    if (type(start_ns) is not int or start_ns <= 0 or not steps
            or type(maximum_boundaries) is not int or not 0 < maximum_boundaries <= 100000
            or type(ruling_sha256) is not str or len(ruling_sha256) != 64
            or ruling_sha256 == "0" * 64 or any(c not in "0123456789abcdef" for c in ruling_sha256)):
        raise ValueError("explicit_bounded_C12309_publish_control_operands_required")
    previous, observed = start_ns, False
    for step in steps:
        stamp = step["clock_ns"]
        if type(stamp) is not int or stamp < previous or step["kind"] == "state_publish_control":
            raise ValueError("publish_control_cannot_repair_or_duplicate_source_timeline")
        if stamp != previous:
            observed = False
        if observed and step["kind"] in {"membership", "scanner_delta", "history_poll"}:
            raise ValueError("same_clock_mutation_after_observation_refused_not_reordered")
        observed = observed or step["kind"].startswith("observe_")
        previous = stamp
    count = (steps[-1]["clock_ns"] - start_ns) // PUBLISH_INTERVAL_NS + 1
    if count > maximum_boundaries or len(steps) + count > 100000:
        raise ValueError("publish_control_predeclared_budget_exceeded_no_growth")
    result, index = [], 0
    for clock_ns in range(start_ns, steps[-1]["clock_ns"] + 1, PUBLISH_INTERVAL_NS):
        while index < len(steps) and steps[index]["clock_ns"] < clock_ns:
            result.append(steps[index])
            index += 1
        while (index < len(steps) and steps[index]["clock_ns"] == clock_ns
                and not steps[index]["kind"].startswith("observe_")):
            result.append(steps[index])
            index += 1
        result.append(dict(kind="state_publish_control", clock_ns=clock_ns,
            original_clock_ns=None, watchlist=None, raw_ref="C12309_PHASE0_MODELED_NOT_RECORDED",
            raw_sha256=ruling_sha256))
    result.extend(steps[index:])
    return result


class PublishControl:
    """One real hold check per sealed boundary, no calls from observations."""

    def __init__(self, steps, *, observe_boot_retry=False):
        if type(observe_boot_retry) is not bool:
            raise ValueError("explicit_boot_retry_observation_mode_required")
        self.observe_boot_retry = observe_boot_retry
        self.boundaries = tuple(deepcopy(step) for step in steps if step["kind"] == "state_publish_control")
        self.clocks = tuple(step["clock_ns"] for step in self.boundaries)
        self.records, self.failed = [], False

    @staticmethod
    def state(bot, *, observe_boot_retry=False):
        result = dict(restoration_complete=bot._boot_state_restoration_complete,
            entries_held=bot.strategy._entries_held,
            selected=sorted(bot._watchlist), scanner_selected=sorted(bot._boot_scanner_selected),
            exclusions_readable=bot._boot_exclusion_sources_readable,
            rest_warmed=sorted(bot._rest_warmup_done),
            timeout_released=sorted(bot._boot_warmup_timeout_released))
        if observe_boot_retry:
            method = bot._keepwatch_boot_retry_state
            if (getattr(method, "__func__", None) is not type(bot)._keepwatch_boot_retry_state):
                raise ValueError("actual_unwrapped_boot_retry_observer_required")
            result["boot_retry"] = deepcopy(method())
        return result

    def invoke(self, bot, service_class, step, *, clock_ns, copy_guard):
        index = len(self.records)
        if (self.failed or step["kind"] != "state_publish_control" or index >= len(self.clocks)
                or step["clock_ns"] != clock_ns or self.clocks[index] != clock_ns
                or step != self.boundaries[index]
                or type(bot) is not service_class
                or getattr(bot._cw_boot_hold_check, "__func__", None) is not service_class._cw_boot_hold_check):
            self.failed = True
            raise ValueError("exact_once_actual_holdcheck_publish_boundary_required")
        row = dict(clock_ns=clock_ns, scope=PUBLISH_CONTROL_SCOPE,
            full_publisher="UNEXERCISED", monotonic_warmup_timeout="UNKNOWN",
            corrected_APP_carry="PENDING_SEPARATE_REVIEW_NOT_0C7_OR_F_QUALIFICATION")
        try:
            row["before"] = self.state(bot, observe_boot_retry=self.observe_boot_retry)
            if copy_guard() is not True:
                raise ValueError("copy_fence_lost_before_publish_control")
            bot._cw_boot_hold_check()
            row["after"] = self.state(bot, observe_boot_retry=self.observe_boot_retry)
            if copy_guard() is not True:
                raise ValueError("copy_fence_lost_after_publish_control")
            row["status"] = "ACTUAL_CONTROL_STEP_RETURNED_NOT_BOOT_QUALIFICATION"
        except BaseException as exc:
            self.failed = True
            row.update(status="FAILED_NO_RETRY", exception_type=type(exc).__name__)
            raise
        finally:
            self.records.append(row)

    def report(self):
        return dict(scope=PUBLISH_CONTROL_SCOPE, expected=len(self.clocks), actual=len(self.records),
            complete=not self.failed and len(self.records) == len(self.clocks), records=self.records,
            full_publisher="UNEXERCISED", monotonic_warmup_timeout="UNKNOWN", permission_granted=False)


def retain_first_flip(first_by_name, observation):
    """Keep actual first observed flip even when no draft is returned.

    This is a diagnostic observation, not native/OMS admission. Never replace an
    earlier refusal with a later successful draft or infer a row's age from an
    opportunity ID that merely resembles a timestamp.
    """
    name = observation["symbol"]
    if name not in first_by_name:
        raise ValueError("first_BUY_outside_recorded_population")
    if observation.get("flip") != "BUY":
        return
    if not isinstance(first_by_name[name], str):
        return
    first_by_name[name] = dict(deepcopy(observation), scope=SCOPE,
        native_admission="UNKNOWN_NOT_INFERRED_FROM_STRATEGY_RETURN",
        durable_row_causal_attribution="UNKNOWN_NO_EXACT_PK_AND_CREATED_AT_IN_GUARD_RETURN",
        permission_granted=False)


class CaseClock:
    def __init__(self, case):
        self.case = case
        self.now_ns = case.now_ms * 1000000

    def advance(self, value):
        if type(value) is not int or value < self.now_ns:
            raise ValueError("boot_model_clock_rewind_refused")
        self.now_ns = value
        self.case.now_ms = value // 1000000


def reviewed_steps(events, mapping, names):
    """Recorded causal ordering; neither sort nor skip malformed source events."""
    if (type(mapping) is not RecordedSessionMap or type(events) is not list
            or not events or len(events) > 100000):
        raise ValueError("finite_recorded_timeline_required")
    result, previous = [], None
    for event in events:
        if type(event) is not dict or set(event) != {"kind", "clock_ns", "watchlist", "raw_ref", "raw_sha256"}:
            raise ValueError("exact_recorded_timeline_event_shape_required")
        kind = event["kind"]
        if kind not in {"membership", "scanner_delta", "history_poll", "observe_0700", "observe_end", "observe_seed"}:
            raise ValueError("unreviewed_timeline_event_kind")
        if (type(event["raw_ref"]) is not str or not event["raw_ref"]
                or type(event["raw_sha256"]) is not str or len(event["raw_sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in event["raw_sha256"])):
            raise ValueError("recorded_event_source_reference_required")
        members = event["watchlist"]
        if kind in {"membership", "scanner_delta"}:
            if (type(members) is not list or len(members) != len(set(members))
                    or any(type(name) is not str or name not in names for name in members)):
                raise ValueError("membership_outside_declared_population")
            if kind == "scanner_delta" and len(members) != 1:
                raise ValueError("one_recorded_scanner_delta_symbol_required")
        elif members is not None:
            raise ValueError("nonmembership_event_cannot_mutate_watchlist")
        stamp = mapping.market_ns(event["clock_ns"])
        if previous is not None and stamp < previous:
            raise ValueError("recorded_timeline_not_causal_no_sort_repair")
        previous = stamp
        result.append(dict(event, original_clock_ns=event["clock_ns"], clock_ns=stamp))
    return result


class RecordedBootDriver:
    """Finite real-callback arm; caller supplies Main-reviewed input operands.

    Any missing provider rows remain actual reader refusal, not empty-session
    proof. The separate existing helper still validates copy/factory/source.
    """

    def __init__(self, *, prepared, mapping, events, maximum_owner_actions, confirmation_tracker=None,
                 initial_carried_review=None, maximum_causal_observations=None,
                 maximum_publish_boundaries=None, publish_control_ruling_sha256=None,
                 boot_deferral_review=None):
        if (type(mapping) is not RecordedSessionMap or type(prepared) is not dict
                or prepared.get("mapping") != mapping.receipt()
                or prepared.get("permission_granted") is not False):
            raise ValueError("reviewed_prepared_boot_mapping_required")
        self.prepared, self.mapping = prepared, mapping
        self.names = tuple(prepared["population"]["names"])
        self.steps = reviewed_steps(events, mapping, self.names)
        self.confirmation_tracker = confirmation_tracker
        if confirmation_tracker is None:
            if any(step["kind"] == "scanner_delta" for step in self.steps):
                raise ValueError("fleet_scanner_delta_requires_explicit_C11720_mode")
            expected_0700 = mapping.market_ns(1792753200000 * 1000000)
            marked = [step for step in self.steps if step["kind"] == "observe_0700"]
            if len(marked) != 1 or marked[0]["clock_ns"] != expected_0700:
                raise ValueError("exactly_one_actual_0700_seed_observation_required")
        elif (type(confirmation_tracker) is not C11810SeedTracker
                or confirmation_tracker.fleet["mapping"] != mapping.receipt()
                or tuple(confirmation_tracker.fleet["names"]) != self.names
                or confirmation_tracker.candidate.get("raw_log_sha256") != OLD_LOG_SHA
                or confirmation_tracker.candidate.get("record_origin") != "ACTUAL_FIXED_SHA_AND_LINE_COUNT_DELIVERY"):
            raise ValueError("explicit_C11810_Friday18_window_tracker_required_no_superseded_FADE_boundary")
        if type(maximum_owner_actions) is not int or maximum_owner_actions <= 0:
            raise ValueError("explicit_finite_owner_action_budget_required")
        self.maximum_owner_actions = maximum_owner_actions
        if maximum_causal_observations is not None and (
                type(maximum_causal_observations) is not int or maximum_causal_observations <= 0):
            raise ValueError("explicit_positive_causal_observation_budget_required")
        self.maximum_causal_observations = maximum_causal_observations
        self.consumed = False
        self.boot_deferral_review = None
        if boot_deferral_review is not None:
            from tools.l12_boot_owner_clock import require_boot_deferral_review
            require_boot_deferral_review(boot_deferral_review)
            if maximum_publish_boundaries is None:
                raise ValueError("4f_elapsed_clock_requires_actual_publish_control_boundaries")
            self.boot_deferral_review = deepcopy(boot_deferral_review)
        self.publish_control = None
        if maximum_publish_boundaries is not None:
            self.steps = publish_control_steps(self.steps,
                start_ns=mapping.market_ms(1792742400000) * 1000000,
                maximum_boundaries=maximum_publish_boundaries,
                ruling_sha256=publish_control_ruling_sha256)
            self.publish_control = PublishControl(self.steps,
                observe_boot_retry=self.boot_deferral_review is not None)
        elif publish_control_ruling_sha256 is not None:
            raise ValueError("publish_control_ruling_requires_predeclared_boundary_budget")
        self.initial_carried_review = None
        if initial_carried_review is not None:
            from tools.l12_recorded_boot_timeline import C11810_CARRIED, c11810_candidate_sha256, _digest
            fields = {"version", "members", "candidate_sha256", "ruling_sha256", "review_sha256", "scope"}
            if (type(initial_carried_review) is not dict or set(initial_carried_review) != fields
                    or initial_carried_review["version"] != "C11810_CONTROLLED_CARRIED_BOOT_MEMBERSHIP_V1"
                    or initial_carried_review["members"] != sorted(C11810_CARRIED)
                    or type(confirmation_tracker) is not C11810SeedTracker
                    or initial_carried_review["candidate_sha256"] != c11810_candidate_sha256(confirmation_tracker.candidate)
                    or initial_carried_review["scope"] != "RECONSTRUCTED_BOOT_MEMBERSHIP_NOT_ORIGINAL_CONFIRM_OR_READINESS"):
                raise ValueError("explicit_reviewed_seven_carried_boot_membership_required")
            for key in ("ruling_sha256", "review_sha256"):
                _digest(initial_carried_review[key])
                if initial_carried_review[key] == "0" * 64:
                    raise ValueError("actual_carried_boot_ruling_and_independent_review_required")
            self.initial_carried_review = deepcopy(initial_carried_review)

    async def run(self, case, *, copy_guard):
        from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
        from project_mai_tai.market_data import keepwatch_history as reader_module
        from project_mai_tai.services import schwab_1m_v2_bot as service
        from tools.l12_boot_owner_clock import (
            OwnerClock, scheduled, boot_deferral_clock, require_boot_deferral_review)
        from tools.l12_service_boot_replay import observe_combined_seed_state
        from tools.l12_causal_short_observer import CausalShortObserver
        from project_mai_tai import brokerdecides1_entry as l12_entry

        if self.consumed:
            raise ValueError("recorded_boot_arm_consumed_no_retry_or_tracker_reuse")
        self.consumed = True
        if self.confirmation_tracker is not None:
            if getattr(self.confirmation_tracker, "_l12_arm_consumed", False):
                raise ValueError("sequential_arm_requires_fresh_confirmation_tracker")
            self.confirmation_tracker._l12_arm_consumed = True
        bot = case.bot
        if type(bot) is not service.SchwabV2BotService or copy_guard() is not True:
            raise ValueError("actual_combined_service_and_held_copy_required")
        if self.boot_deferral_review is not None:
            require_boot_deferral_review(self.boot_deferral_review)
            component = self.boot_deferral_review["app_component"]
            if sha256(Path(service.__file__).read_bytes()).hexdigest() != component["after_sha256"]:
                raise ValueError("elapsed_clock_requires_literal_4f_service_not_old_F_or_descendant")
            if copy_guard() is not True:
                raise ValueError("copy_source_fence_lost_during_4f_clock_binding")
        expected_start = self.mapping.market_ms(1792742400000)
        if case.now_ms != expected_start or bot._keepwatch_bar_tasks:
            raise ValueError("actual_0400_unstarted_owners_required")
        if self.steps[0]["clock_ns"] < expected_start * 1000000:
            raise ValueError("timeline_cannot_rewind_before_actual_boot")
        if not bot._keepwatch_enabled:
            raise ValueError("KEEPWATCH_ON_required")
        if self.publish_control is not None and service.STATE_PUBLISH_INTERVAL_SECONDS != 5:
            raise ValueError("actual_production_publish_interval_changed_STOP")
        if self.confirmation_tracker is not None:
            interval = Decimal(str(bot.settings.strategy_schwab_1m_v2_bar_poll_interval_seconds)) * 1000
            if interval != self.prepared["poll_interval_ms"]:
                raise ValueError("fleet_external_poll_schedule_and_actual_Settings_cadence_disagree")
        if sha256(FILE_PORT.read_bytes()).hexdigest() != FILE_PORT_SHA:
            raise ValueError("recorded_FILE_adapter_source_changed")
        spec = importlib.util.spec_from_file_location("_l12_reviewed_recorded_FILE", FILE_PORT)
        port_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(port_module)
        context = ContextVar("l12_boot_owned_FILE_context", default=None)

        class TaskFileHistoryReader(port_module.FileHistoryReader):
            @property
            def context(self):
                return context.get()

            @context.setter
            def context(self, value):
                context.set(value)

        clock, requests, owner_records, samples = CaseClock(case), [], [], []
        ports = {name: TaskFileHistoryReader(bot, name, expected_start,
            self.prepared["histories"][name], clock, reconstruct_availability=True)
            for name in self.names if name in self.prepared["histories"]}
        original_read = bot._read_keepwatch_history_owned
        original_rest, original_massive = bot.rest_client, bot._keepwatch_history_client
        original_evaluate = bot.strategy._evaluate_completed_bar
        original_update = bot.strategy._update_atr_state
        original_admission = bot.strategy._strict_first_rest_admitted
        first_buy = {name: "UNKNOWN_NO_BUY_FLIP_OBSERVED" for name in self.names}
        reasons = []
        original_reason = bot.strategy._resting_return_reason
        evaluation_context = ContextVar("l12_boot_completed_bar_observation", default=None)
        causal = (CausalShortObserver(bot, names=self.names, clock_ns=lambda: clock.now_ns,
            maximum_records=self.maximum_causal_observations,
            active_windows=(self.confirmation_tracker.active if self.confirmation_tracker is not None
                else lambda symbol, clock_ns: ())) if self.maximum_causal_observations is not None else None)
        file_lock = asyncio.Lock()

        async def read(name, anchor, through):
            if name not in ports:
                raise ValueError("recorded_provider_history_UNKNOWN_for_symbol")
            if anchor != expected_start:
                raise ValueError("foreign_recorded_session_anchor")
            async with file_lock:
                row = dict(symbol=name, anchor_ms=anchor, through_ms=through,
                    request_clock_ns=clock.now_ns, scope=SCOPE, original_HTTP="UNKNOWN")
                if self.confirmation_tracker is not None:
                    row.update(original_provider_adjustment="UNKNOWN_NOT_EXPORTED_IN_FRIDAY_CSV",
                        FILE_adjusted_header="CONTROLLED_TRUE_PARSER_CONTRACT_NOT_ORIGINAL_METADATA",
                        legacy_adapter_annotations="NOT_FLEET_SOURCE_ATTESTATIONS")
                requests.append(row)
                token = context.set(row)
                bot.rest_client, bot._keepwatch_history_client = ports[name].schwab, ports[name].massive
                try:
                    with patch.object(reader_module, "time", SimpleNamespace(time_ns=lambda: clock.now_ns)):
                        value = await original_read(name, anchor, through)
                    row.update(started_ns=value.started_ns, completed_ns=value.completed_ns,
                        massive_sha256=value.massive_sha256, schwab_sha256=value.schwab_sha256)
                    return value
                finally:
                    bot.rest_client, bot._keepwatch_history_client = original_rest, original_massive
                    context.reset(token)

        def reason(state, value, *, slot="first"):
            result = original_reason(state, value, slot=slot)
            observed = dict(clock_ns=clock.now_ns, symbol=getattr(state, "symbol", None),
                reason=value, slot=slot)
            reasons.append(observed)
            if self.confirmation_tracker is not None:
                self.confirmation_tracker.observe_return_reason(symbol=state.symbol, reason=value,
                    clock_ns=clock.now_ns,
                    closed_bar_ms=state.bars[-1].timestamp_ms if state.bars else None)
            current = evaluation_context.get()
            if current is not None and current["symbol"] == observed["symbol"]:
                current["actual_return_reasons"].append(observed)
            return result

        def update(state, *args, **kwargs):
            before = causal._state(state.symbol) if causal is not None else None
            result = original_update(state, *args, **kwargs)
            current = evaluation_context.get()
            if causal is not None:
                causal.signal(state, result, before=before, context=current,
                    state_only=kwargs.get("state_only", False))
            if (current is not None and current["symbol"] == state.symbol
                    and isinstance(result, dict) and result.get("flip") == "BUY"):
                current.update(flip="BUY", signal_source="ACTUAL_UPDATE_ATR_STATE_RETURN",
                    state_only=kwargs.get("state_only", False))
            return result

        def admission(state, *, slot):
            current = evaluation_context.get()
            scoped = current is not None and current["symbol"] == state.symbol
            had_slot = scoped and "actual_strategy_slot" in current
            previous_slot = current.get("actual_strategy_slot") if scoped else None
            if scoped:
                current["actual_strategy_slot"] = slot
            try:
                result = original_admission(state, slot=slot)
            finally:
                if scoped:
                    if had_slot:
                        current["actual_strategy_slot"] = previous_slot
                    else:
                        current.pop("actual_strategy_slot", None)
            if current is not None and current["symbol"] == state.symbol:
                current["actual_strategy_admissions"].append(dict(allowed=result, slot=slot,
                    scope="ACTUAL_STRATEGY_RETURN_NOT_NATIVE_OR_OMS_ADMISSION"))
            return result

        def evaluate(*args, **kwargs):
            state = args[0] if args else kwargs["state"]
            observed = dict(symbol=state.symbol, clock_ns=clock.now_ns, flip=None,
                actual_return_reasons=[], actual_strategy_admissions=[], actual_l12_decisions=[], actual_causal_signals=[],
                flip_owner_opportunity_id=state.flip_owner_opportunity_id,
                retry_segment_id=state.retry_one_segment_id,
                bar_ms=state.bars[-1].timestamp_ms if state.bars else None,
                entries_held=bot.strategy._entries_held)
            supplied = kwargs.get("restored_signal")
            if (kwargs.get("is_new_bar") is True and kwargs.get("restored") is True
                    and isinstance(supplied, dict) and supplied.get("flip") == "BUY"):
                observed.update(flip="BUY", signal_source="ACTUAL_RESTORED_SIGNAL_ARGUMENT")
                observed["actual_restored_signal_lineage"] = {key: supplied.get(key) for key in (
                    "decision_prev_bar_ts", "decision_cur_bar_ts", "decision_short_segment_id", "observation_phase")}
                if causal is not None:
                    causal.signal(state, supplied, before=causal._state(state.symbol), context=observed,
                        state_only=False, origin="ACTUAL_RESTORED_SIGNAL_ARGUMENT")
            token = evaluation_context.set(observed)
            primary = None
            try:
                value = original_evaluate(*args, **kwargs)
                observed["returned_BUY_draft"] = bool(value is not None
                    and value.side == "buy" and value.intent_type == "open")
                observed["returned_draft_reason"] = getattr(value, "reason", None)
                observed["status"] = ("ACTUAL_BUY_DRAFT_NOT_WIRE_OR_NATIVE_ADMISSION"
                    if observed["returned_BUY_draft"] else "NO_BUY_DRAFT_ACTUAL_GUARD_REASONS_OR_UNKNOWN")
                return value
            except BaseException as exc:
                primary = exc
                observed.update(status="ACTUAL_EVALUATION_RAISED_NOT_REFUSAL_OR_PASS",
                    exception_type=type(exc).__name__)
                raise
            finally:
                evaluation_context.reset(token)
                # Unknown subscription-only names are not admitted by inventing
                # denominator membership. Source/runtime must review them.
                if observed["flip"] == "BUY":
                    try:
                        retain_first_flip(first_buy, observed)
                        if causal is not None:
                            causal.evaluation(observed)
                    except BaseException as exc:
                        if primary is None:
                            raise
                        primary.add_note("first_flip_observer_failure_type=" + type(exc).__name__)

        class WallClock(datetime):
            @classmethod
            def now(cls, tz=None):
                seconds, nanos = divmod(clock.now_ns, 1000000000)
                value = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(seconds=seconds, microseconds=nanos // 1000)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

        owner = OwnerClock(bot, service, clock, self.names, owner_records,
            maximum_actions=self.maximum_owner_actions, before_action=lambda event: None)
        primary = None
        boot_clock = None
        try:
            with ExitStack() as stack:
                if self.boot_deferral_review is not None:
                    boot_clock = stack.enter_context(boot_deferral_clock(bot, clock))
                stack.enter_context(owner.bound())
                stack.enter_context(patch.object(bot, "_read_keepwatch_history_owned", read))
                stack.enter_context(patch.object(bot.strategy, "_evaluate_completed_bar", evaluate))
                stack.enter_context(patch.object(bot.strategy, "_update_atr_state", update))
                stack.enter_context(patch.object(bot.strategy, "_strict_first_rest_admitted", admission))
                stack.enter_context(patch.object(bot.strategy, "_resting_return_reason", reason))
                if causal is not None:
                    stack.enter_context(causal.bound(l12_entry, evaluation_context=evaluation_context))
                stack.enter_context(patch.object(service, "datetime", WallClock))
                carried_observation = None
                if self.initial_carried_review is not None:
                    if copy_guard() is not True:
                        raise ValueError("copy_writer_fence_lost_before_carried_boot_callback")
                    before_members = sorted(bot._watchlist)
                    event = StrategyStateSnapshotEvent(source_service="C11810_RECONSTRUCTED_CARRIED_BOOT_NOT_ORIGINAL_CONFIRM",
                        produced_at=WallClock.now(UTC), payload=StrategyStateSnapshotPayload(
                            watchlist=sorted(set(bot._watchlist) | set(self.initial_carried_review["members"]))))
                    await bot._apply_strategy_state_event_async({"data": event.model_dump_json()},
                        max_watchlist=int(bot.settings.strategy_schwab_1m_v2_max_watchlist_size))
                    carried_observation = dict(review=self.initial_carried_review,
                        before=before_members, after=sorted(bot._watchlist), clock_ns=clock.now_ns,
                        source="REAL_CALLBACK_RECONSTRUCTED_MEMBERSHIP_NOT_SEED_OR_READY_PROOF")
                async for step in scheduled(self.steps, clock, owner):
                    if copy_guard() is not True:
                        raise ValueError("copy_writer_fence_lost_during_timeline")
                    if step["kind"] in {"membership", "scanner_delta"}:
                        members = step["watchlist"]
                        if step["kind"] == "scanner_delta":
                            known = self.prepared["scanner_records_by_ref"].get(step["raw_ref"])
                            if (known is None or known["mapped_ns"] != step["clock_ns"]
                                    or known["symbol"] != members[0]
                                    or known["raw_sha256"] != step["raw_sha256"]):
                                raise ValueError("scanner_delta_not_bound_to_exact_record")
                            # Preserve the actual restored startup population.
                            # A FADE cannot manufacture a preceding CONFIRM.
                            members = set(bot._watchlist)
                            if known["event_type"] == "CONFIRM":
                                members.add(known["symbol"])
                            else:
                                members.discard(known["symbol"])
                            members = sorted(members)
                        event = StrategyStateSnapshotEvent(source_service="RECONSTRUCTED_RETIMED_RECORDED_SCANNER_BOOT_ONLY",
                            produced_at=WallClock.now(UTC), payload=StrategyStateSnapshotPayload(watchlist=members))
                        await bot._apply_strategy_state_event_async({"data": event.model_dump_json()},
                            max_watchlist=int(bot.settings.strategy_schwab_1m_v2_max_watchlist_size))
                    elif step["kind"] == "history_poll":
                        await bot._keepwatch_history_pass()
                        await owner.settle()
                        if bot._line_dirty:
                            await bot._line_restoration_pass()
                    elif step["kind"] == "state_publish_control":
                        self.publish_control.invoke(bot, service.SchwabV2BotService, step,
                            clock_ns=clock.now_ns, copy_guard=copy_guard)
                    if self.confirmation_tracker is not None or step["kind"].startswith("observe_"):
                        sample = observe_combined_seed_state(case, causal_observer=causal)
                        sample["observed_clock_ns"] = clock.now_ns
                        if self.confirmation_tracker is not None:
                            self.confirmation_tracker.observe(sample)
                        samples.append(dict(sample, event=step,
                            declared_recorded_population=list(self.names),
                            declared_but_unobserved=sorted(set(self.names) - set(sample["watched"])),
                            missing_recorded_chronology=self.prepared["population"]["missing_chronology"],
                            missing_recorded_history=self.prepared["missing_history"]))
                await bot._stop_keepwatch_bar_owners()
        except BaseException as exc:
            primary = exc
            raise
        finally:
            try:
                await bot._stop_keepwatch_bar_owners()
            except BaseException as exc:
                if primary is None:
                    raise
                primary.add_note("timeline_owner_cleanup_failure_type=" + type(exc).__name__)
        return dict(status="RECORDED_BOOT_CALLBACKS_EXECUTED_NOT_COMPLETE_GATE", mapping=self.mapping.receipt(),
            boot_deferral_clock=(dict(boot_clock, review=deepcopy(self.boot_deferral_review))
                if boot_clock is not None else dict(status="UNBOUND_REAL_CLOCK_UNCHANGED")),
            publish_control=(self.publish_control.report() if self.publish_control is not None else
                dict(scope=PUBLISH_CONTROL_SCOPE, status="UNBOUND_NOT_C12309_QUALIFIED")),
            initial_carried_membership=carried_observation,
            input_population=self.prepared["population"], samples=samples, history_requests=requests,
            per_confirm_seed=self.confirmation_tracker.report() if self.confirmation_tracker is not None else None,
            first_BUY=first_buy, actual_return_reasons=reasons, owner_records=owner_records,
            C12149_causal_SHORT=(causal.report(self.confirmation_tracker.windows
                if self.confirmation_tracker is not None else []) if causal is not None else
                dict(status="UNKNOWN_CAUSAL_OBSERVATION_BUDGET_UNBOUND_NOT_QUALIFIED", permission_granted=False)),
            old_session_entry_blocks="UNKNOWN_PENDING_EXACT_DURABLE_REASON_ATTRIBUTION",
            MANSTOP_0400="UNKNOWN_PENDING_ACTUAL_ROLL_OBSERVATION",
            Mirror4="UNKNOWN_UNLESS_MAIN_COMMIT_RECEIPT_BOUND", permission_granted=False,
            native_facts="UNKNOWN_NO_RETIMED_NATIVE_HEADERS")


def require_4f_boot_driver(driver):
    """No silent real-clock fallback for the new literal4f execution profile."""
    from tools.l12_boot_owner_clock import require_boot_deferral_review

    if (type(driver) is not RecordedBootDriver or driver.boot_deferral_review is None
            or type(driver.publish_control) is not PublishControl
            or driver.publish_control.observe_boot_retry is not True):
        raise ValueError("literal_4f_requires_reviewed_existing_driver_elapsed_clock")
    require_boot_deferral_review(driver.boot_deferral_review)
