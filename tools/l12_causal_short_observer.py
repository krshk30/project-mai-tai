"""C12149 read-only, delegate-once observations inside the existing boot driver.

No reseeding, guard re-evaluation, strategy writes, native header changes or IO.
Observations do not turn a CONTROLLED or UNKNOWN decision into permission.
"""

from copy import deepcopy
from contextlib import contextmanager, ExitStack
from unittest.mock import patch


class CausalShortObserver:
    def __init__(self, bot, *, names, clock_ns, maximum_records, active_windows):
        if (type(maximum_records) is not int or maximum_records <= 0
                or not callable(clock_ns) or not callable(active_windows)
                or not names or len(names) != len(set(names))):
            raise ValueError("explicit_finite_causal_observer_scope_required")
        self.bot, self.strategy = bot, bot.strategy
        self.names, self.clock_ns, self.maximum_records = tuple(names), clock_ns, maximum_records
        self.active_windows, self.records = active_windows, []

    def _base(self, symbol, kind):
        clock = self.clock_ns()
        if type(clock) is not int or clock <= 0:
            raise ValueError("actual_causal_model_clock_required")
        windows = list(self.active_windows(symbol, clock)) if symbol in self.names else []
        return dict(kind=kind, symbol=symbol, clock_ns=clock,
            actual_strategy_clock_ms=self.strategy._now_ms(),
            window_ids=[w["event_id"] for w in windows],
            scope="DECLARED_FLEET" if symbol in self.names else "OUTSIDE_DECLARED_FLEET_CONTEXT_NOT_DENOMINATOR",
            native_permission="UNKNOWN_NOT_INFERRED", permission_granted=False)

    def _append(self, row):
        if len(self.records) >= self.maximum_records:
            raise ValueError("causal_observation_budget_exceeded_INCOMPLETE_STOP")
        row["observation_id"] = len(self.records)
        self.records.append(row)
        return row

    def _state(self, symbol):
        from project_mai_tai.brokerdecides1_entry import line_segment

        state = self.strategy._symbol_states.get(symbol.upper())
        fields = ("atr_state", "atr_short_flip_bar_ts", "slotclear_fresh_buy_bar_ms",
            "slotclear_fresh_buy_segment_id", "flip_owner_opportunity_id", "fanout_segment_id",
            "retry_one_segment_id", "retry_one_closes_in_segment", "retry_one_budget_readable",
            "flip_owner_evidence_readable", "flip_owner_evidence_at_ms", "flip_owner_phase")
        result = {name: getattr(state, name, None) for name in fields}
        result["last_state_bar_ms"] = state.bars[-1].timestamp_ms if state is not None and state.bars else None
        required = ("atr_state", "atr_short_flip_bar_ts", "slotclear_fresh_buy_bar_ms", "slotclear_fresh_buy_segment_id", "bars")
        result["line_segment"] = (line_segment(state)
            if state is not None and all(hasattr(state, name) for name in required) else None)
        result["state_present"] = state is not None
        return result

    def _published(self, symbol):
        published = self.bot._line_published.get(symbol)
        if published is None:
            return dict(status="NO_PUBLISHED_PREFIX_OBSERVED")
        request, snapshot = published.request, published.snapshot
        indicator = dict(snapshot.indicator)
        return dict(status="ACTUAL_EXISTING_PUBLISHED_PREFIX", epoch=request.epoch,
            revision=request.revision, anchor_ms=request.anchor_ms, current_bar_ms=request.current_bar_ms,
            prefix_count=len(request.bars), first_prefix_bar_ms=(request.bars[0].timestamp_ms if request.bars else None),
            last_prefix_bar_ms=(request.bars[-1].timestamp_ms if request.bars else None),
            indicator_state=indicator.get("atr_state"), indicator_SHORT_id=indicator.get("atr_short_flip_bar_ts"))

    def sample(self, symbol):
        return dict(self._base(symbol, "SEED_SAMPLE_CAUSAL_LINE"), state=self._state(symbol),
            published=self._published(symbol), observed_only=True)

    @contextmanager
    def bound(self, entry_module, *, evaluation_context):
        """Exactly the same scoped installer used by the actual driver."""
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.bot, "_rebuild_session_line",
                self.wrap_rebuild(self.bot._rebuild_session_line)))
            stack.enter_context(patch.object(entry_module, "entry_decision",
                self.wrap_entry_decision(entry_module.entry_decision, evaluation_context=evaluation_context)))
            yield self

    def _record_error(self, row, primary):
        try:
            row.update(status="ORIGINAL_CALL_RAISED_NOT_REFUSAL_OR_PASS", exception_type=type(primary).__name__)
            self._append(row)
        except BaseException as observer_failure:
            primary.add_note("causal_observer_failure_type=" + type(observer_failure).__name__)

    def wrap_entry_decision(self, original, *, evaluation_context):
        def entry(strategy, symbol):
            if strategy is not self.strategy:
                return original(strategy, symbol)
            row = dict(self._base(symbol, "ACTUAL_L12_ENTRY_DECISION"), before=self._state(symbol))
            caller = evaluation_context.get()
            row["slot_context"] = (caller.get("actual_strategy_slot", "UNKNOWN_NO_STRICT_SLOT_CONTEXT")
                if caller is not None and caller["symbol"] == symbol else "UNKNOWN_NO_STRICT_SLOT_CONTEXT")
            snapshot = getattr(getattr(strategy, "_brokerdecides1_memory", None), "snapshot", None)
            row["snapshot"] = dict(epoch=getattr(snapshot, "epoch", None), generation=getattr(snapshot, "generation", None),
                local_complete=getattr(snapshot, "local_complete", None),
                origin="OBSERVED_EXISTING_MEMORY_NOT_NATIVE_ATTESTATION")
            try:
                result = original(strategy, symbol)  # Exactly this actual invocation, never an extra guard call.
            except BaseException as primary:
                self._record_error(row, primary)
                raise
            segment = row["before"]["line_segment"]
            row.update(allowed=result.allowed, reason=result.reason, oldest_age_ms=result.oldest_age_ms,
                after=self._state(symbol), status="ACTUAL_DECISION_RETURN_NOT_NATIVE_PERMISSION",
                local_segment_operand=("OBSERVED_POSITIVE_KEY_ONLY" if type(segment) is int and segment > 0
                    else "UNKNOWN_OR_NONPOSITIVE_KEY"),
                segment_check_exercised=("ACTUAL_FILL_SEGMENT_REFUSAL" if result.reason == "fill_segment_UNKNOWN"
                    else "NOT_INFERRED_FROM_OTHER_REASON"))
            self._append(row)
            current = evaluation_context.get()
            if current is not None and current["symbol"] == symbol:
                current.setdefault("actual_l12_decisions", []).append(deepcopy(row))
            return result
        return entry

    def wrap_rebuild(self, original):
        async def rebuild(symbol, *args, **kwargs):
            before = self.bot._line_published.get(symbol)
            row = dict(self._base(symbol, "ACTUAL_LINE_REBUILD"), before=self._state(symbol))
            try:
                result = await original(symbol, *args, **kwargs)
            except BaseException as primary:
                self._record_error(row, primary)
                raise
            # No extra await after the real publication before observing it.
            completed = self._base(symbol, "ACTUAL_LINE_REBUILD")
            row["started_clock_ns"], row["started_window_ids"] = row["clock_ns"], row["window_ids"]
            row.update(completed)
            row.update(returned=result if type(result) is bool or result is None else None,
                return_type=type(result).__name__, after=self._state(symbol), published=self._published(symbol),
                publication_changed=self.bot._line_published.get(symbol) is not before)
            published = row["published"]
            key, live = published.get("indicator_SHORT_id"), row["after"]["atr_short_flip_bar_ts"]
            if result is not True:
                status = "REBUILD_NOT_SUCCESSFUL_NOT_NO_SELL_PROOF"
            elif published["status"] == "NO_PUBLISHED_PREFIX_OBSERVED":
                status = "UNKNOWN_SUCCESS_WITHOUT_PUBLISHED_PREFIX_WITNESS"
            elif not row["publication_changed"]:
                status = "CACHED_CURRENT_LINE_NOT_NEW_SELL"
            elif published.get("indicator_state") == "short" and (type(key) is not int or key <= 0 or live != key
                    or row["after"]["atr_state"] != "short"):
                status = "STOP_SUCCESSFUL_SHORT_KEY_PUBLICATION_CONTRADICTION"
            elif published.get("indicator_state") == "short":
                status = "OBSERVED_SHORT_KEY_WRITE_THROUGH_NOT_ENTRY_PERMISSION"
            else:
                status = "OBSERVED_NONSHORT_PREFIX_NO_SELL_GUARANTEE"
            row["status"] = status
            self._append(row)
            return result
        return rebuild

    def signal(self, state, result, *, before, context, state_only, origin="ACTUAL_INDICATOR_RETURN"):
        if not isinstance(result, dict) or result.get("flip") not in {"BUY", "SELL"}:
            return
        kind = "ACTUAL_INDICATOR_FLIP" if origin == "ACTUAL_INDICATOR_RETURN" else "ACTUAL_RESTORED_SIGNAL_ARGUMENT"
        row = dict(self._base(state.symbol, kind), before=before, signal_origin=origin,
            after=self._state(state.symbol), state_only=state_only,
            signal={key: result.get(key) for key in ("flip", "decision_prev_bar_ts", "decision_cur_bar_ts",
                "decision_gap_ms", "decision_short_segment_id", "observation_phase")},
            status="ACTUAL_SIGNAL_NOT_DRAFT_OR_NATIVE_ADMISSION")
        self._append(row)
        if context is not None and context["symbol"] == state.symbol:
            context.setdefault("actual_causal_signals", []).append(deepcopy(row))

    def evaluation(self, observed):
        if observed.get("flip") != "BUY":
            return
        row = self._base(observed["symbol"], "ACTUAL_BUY_EVALUATION")
        row.update(status=observed["status"], bar_ms=observed["bar_ms"],
            signal_source=observed.get("signal_source"), returned_BUY_draft=observed.get("returned_BUY_draft"),
            returned_draft_reason=observed.get("returned_draft_reason"), exception_type=observed.get("exception_type"),
            decision_ids=[value["observation_id"] for value in observed.get("actual_l12_decisions", [])],
            signal_ids=[value["observation_id"] for value in observed.get("actual_causal_signals", [])],
            actual_return_reasons=deepcopy(observed["actual_return_reasons"]))
        self._append(row)

    def report(self, windows):
        ids = [window["event_id"] for window in windows]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate_causal_window_identity")
        return dict(status="OBSERVATIONS_ONLY_NOT_FULL_MUST_NOT_BREAK_ACCEPTANCE", window_denominator=len(ids),
            windows=[dict(event_id=identity, observation_ids=[row["observation_id"] for row in self.records
                if identity in row["window_ids"]], missing_witnesses=[label for kind, label in (
                    ("ACTUAL_LINE_REBUILD", "NO_REBUILD_CALL_OBSERVED"),
                    ("ACTUAL_BUY_EVALUATION", "NO_ACTUAL_BUY_EVALUATION_OBSERVED"),
                    ("ACTUAL_L12_ENTRY_DECISION", "NO_ACTUAL_L12_CALL_OBSERVED"))
                if not any(row["kind"] == kind and identity in row["window_ids"] for row in self.records)],
                first_BUY_evaluation_id=next((row["observation_id"] for row in self.records
                    if row["kind"] == "ACTUAL_BUY_EVALUATION" and identity in row["window_ids"]), None))
                for identity in ids], records=self.records,
            observed_contradictions=[row["observation_id"] for row in self.records if row["status"].startswith("STOP_")],
            native_permission="UNKNOWN", permission_granted=False)
