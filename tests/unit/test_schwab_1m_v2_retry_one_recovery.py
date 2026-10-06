"""LXEH 2026-09-23: no-order watchlist removal must not pollute recovery all night."""

from __future__ import annotations

import logging

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


OPPORTUNITY = 1790163002631


def _strategy():
    strategy = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_flip_owned_first_entry_enabled=True,
            strategy_schwab_1m_v2_removed_wait_clear_enabled=False,
            strategy_schwab_1m_v2_retry_one_enabled=True,
        )
    )
    clock = [1_790_164_000_000]
    strategy._now_ms = lambda: clock[0]
    identities = []
    owners = []
    strategy.configure_fanout_identity_persistence(
        lambda symbol, segment, active, reason: identities.append(
            (symbol, segment, active, reason)
        )
    )
    strategy.configure_flip_entry_ownership(
        lambda record, active, reason: owners.append((record, active, reason)),
        restore_readable=True,
    )
    return strategy, clock, identities, owners


def test_lxeh_flat_no_order_owner_retires_on_watchlist_removal() -> None:
    strategy, clock, identities, owners = _strategy()
    state = strategy.watchlist_state("LXEH")
    state.fanout_segment_id = OPPORTUNITY
    state.flip_owner_opportunity_id = OPPORTUNITY
    state.flip_owner_phase = "unknown"
    state.flip_owner_first_rest_placed = False
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = clock[0]

    strategy.release_and_drop_symbol("LXEH")

    assert "LXEH" not in strategy._symbol_states
    assert strategy.unknown_flip_owner_opportunities() == {}
    assert identities[-1] == (
        "LXEH", OPPORTUNITY, False, "watchlist_removed_without_entry_order"
    )
    assert owners[-1][1:] == (False, "watchlist_removed_without_entry_order")


def test_unreadable_lxeh_book_keeps_unknown_owner() -> None:
    strategy, _clock, identities, _owners = _strategy()
    state = strategy.watchlist_state("LXEH")
    state.fanout_segment_id = OPPORTUNITY
    state.flip_owner_opportunity_id = OPPORTUNITY
    state.flip_owner_phase = "unknown"
    state.flip_owner_first_rest_placed = False
    state.flip_owner_evidence_readable = False

    strategy.release_and_drop_symbol("LXEH")

    assert state.flip_owner_phase == "unknown"
    assert strategy.unknown_flip_owner_opportunities() == {"LXEH": OPPORTUNITY}
    assert not any(not active for _symbol, _segment, active, _reason in identities)


@pytest.mark.parametrize(
    ("symbol", "evidence"),
    [
        ("FTFT", "resting_active"),
        ("VSA", "webull_resting_active"),
        ("FTFT", "schwab_fill"),
        ("VSA", "webull_fill"),
        ("FTFT", "position_id"),
    ],
)
def test_ambiguous_removed_symbol_stays_unknown_on_readd(
    symbol: str, evidence: str
) -> None:
    strategy, clock, identities, _owners = _strategy()
    state = strategy.watchlist_state(symbol)
    state.fanout_segment_id = OPPORTUNITY
    state.flip_owner_opportunity_id = OPPORTUNITY
    state.flip_owner_phase = "unknown"
    state.flip_owner_first_rest_placed = False
    state.flip_owner_evidence_readable = True
    state.flip_owner_evidence_at_ms = clock[0]
    if evidence == "resting_active":
        state.resting_active = True
    elif evidence == "webull_resting_active":
        state.webull_resting_active = True
    elif evidence == "schwab_fill":
        state.flip_owner_fill_accounts.add("live:schwab_1m_v2")
    elif evidence == "webull_fill":
        state.flip_owner_fill_accounts.add("live:orb")
    else:
        state.flip_owner_position_ids["live:orb"] = "owned-position"

    strategy.release_and_drop_symbol(symbol)
    readded = strategy.watchlist_state(symbol)

    assert readded is state
    assert readded.flip_owner_phase == "unknown"
    assert strategy._strict_first_rest_admitted(readded, slot="first") is False
    assert not any(not active for _symbol, _segment, active, _reason in identities)


def test_unknown_recovery_warning_is_at_most_once_per_minute(caplog) -> None:
    strategy, clock, _identities, _owners = _strategy()
    state = strategy.watchlist_state("LXEH")
    state.fanout_segment_id = OPPORTUNITY
    state.flip_owner_opportunity_id = OPPORTUNITY
    state.flip_owner_phase = "unknown"
    state.flip_owner_first_rest_placed = True  # an emitted order must stay guarded

    with caplog.at_level(logging.WARNING, logger="project_mai_tai.strategy_core.schwab_1m_v2"):
        for seconds in range(0, 61, 5):
            clock[0] = 1_790_164_000_000 + seconds * 1000
            strategy._recover_unknown_flip_owner(state, (), (), frozenset())

    warnings = [
        record for record in caplog.records
        if "[V2-FLIP-OWNER-RECOVERY] LXEH" in record.message
        and "reason=insufficient_unambiguous_evidence" in record.message
    ]
    assert len(warnings) == 2
    assert state.flip_owner_phase == "unknown"
