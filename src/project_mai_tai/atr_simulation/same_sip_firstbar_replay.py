"""C1 2026-10-28 explicit TODAY tie successor. Original sealed module unchanged."""
"""Pure FIRSTBAR cohort replay; callers own files, storage, services and UI.

All overlapping accounts must enter one simulate_many call. Each mode is an
independent world. Modeled account priority and print-volume fills are explicit
counterfactuals, not reconstructed exchange queue priority or native execution.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .same_sip_eligibility import execution_status
from .same_sip_exits import UNMODELED_LIVE_REASONS, causal_key
from .same_sip_firstbar import (
    CompletedFlipBar, FirstBarEntryPlan,
    FirstBarMode, plan_firstbar_entry,
)
from .same_sip_firstbar_exits import FirstBarPosition
from .same_sip import TieTape, TieState, enabled, report_key, LABEL
from .same_sip_types import (
    Boundary, EntryFact, EpisodeInput, FlipIdentity, LiveFacts, PositionReceipt,
    SimExit, SimExitReason, TradePrint,
)


FIRSTBAR_REPLAY_VERSION = "ATRSIM_FIRSTBAR_REPLAY_V2_A2_CEILING_CAUSAL_STOP_PRINT"


class FirstBarFillPolicy(str, Enum):
    OBSERVED_PRINT_VOLUME = "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME"


@dataclass(frozen=True)
class FirstBarReplayInput:
    episode: EpisodeInput
    flip_bar: CompletedFlipBar
    entry_cutoff: Boundary


@dataclass(frozen=True)
class FirstBarReplayResult:
    version: str
    identity: FlipIdentity
    mode: FirstBarMode
    fill_policy: FirstBarFillPolicy
    clock_label: str
    live: LiveFacts
    plan: FirstBarEntryPlan
    decision: str
    fills: tuple[EntryFact, ...]
    exits: tuple[SimExit, ...]
    position: PositionReceipt
    gross_pnl: Decimal | None
    issues: tuple[str, ...]
    assumption: str

    @property
    def is_trade(self) -> bool:
        return bool(self.fills)

    @property
    def is_skipped(self) -> bool:
        return self.decision.startswith("SKIPPED_")


def _identity(identity: FlipIdentity) -> tuple[str, ...]:
    return (identity.trading_day, identity.symbol, identity.broker,
            identity.account_id, identity.strategy, identity.flip_id, identity.segment_id)


def _slot(identity: FlipIdentity) -> tuple[str, str, str]:
    # Broker labels do not create two slots for the same supplied account.
    return identity.account_id, identity.strategy, identity.symbol


def _entry_lifetime_end(item: FirstBarReplayInput) -> int:
    episode = item.episode
    return min(episode.session_end.available_ns,
               episode.sell_flip.available_ns if episode.sell_flip else
               episode.session_end.available_ns)


def _position(item: FirstBarReplayInput, mode: FirstBarMode, *, same_sip_state=None) -> FirstBarPosition:
    episode = item.episode
    fallback = None
    issues = []
    unmodeled = tuple(fact for fact in episode.live.exits
                      if fact.mapped_reason in UNMODELED_LIVE_REASONS)
    if unmodeled:
        fact = unmodeled[0]
        if (episode.live.bought is True and episode.live.coverage.complete is True
                and not episode.live.issues and len(episode.live.exits) == 1
                and sum(entry.quantity for entry in episode.live.entries) == fact.quantity
                and all(causal_key(entry.at) < causal_key(fact.at) for entry in episode.live.entries)):
            fallback = fact
        else:
            issues.append("UNKNOWN_FIRSTBAR_LIVE_CONFIRMATION_FALLBACK")
    return FirstBarPosition(mode=mode, sell_flip=episode.sell_flip,
                            session_end=episode.session_end, coverage=episode.tape_coverage,
                            live_fallback=fallback, issues=tuple(issues), same_sip_state=same_sip_state)


class FirstBarReplayEngine:
    def simulate(self, item: FirstBarReplayInput, *, fill_policy: FirstBarFillPolicy
                 ) -> tuple[FirstBarReplayResult, ...]:
        """Single input convenience; use simulate_many for shared physical tape."""
        return self.simulate_many((item,), fill_policy=fill_policy)

    def simulate_many(self, items: tuple[FirstBarReplayInput, ...], *,
                      fill_policy: FirstBarFillPolicy, retained_modes=None, retained_reports=None,
                      retained_bindings=None, current_bindings=None) -> tuple[FirstBarReplayResult, ...]:
        if fill_policy is not FirstBarFillPolicy.OBSERVED_PRINT_VOLUME:
            raise ValueError("explicit_supported_firstbar_fill_policy_required")
        for item in items:
            enabled(item.episode.same_sip_policy)
        self.same_sip_reports = {}
        ids = tuple(_identity(item.episode.identity) for item in items)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate_firstbar_flip_account_identity")
        physical: dict[tuple[str, str], TradePrint] = {}
        membership = []
        for item in items:
            membership.append({row.key.raw_id for row in item.episode.trades})
            for trade in item.episode.trades:
                if trade.execution is not None and trade.execution.policy != item.episode.execution_policy:
                    raise ValueError("episode_execution_policy_mismatch")
                execution_status(trade)
                key = (trade.symbol, trade.key.raw_id)
                if key in physical and physical[key] != trade:
                    raise ValueError("conflicting_shared_firstbar_print")
                physical[key] = trade
        tape = sorted(physical.values(), key=lambda row: (
            row.key.event_ns, row.key.sequence, row.symbol, row.key.raw_id))
        from .same_sip_reuse import firstbar_retained
        retained, reports = firstbar_retained(items, retained_modes, retained_reports,
            retained_bindings, current_bindings, fill_policy)
        self.same_sip_reports = reports
        tie_index = TieTape(tape) if any(enabled(item.episode.same_sip_policy) for item in items) else None
        results = []
        for mode in FirstBarMode:
            if mode.value in retained:
                results.extend(retained[mode.value])
                continue
            states = tuple(TieState(tie_index) if enabled(item.episode.same_sip_policy) else None for item in items)
            plans = tuple(plan_firstbar_entry(item.episode, item.flip_bar, mode=mode,
                          entry_cutoff=item.entry_cutoff, same_sip_state=state) for item, state in zip(items, states))
            positions = tuple(_position(item, mode, same_sip_state=state) for item, state in zip(items, states))
            extra_issues: list[list[str]] = [list(position.issues) for position in positions]
            for index, item in enumerate(items):
                for other, other_item in enumerate(items):
                    if index == other or item.episode.identity.symbol != other_item.episode.identity.symbol:
                        continue
                    left = max(plans[index].eligible_after_ns, plans[other].eligible_after_ns)
                    right = min(plans[index].end_ns, plans[other].end_ns)
                    if left < right and plans[other].decision == "UNKNOWN":
                        extra_issues[index].append("UNKNOWN_SHARED_FIRSTBAR_ALLOCATION")
                    if (_slot(item.episode.identity) == _slot(other_item.episode.identity)
                            and plans[index].decision in ("ENTRY_CANDIDATE", "UNKNOWN")
                            and plans[other].decision in ("ENTRY_CANDIDATE", "UNKNOWN")
                            and max(item.episode.buy_flip.available_ns,
                                    other_item.episode.buy_flip.available_ns)
                            < min(_entry_lifetime_end(item), _entry_lifetime_end(other_item))):
                        # Distinct flip IDs alone do not prove a second slot.
                        # A target guessed from future tape cannot authorize it.
                        extra_issues[index].append("UNKNOWN_OVERLAPPING_FIRSTBAR_SLOT")
            priority = sorted(range(len(items)), key=lambda i: (
                plans[i].candidate.key.event_ns if plans[i].candidate else plans[i].end_ns,
                plans[i].candidate.key.sequence if plans[i].candidate else 0, ids[i]))
            for trade in tape:
                remaining = trade.size
                eligible, _ = execution_status(trade)
                if eligible is None:
                    uncertain = any(
                        item.episode.identity.symbol == trade.symbol
                        and trade.key.raw_id in membership[index]
                        and plans[index].decision == "ENTRY_CANDIDATE"
                        and causal_key(trade.key) >= causal_key(plans[index].candidate.key)
                        and positions[index].can_fill
                        and positions[index].quantity < plans[index].requested_quantity
                        and trade.key.event_ns < min(
                            item.episode.session_end.available_ns,
                            item.episode.sell_flip.available_ns if item.episode.sell_flip else
                            item.episode.session_end.available_ns,
                        )
                        for index, item in enumerate(items)
                    )
                    if uncertain:
                        # Unknown earlier exposure can alter later shared volume.
                        # Preserve already allocated facts but never certify a
                        # different account's outcome by ignoring that exposure.
                        for index, item in enumerate(items):
                            if item.episode.identity.symbol == trade.symbol:
                                extra_issues[index].append("UNKNOWN_SHARED_FIRSTBAR_PENDING_FILL")
                for index in priority:
                    item, plan, position = items[index], plans[index], positions[index]
                    if trade.symbol != item.episode.identity.symbol:
                        continue
                    # An episode cannot consume evidence absent from its bound raw tape.
                    if trade.key.raw_id not in membership[index]:
                        continue
                    position.before(trade.key)
                    if plan.decision != "ENTRY_CANDIDATE":
                        continue
                    candidate = plan.candidate
                    if causal_key(trade.key) < causal_key(candidate.key):
                        continue
                    if eligible is None and position.can_fill:
                        position.issues.append("UNKNOWN_FIRSTBAR_PENDING_FILL_ELIGIBILITY")
                        extra_issues[index].append("UNKNOWN_FIRSTBAR_PENDING_FILL_ELIGIBILITY")
                    state = states[index]
                    if state is not None and state.tape.unresolved(trade) and position.can_fill:
                        extra_issues[index].append('UNKNOWN_FIRSTBAR_PENDING_FILL_ELIGIBILITY')
                    if eligible is True and position.can_fill and not extra_issues[index]:
                        if state is not None and not state.entry_allowed(trade, plan.trigger,
                                kind='stop', placed_ns=plan.eligible_after_ns):
                            position.on_trade(trade)
                            continue
                        quantity = min(plan.requested_quantity - position.quantity, remaining)
                        if quantity > 0:
                            for other, other_item in enumerate(items):
                                if (other != index and _slot(item.episode.identity)
                                        == _slot(other_item.episode.identity)):
                                    prior = positions[other]
                                    prior.before(trade.key)
                                    if prior.quantity and prior.exit is None:
                                        extra_issues[index].append("UNKNOWN_PRIOR_FIRSTBAR_SLOT_NOT_CLOSED")
                                        break
                            if extra_issues[index]:
                                position.on_trade(trade)
                                continue
                            fact = EntryFact(
                                "FIRSTBAR:" + ":".join(ids[index]) + ":" + trade.key.raw_id,
                                trade.key, trade.price, quantity, trade.key.source,
                            )
                            if position.add_fill(fact):
                                remaining -= quantity
                    position.on_trade(trade)
            for index, item in enumerate(items):
                position, plan = positions[index], plans[index]
                exits = position.finish()
                issues = tuple(dict.fromkeys((*plan.issues, *extra_issues[index],
                                              *position.issues,
                                              *(issue for exit in exits for issue in exit.issues))))
                decision = plan.decision
                pnl = None
                if issues:
                    decision = "UNKNOWN"
                elif position.quantity:
                    decision = "TRADE_OPEN" if exits[0].reason is SimExitReason.OPEN else "TRADE_CLOSED"
                    if exits[0].price is not None:
                        pnl = position.quantity * (exits[0].price - position.vwap)
                elif plan.decision == "ENTRY_CANDIDATE":
                    decision = "SKIPPED_NO_ALLOCATED_VOLUME_BEFORE_BOUNDARY"
                state = states[index]
                if state is not None:
                    self.same_sip_reports[report_key(item.episode.identity, mode.value)] = state.report()
                results.append(FirstBarReplayResult(
                    FIRSTBAR_REPLAY_VERSION, item.episode.identity, mode, fill_policy,
                    "reconstructed SIP clock", item.episode.live, plan, decision,
                    tuple(position.fills), exits, position.receipt(), pnl, issues,
                    "MODELED_STOP_MARKET_PRINT_VOLUME; stable account priority NOT native queue; "
                    "partial remainder may execute at later eligible print prices; "
                    "no reference next-print lookahead or full-quantity shortcut; "
                    "C1_20261023_2129: A2 CEILING target / first eligible stop print; "
                    "historical native route and queue UNKNOWN" + ('; ' + LABEL if state is not None and state.touched else ''),
                ))
        return tuple(results)
