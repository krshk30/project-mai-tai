from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from project_mai_tai.falseflip1 import (
    EntryBarClose,
    EntryIdentity,
    classified_exit_reason,
    classify_entry,
    false_episode_proven_closed,
)
from project_mai_tai.fanout_identity import fanout_slot_id
from tests.unit.test_v2_retry_one import (
    _close_primary,
    _fill_primary,
    _place_first,
    _strategy,
)


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "falseflip1_study", ROOT / "scripts" / "falseflip1_step0.py")
assert SPEC and SPEC.loader
STUDY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STUDY)
REPLAY = json.loads((ROOT / "docs/review-artifacts/falseflip1/"
                     "STEP0_REPLAY_2026-10-08.json").read_text())
CASES = REPLAY["cases"]
FALSE_SLOTS = sorted({row["slot"] for row in CASES
                      if row["classification"] == "FALSE_FLIP"})
BOUND_FALSE = [row for row in CASES if row["classification"] == "FALSE_FLIP"
               and row["managed_row_id"]
               and row["managed_entry_order_id"] == row["order_id"]
               and row["managed_entry_client_order_id"] == row["fill_client_order_id"]
               and row["managed_account"] == row["account"]
               and row["managed_symbol"] == row["symbol"]]
UNBOUND_FALSE = [row for row in CASES if row["classification"] == "FALSE_FLIP"
                 and row not in BOUND_FALSE]
REAL_KEYS = {
    ("IPDN", "2026-10-06T15:01:41+00:00"),
    ("IPDN", "2026-10-06T16:42:02+00:00"),
    ("MOBX", "2026-10-06T18:21:14+00:00"),
    ("IPDN", "2026-10-06T18:32:18+00:00"),
    ("APUS", "2026-10-06T18:49:49+00:00"),
}
REAL_CASES = [row for row in CASES if row["account"] == "live:schwab_1m_v2"
              and (row["symbol"], row["filled_at"]) in REAL_KEYS]


def _evidence(row: dict) -> tuple[EntryIdentity, EntryBarClose]:
    probe = row["probes"][0]
    observed = datetime.strptime(probe["line"][:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC)
    identity = EntryIdentity(
        symbol=row["symbol"], account=row["account"],
        managed_row_id=row["managed_row_id"] or "",
        entry_order_id=row["managed_entry_order_id"] or "",
        entry_client_order_id=row["managed_entry_client_order_id"] or "",
        fill_order_id=row["order_id"], fill_client_order_id=row["fill_client_order_id"],
        fill_ms=int(datetime.fromisoformat(row["filled_at"]).timestamp() * 1000),
        opportunity_id=int(row["segment"]), slot_id=row["slot"],
        managed_account=row["managed_account"] or "",
        managed_symbol=row["managed_symbol"] or "", fill_account=row["account"],
        fill_symbol=row["symbol"],
    )
    bar = EntryBarClose(row["symbol"], row["bar_ms"], int(observed.timestamp() * 1000),
                        Decimal(probe["close"]), Decimal(probe["trail"]), probe["state"])
    return identity, bar


@pytest.mark.parametrize("slot", FALSE_SLOTS)
def test_recorded_seventeen_false_opportunities_use_actual_entry_bar_probe(slot: str) -> None:
    rows = [row for row in CASES if row["slot"] == slot]
    assert rows
    for row in rows:
        assert row["classification"] == "FALSE_FLIP"
        probe = row["probes"][0]
        assert probe["state"] == "short"
        assert Decimal(probe["close"]) < Decimal(probe["trail"])
        assert probe["minute"] == row["bar_ms"]
        following = row["next_buy"]
        assert following and following["minute"] > row["bar_ms"]


@pytest.mark.parametrize("row", BOUND_FALSE, ids=lambda row: row["fill_id"])
def test_bound_recorded_false_entry_classifies_without_mutating_exit(row: dict) -> None:
    identity, bar = _evidence(row)
    result = classify_entry(identity, bar)
    assert result.kind == "FALSE_FLIP"
    assert result.identity == identity and result.bar == bar
    assert classified_exit_reason(result, "CW_HARD_STOP") == "false_flip_cw_hard_stop"
    assert result.as_payload()["managed_row_id"] == row["managed_row_id"]


@pytest.mark.parametrize("row", UNBOUND_FALSE, ids=lambda row: row["fill_id"])
def test_recorded_unbound_false_colour_never_authorizes_a_runtime_refund(row: dict) -> None:
    identity, bar = _evidence(row)
    result = classify_entry(identity, bar)
    assert result.kind == "UNKNOWN"
    assert not false_episode_proven_closed((result,), filled_accounts=frozenset({row["account"]}),
                                          closed_rows=frozenset({(row["account"], identity.managed_row_id)}))


@pytest.mark.parametrize("row", REAL_CASES, ids=lambda row: row["fill_id"])
def test_five_recorded_real_controls_keep_classification_and_exit_mechanism(row: dict) -> None:
    identity, bar = _evidence(row)
    result = classify_entry(identity, bar)
    assert result.kind == "REAL_FLIP"
    assert classified_exit_reason(result, "CW_HARD_STOP") == "CW_HARD_STOP"
    assert classified_exit_reason(result, "CONFIRMATION_EXIT") == "CONFIRMATION_EXIT"


@pytest.mark.parametrize("row", REAL_CASES, ids=lambda row: row["fill_id"])
def test_current_main_five_real_closes_still_prevent_a_second_rest(row: dict) -> None:
    strategy, clock, _ = _strategy(enabled=True, max_retries=0)
    clock[0] = int(datetime.fromisoformat(row["filled_at"]).timestamp() * 1000)
    state, _ = _place_first(strategy, clock, row["symbol"])
    _fill_primary(strategy, clock, row["symbol"], row["managed_row_id"])
    probe = row["probes"][0]
    state.atr_state = state.atr_prev_state = "long"
    strategy._cw_v2_track(state, {"flip": "BUY", "state": "long",
                                 "trail": float(probe["trail"]), "flip_level": float(probe["trail"])})
    _close_primary(strategy, clock, row["symbol"], row["managed_row_id"], "OCO_RESOLVED_FLAT")
    strategy._queue_resting_place(state, float(probe["trail"]), slot="first")
    assert strategy.drain_pending_intents() == []
    assert state.flip_owner_phase in {"bound", "consumed"}


def test_disclosed_denominators_and_unmeasured_legs_are_not_squeezed_to_62() -> None:
    assert len(CASES) == 90 and len(FALSE_SLOTS) == 17
    assert REPLAY["distinct_slots"] == 61
    assert REPLAY["classification_counts"] == {"REAL_FLIP": 64, "FALSE_FLIP": 24, "UNMEASURED": 2}
    assert len(REAL_CASES) == 5
    assert len(BOUND_FALSE) == 8 and len(UNBOUND_FALSE) == 16


@pytest.mark.parametrize("change", [
    {"managed_row_id": ""}, {"entry_order_id": "foreign-order"},
    {"entry_client_order_id": "foreign-client"}, {"opportunity_id": 0},
    {"slot_id": "foreign-slot"}, {"account": ""},
    {"managed_account": "foreign-account"}, {"fill_account": "foreign-account"},
    {"managed_symbol": "FOREIGN"}, {"fill_symbol": "FOREIGN"},
])
def test_unbound_or_foreign_entry_never_classifies_as_refundable(change: dict) -> None:
    identity, bar = _evidence(BOUND_FALSE[-1])
    assert classify_entry(replace(identity, **change), bar).kind == "UNKNOWN"


@pytest.mark.parametrize("change", [
    {"bar_ms": 1791467940000}, {"observed_at_ms": 1791468000001},
    {"observation_phase": "replay"}, {"state": "unknown"},
    {"close": Decimal("NaN")}, {"trail": Decimal("Infinity")},
    {"trail": Decimal("0")},
])
def test_wrong_unclosed_historical_or_unreadable_bar_fails_closed(change: dict) -> None:
    row = next(row for row in BOUND_FALSE if row["symbol"] == "FLYE")
    identity, bar = _evidence(row)
    assert classify_entry(identity, replace(bar, **change)).kind == "UNKNOWN"


def test_short_state_at_or_above_the_line_is_not_a_proven_false_flip() -> None:
    identity, bar = _evidence(BOUND_FALSE[-1])
    assert classify_entry(identity, replace(bar, close=bar.trail)).kind == "UNKNOWN"
    assert classify_entry(identity, replace(bar, close=bar.trail + Decimal("0.01"))).kind == "UNKNOWN"


def test_synthetic_mixed_sibling_and_still_held_sibling_do_not_prove_false_close() -> None:
    identity, bar = _evidence(BOUND_FALSE[-1])
    first = classify_entry(identity, bar)
    other_identity = replace(identity, account="synthetic-other-account", managed_row_id="synthetic-row",
                             managed_account="synthetic-other-account", fill_account="synthetic-other-account")
    other = classify_entry(other_identity, replace(bar, state="long"))
    accounts = frozenset({identity.account, other_identity.account})
    rows = frozenset({(identity.account, identity.managed_row_id),
                      (other_identity.account, other_identity.managed_row_id)})
    assert not false_episode_proven_closed((first, other), filled_accounts=accounts, closed_rows=rows)
    other = classify_entry(other_identity, bar)
    assert not false_episode_proven_closed((first, other), filled_accounts=accounts,
                                          closed_rows=frozenset({(identity.account, identity.managed_row_id)}))
    assert false_episode_proven_closed((first, other), filled_accounts=accounts, closed_rows=rows)
    assert not false_episode_proven_closed((first,), filled_accounts=accounts, closed_rows=rows)
    assert not false_episode_proven_closed((first, first), filled_accounts=frozenset({identity.account}),
                                          closed_rows=rows)


def test_synthetic_different_entry_bars_same_slot_count_as_one_episode_proof() -> None:
    identity, bar = _evidence(BOUND_FALSE[-1])
    other_identity = replace(identity, account="synthetic-other-account", managed_row_id="synthetic-row",
                             fill_ms=identity.fill_ms + 60000, managed_account="synthetic-other-account",
                             fill_account="synthetic-other-account")
    other_bar = replace(bar, bar_ms=bar.bar_ms + 60000, observed_at_ms=bar.observed_at_ms + 60000)
    first, second = classify_entry(identity, bar), classify_entry(other_identity, other_bar)
    assert false_episode_proven_closed((first, second),
        filled_accounts=frozenset({identity.account, other_identity.account}),
        closed_rows=frozenset({(identity.account, identity.managed_row_id),
                              (other_identity.account, other_identity.managed_row_id)}))
    assert not false_episode_proven_closed((first, replace(second, kind="UNKNOWN")),
        filled_accounts=frozenset({identity.account, other_identity.account}),
        closed_rows=frozenset({(identity.account, identity.managed_row_id),
                              (other_identity.account, other_identity.managed_row_id)}))


@pytest.mark.parametrize("mechanism", ["CW_HARD_STOP", "CONFIRMATION_EXIT", "OCO_RESOLVED_FLAT"])
def test_current_main_zero_retry_budget_consumes_preflip_close_control(mechanism: str) -> None:
    strategy, clock, writes = _strategy(enabled=True, max_retries=0)
    state, _ = _place_first(strategy, clock, "BASELINE_FALSE")
    state.atr_state = state.atr_prev_state = "short"
    _fill_primary(strategy, clock, "BASELINE_FALSE", "controlled-bound-row")
    _close_primary(strategy, clock, "BASELINE_FALSE", "controlled-bound-row", mechanism)
    assert state.flip_owner_phase == "consumed"
    assert state.retry_one_closes_in_segment == 1
    assert writes[-1][2] == 1
    strategy._queue_resting_place(state, 3.006326, slot="first")
    assert strategy.drain_pending_intents() == []


def test_study_never_uses_next_sessions_buy_as_same_segment_follow_up() -> None:
    ts = 1791468000000
    filled = datetime.fromtimestamp((ts + 1000) / 1000, UTC).isoformat()
    slot = fanout_slot_id(strategy_code="schwab_1m_v2", symbol="CONTROL",
                          segment_id=ts - 60000, slot="resting")
    fill = dict(id="controlled", side="buy", symbol="CONTROL", filled_at=filled,
                order_id="controlled-order", client_order_id="controlled-client", account="controlled-account",
                order_payload={"fanout_slot_id": slot, "fanout_segment_id": ts - 60000})
    text = (f"2026-10-08 14:01:00,000 [V2-ATR-PROBE] sym=CONTROL ts_ms={ts} "
            "close=2 trail=3 state=short age=3 flip=none\n"
            f"2026-10-09 14:01:00,000 [V2-ATR-PROBE] sym=CONTROL ts_ms={ts + 86400000} "
            "close=4 trail=3 state=long age=0 flip=BUY\n")
    result = STUDY.assess({"as_of": filled, "rows": [fill]}, {"rows": []}, text)
    assert result["cases"][0]["next_buy"] is None
