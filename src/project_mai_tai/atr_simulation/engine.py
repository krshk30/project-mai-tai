"""Side-by-side pure episode engine; storage and provider access are external.

simulate_many is the conservation boundary: submit all overlapping broker/account
episodes together. Its stable simulated placement priority is not native priority.
Separate calls are independent alternative worlds, not additive execution proof.
"""

from dataclasses import dataclass
from decimal import Decimal

from .entry import episode_id, prepare_entry
from .exits import Position, UNMODELED_LIVE_REASONS, causal_key
from .fill_models import PrintLiquidity
from .execution_eligibility import classify_execution, condition_coverage, execution_status, note_unknown
from .volume_body import legal_cap
from .types import (
    CONTRACT_VERSION, EntryDecision, EntryFact, EntryMode, EntryPlan, EpisodeInput,
    EpisodeResult, FillAllocation, FillModel, ScenarioResult, SimExit, TradePrint,
)


def _validate(episode: EpisodeInput) -> None:
    for boundary in (episode.buy_flip, episode.sell_flip):
        if boundary is not None and boundary.bar_start_ns is not None:
            if boundary.available_ns < boundary.bar_start_ns + 60_000_000_000:
                raise ValueError("flip_close_availability_required")
    keys = set()
    prior = None
    for trade in episode.trades:
        if trade.symbol != episode.identity.symbol:
            raise ValueError("episode_trade_symbol_mismatch")
        key = (trade.key.event_ns, trade.key.sequence)
        raw = trade.key.raw_id
        if raw in keys or (prior is not None and key <= prior):
            raise ValueError("duplicate_or_noncausal_trade")
        keys.add(raw)
        prior = key
        execution_status(trade)
        if trade.execution is not None and trade.execution.policy != episode.execution_policy:
            raise ValueError("episode_execution_policy_mismatch")
    if episode.live.bought is False and (
        episode.live.entries or episode.live.coverage.complete is not True
    ):
        raise ValueError("did_not_buy_requires_complete_affirmative_evidence")


def _position_args(episode: EpisodeInput) -> dict:
    fallback = None
    unmodeled = tuple(exit for exit in episode.live.exits if exit.mapped_reason in UNMODELED_LIVE_REASONS)
    fallback_issues = ()
    if unmodeled:
        candidate = unmodeled[0]
        if (
            episode.live.bought is True and len(episode.live.exits) == 1
            and sum(entry.quantity for entry in episode.live.entries) == candidate.quantity
            and all(causal_key(entry.at) < causal_key(candidate.at) for entry in episode.live.entries)
        ):
            fallback = candidate
        else:
            fallback_issues = ("UNKNOWN_LIVE_FALLBACK_QUANTITY_OR_CAUSAL_ALLOCATION",)
    return dict(
        sell_flip=episode.sell_flip, session_end=episode.session_end,
        coverage=episode.tape_coverage, live_fallback=fallback, issues=fallback_issues,
    )


def _pnl(entries: tuple[EntryFact, ...], exits: tuple[SimExit, ...], issues: tuple[str, ...], position: Position) -> Decimal | None:
    if not entries or issues or any(exit.price is None or exit.issues for exit in exits):
        return None
    if sum(exit.quantity for exit in exits) != sum(entry.quantity for entry in entries):
        raise ValueError("exit_quantity_does_not_conserve_entries")
    if len(exits) != 1:
        raise ValueError("one_position_one_exit_required")
    return position.quantity * (exits[0].price - position.vwap)


def _fill_entries(fills: tuple[FillAllocation, ...]) -> tuple[EntryFact, ...]:
    return tuple(
        EntryFact(f"{fill.order_id}:{fill.print_key.raw_id}", fill.print_key, fill.price, fill.quantity, fill.print_key.source)
        for fill in fills if fill.quantity
    )


def _risk_trade(position: Position, trade: TradePrint, unknowns: dict) -> None:
    """Boundary/LIVE events remain unconditional; only print risk is eligible."""
    position.before(trade.key)
    status, _ = execution_status(trade)
    if status is None and position.exit is None and position.first is not None and (
        causal_key(trade.key) > causal_key(position.first)
    ):
        note_unknown(position.issues, "UNKNOWN_EXECUTION_EXIT_ELIGIBILITY")
        unknowns[trade.key.raw_id] = trade.execution or classify_execution(trade, None, None)
    if status is True:
        position.on_trade(trade)


def _eligibility_position(entries, episode, unknowns):
    position = Position(**_position_args(episode))
    pending = iter(entries)
    entry = next(pending, None)
    for trade in episode.trades:
        while entry is not None and causal_key(entry.at) <= causal_key(trade.key):
            position.add_fill(entry)
            entry = next(pending, None)
        _risk_trade(position, trade, unknowns)
    while entry is not None:
        position.add_fill(entry)
        entry = next(pending, None)
    return position


def _execution_unknown(issues):
    return any("EXECUTION" in issue for issue in issues if issue.startswith("UNKNOWN_"))


@dataclass(frozen=True)
class _TodayPlan:
    entries: tuple[EntryFact, ...]
    issues: tuple[str, ...]
    decision: EntryDecision
    assumption: str
    position: Position | None = None
    execution_unknowns: tuple = ()


def _today_plans(episodes: tuple[EpisodeInput, ...], ids: tuple[str, ...], tape: tuple[TradePrint, ...]) -> dict[str, _TodayPlan]:
    plans: dict[str, _TodayPlan] = {}
    candidates: dict[str, EpisodeInput] = {}
    for key, episode in zip(ids, episodes):
        entries = episode.live.entries if episode.live.bought is True else episode.today_entries
        assumption = "ACTUAL_LIVE_ENTRY_FACT_NOT_NEW_NATIVE_FILL" if episode.live.bought is True else "SUPPLIED_TODAY_ENTRY_EVIDENCE_NOT_NEW_NATIVE_FILL"
        if episode.live.bought is True and episode.today_entries and episode.today_entries != entries:
            raise ValueError("conflicting_live_and_supplied_today_entries")
        if entries:
            coverage = episode.live.coverage if episode.live.bought is True else episode.today_entry_coverage
            issues = (*coverage.gaps, *(episode.live.issues if episode.live.bought is True else ()))
            if coverage.complete is not True:
                issues += ("UNKNOWN_TODAY_ENTRY_COVERAGE",)
            plans[key] = _TodayPlan(entries, issues, EntryDecision.BUY_ON_TOUCH, assumption)
            continue
        issues = []
        if episode.live.bought is True:
            issues.append("UNKNOWN_LIVE_BUY_FILL_DETAILS")
        if episode.watched is not True or episode.watched_source is None:
            issues.append("UNKNOWN_OR_NOT_WATCHED")
        if episode.trigger is None or episode.quantity is None or episode.sizing_source is None:
            issues.append("UNKNOWN_TRIGGER_OR_SIZING")
        elif isinstance(episode.quantity, bool) or not isinstance(episode.quantity, int) or episode.quantity <= 0:
            raise ValueError("positive_integer_quantity_required")
        end_ns = _wait_end(episode)
        if end_ns <= episode.buy_flip.available_ns:
            issues.append("INVALID_FLIP_BOUNDARY_ORDER")
        for name, coverage in (("FLIP", episode.flip_coverage), ("TAPE", episode.tape_coverage)):
            issues.extend(coverage.gaps)
            if (
                coverage.complete is not True or coverage.start_ns > episode.buy_flip.available_ns
                or coverage.end_ns < end_ns
            ):
                issues.append(f"UNKNOWN_TODAY_{name}_WINDOW")
        if issues:
            plans[key] = _TodayPlan((), tuple(dict.fromkeys(issues)), EntryDecision.UNKNOWN, "NO_ENTRY_INVENTED")
        else:
            legal_cap(episode.trigger)  # Validate the exact trigger before planning.
            candidates[key] = episode

    # TODAY is a separate counterfactual world from either VOLBODY model.
    # Causal first-in-band allocations share one print ledger across accounts.
    quantities = {key: episode.quantity for key, episode in candidates.items()}
    allocated: dict[str, list[EntryFact]] = {key: [] for key in candidates}
    positions = {key: Position(**_position_args(episode)) for key, episode in candidates.items()}
    priority = sorted(candidates, key=lambda key: (candidates[key].buy_flip.available_ns, key))
    unknowns = {key: {} for key in candidates}
    uncertain_windows = {}
    for trade in tape:
        if isinstance(trade.size, bool) or not isinstance(trade.size, int) or trade.size < 0:
            raise ValueError("nonnegative_integer_size_required")
        if not trade.price.is_finite() or trade.price <= 0:
            raise ValueError("positive_finite_price_required")
        remaining = trade.size
        status, _ = execution_status(trade)
        if status is None:
            for key in priority:
                episode, position = candidates[key], positions[key]
                if (trade.symbol == episode.identity.symbol and position.can_fill and quantities[key] > 0
                        and episode.buy_flip.available_ns <= trade.key.event_ns < _wait_end(episode)
                        and episode.trigger <= trade.price <= legal_cap(episode.trigger)):
                    uncertain_windows.setdefault(key, (trade.symbol, _wait_end(episode),
                        trade.execution or classify_execution(trade, None, None)))
        for key in priority:
            episode = candidates[key]
            if trade.symbol != episode.identity.symbol:
                continue
            position = positions[key]
            position.before(trade.key)
            for symbol, end, evidence in uncertain_windows.values():
                if (symbol == trade.symbol and trade.key.event_ns < end
                        and episode.buy_flip.available_ns <= trade.key.event_ns < _wait_end(episode)):
                    note_unknown(position.issues)
                    unknowns[key][evidence.raw_id] = evidence
            if (
                position.can_fill and remaining > 0 and quantities[key] > 0
                and status is True and not _execution_unknown(position.issues)
                and episode.buy_flip.available_ns <= trade.key.event_ns < _wait_end(episode)
                and episode.trigger <= trade.price <= legal_cap(episode.trigger)
            ):
                quantity = min(remaining, quantities[key])
                entry = EntryFact(f"ATRSIM1:{key}:TODAY:{trade.key.raw_id}", trade.key, trade.price, quantity, trade.key.source)
                if position.add_fill(entry):
                    remaining -= quantity
                    quantities[key] -= quantity
                    allocated[key].append(entry)
            _risk_trade(position, trade, unknowns[key])
    actual_or_supplied_keys = frozenset(plans)
    for key, episode in candidates.items():
        entries = tuple(allocated[key])
        issues = list(positions[key].issues)
        # Actual fills have no proven mapping to public print volume. Do not
        # pretend mixed real/modeled accounts establish native conservation.
        if any(
            other.identity.symbol == episode.identity.symbol and other_key in actual_or_supplied_keys
            and plans[other_key].entries
            and max(episode.buy_flip.available_ns, other.buy_flip.available_ns) < min(_wait_end(episode), _wait_end(other))
            for other_key, other in zip(ids, episodes) if other_key != key
        ):
            issues.append("UNKNOWN_ACTUAL_FILL_PRINT_ALLOCATION")
        plans[key] = _TodayPlan(
            entries, tuple(issues), EntryDecision.UNKNOWN if _execution_unknown(issues) else
            EntryDecision.MODELLED_ENTRY if entries else EntryDecision.NO_MODELLED_ENTRY,
            "MODELED_FIRST_IN_BAND_SHARED_PRINT_VOLUME_NOT_NATIVE_PRIORITY",
            positions[key],
            tuple(unknowns[key].values()),
        )
    return plans


def _today(episode: EpisodeInput, plan: _TodayPlan) -> ScenarioResult:
    issues = list(plan.issues)
    entries = plan.entries
    if len({entry.fill_id for entry in entries}) != len(entries):
        raise ValueError("duplicate_today_fill_id")
    for entry in entries:
        if entry.at.event_ns < episode.buy_flip.available_ns:
            raise ValueError("today_entry_precedes_buy_flip")
        if episode.sell_flip is not None and entry.at.event_ns >= episode.sell_flip.available_ns:
            raise ValueError("today_entry_not_in_buy_segment")
    unknowns = {evidence.raw_id: evidence for evidence in plan.execution_unknowns}
    position = plan.position or _eligibility_position(entries, episode, unknowns)
    exits = position.finish()
    entries = tuple(position.fills)
    issues.extend(issue for exit in exits for issue in exit.issues)
    issues.extend(position.issues)
    issues = list(dict.fromkeys(issues))
    fills = tuple(
        FillAllocation(entry.fill_id, entry.at, entry.price, entry.quantity, 0, plan.assumption)
        for entry in entries
    )
    return ScenarioResult(
        EntryMode.TODAY, None,
        EntryDecision.UNKNOWN if _execution_unknown(issues) else plan.decision,
        (), fills, exits, _pnl(entries, exits, tuple(issues), position), tuple(issues),
        entry_facts=entries, position=position.receipt(),
        execution_unknowns=tuple(unknowns.values()),
    )


def _finish(episode: EpisodeInput, model: FillModel, plan: EntryPlan, fills: tuple[FillAllocation, ...], position: Position, unknowns: dict) -> ScenarioResult:
    entries = tuple(position.fills)
    exits = position.finish()
    issues = list(plan.issues)
    issues.extend(position.issues)
    issues.extend(issue for exit in exits for issue in exit.issues)
    wait_end_ns = min(
        episode.session_end.available_ns,
        episode.sell_flip.available_ns if episode.sell_flip is not None else episode.session_end.available_ns,
    )
    if episode.tape_coverage.end_ns < wait_end_ns:
        issues.append("UNKNOWN_WAIT_CONTINUATION_TO_TERMINATION")
    if _execution_unknown(issues) or issues and not entries:
        decision = EntryDecision.UNKNOWN
    elif entries:
        decision = EntryDecision.VB_WAIT_BUY
    elif plan.touched:
        decision = EntryDecision.VB_WAIT_NEVER_FILLED
    else:
        decision = EntryDecision.VB_SKIP_TO_SELL_FLIP
    unique_issues = tuple(dict.fromkeys(issues))
    return ScenarioResult(
        EntryMode.VOLBODY, model, decision,
        (plan.order,) if plan.order is not None else (), fills, exits,
        _pnl(entries, exits, unique_issues, position), unique_issues, plan.observations, entries, position.receipt(),
        tuple(unknowns.values()),
    )


def _wait_end(episode: EpisodeInput) -> int:
    return min(
        episode.session_end.available_ns,
        episode.sell_flip.available_ns if episode.sell_flip is not None else episode.session_end.available_ns,
    )


def _shared_tape(episodes: tuple[EpisodeInput, ...]) -> tuple[TradePrint, ...]:
    tape: dict[tuple[str, str], TradePrint] = {}
    for episode in episodes:
        for trade in episode.trades:
            raw = (trade.symbol, trade.key.raw_id)
            if raw in tape:
                prior = tape[raw]
                if (
                    prior.price, prior.size, prior.key.event_ns, prior.key.sequence, prior.key.received_ns, prior.execution,
                ) != (
                    trade.price, trade.size, trade.key.event_ns, trade.key.sequence, trade.key.received_ns, trade.execution,
                ):
                    raise ValueError("conflicting_shared_raw_print")
            else:
                tape[raw] = trade
    return tuple(sorted(tape.values(), key=lambda t: (t.key.event_ns, t.key.sequence, t.symbol, t.key.raw_id)))


class SimulationEngine:
    def simulate(self, episode: EpisodeInput) -> EpisodeResult:
        return self.simulate_many((episode,))[0]

    def simulate_many(self, episodes: tuple[EpisodeInput, ...]) -> tuple[EpisodeResult, ...]:
        ids = tuple(episode_id(episode.identity) for episode in episodes)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_episode_identity")
        for episode in episodes:
            _validate(episode)
        tape = _shared_tape(episodes)
        today_plans = _today_plans(episodes, ids, tape)
        model_results: dict[tuple[str, FillModel], ScenarioResult] = {}
        for model in FillModel:
            plans = {key: prepare_entry(episode, model) for key, episode in zip(ids, episodes)}
            orders = sorted(
                ((key, episode, plans[key].order) for key, episode in zip(ids, episodes) if plans[key].order is not None),
                key=lambda item: (item[2].placed_at.event_ns, item[2].placed_at.sequence, item[2].order_id),
            )
            allocations: dict[str, list[FillAllocation]] = {key: [] for key in ids}
            positions = {key: Position(**_position_args(episode)) for key, episode in zip(ids, episodes)}
            unknowns = {key: {} for key in ids}
            book = PrintLiquidity(model)
            # Register all possible earlier orders before account priority can
            # spend either shared pool. Their causal starts prevent look-ahead.
            for key, episode in zip(ids, episodes):
                for issue in plans[key].issues:
                    note_unknown(positions[key].issues, issue)
                raw_keys = {trade.key.raw_id: trade.key for trade in episode.trades}
                for evidence in plans[key].execution_unknowns:
                    unknowns[key][evidence.raw_id] = evidence
                    book.register_planning_uncertainty(key, episode.identity.symbol,
                        raw_keys[evidence.raw_id], _wait_end(episode), evidence)
            for trade in tape:
                for key, episode, order in orders:
                    if trade.symbol != episode.identity.symbol:
                        continue
                    position = positions[key]
                    position.before(trade.key)
                    active = (causal_key(trade.key) >= causal_key(order.placed_at)
                              and trade.key.event_ns < _wait_end(episode))
                    if active and position.can_fill:
                        for evidence in book.unknown_evidence(trade.symbol, trade.key.event_ns, trade.key.sequence):
                            note_unknown(position.issues)
                            unknowns[key][evidence.raw_id] = evidence
                    if (position.can_fill and not plans[key].issues and not _execution_unknown(position.issues)
                            and causal_key(trade.key) >= causal_key(order.placed_at)
                            and trade.key.event_ns < _wait_end(episode)):
                        if (trade.key.event_ns, trade.key.sequence) == (
                            order.placed_at.event_ns, order.placed_at.sequence
                        ):
                            immediate = book.allocate_placement(order)
                            if immediate is not None:
                                allocations[key].append(immediate)
                                if not position.add_fill(_fill_entries((immediate,))[0]):
                                    raise ValueError("placement_fill_after_position_closed")
                        fill = book.allocate(order, trade)
                        for evidence in book.unknown_evidence(trade.symbol, trade.key.event_ns, trade.key.sequence):
                            note_unknown(position.issues)
                            unknowns[key][evidence.raw_id] = evidence
                        if fill is not None:
                            allocations[key].append(fill)
                            if fill.quantity:
                                if not position.add_fill(_fill_entries((fill,))[0]):
                                    raise ValueError("allocated_fill_after_position_closed")
                    _risk_trade(position, trade, unknowns[key])
            for key, episode in zip(ids, episodes):
                model_results[key, model] = _finish(episode, model, plans[key], tuple(allocations[key]), positions[key], unknowns[key])
        return tuple(
            EpisodeResult(
                CONTRACT_VERSION, episode.identity, episode.live, _today(episode, today_plans[key]),
                model_results[key, FillModel.QUEUE_AWARE], model_results[key, FillModel.OPTIMISTIC],
                (), ("REFERENCE_COMPATIBILITY_NOT_COMPUTED",),
                episode.execution_policy,
                condition_coverage=condition_coverage(episode.trades),
            ) for key, episode in zip(ids, episodes)
        )
