from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
import threading
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.models import Base, DashboardSnapshot
from project_mai_tai.fanout_outcome_consumer import (
    OUTCOME_SNAPSHOT_TYPE,
    FanoutOutcome,
    FanoutOutcomeJournal,
    broker_outcome,
)
from project_mai_tai.market_data.schwab_v2_rest_client import Quote
from project_mai_tai.settings import Settings
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    FANOUT_POSITIVE_ZERO_HOLD_MS,
    OHLCVBar,
    SchwabV2Strategy,
)


SEGMENT = 1787846400000


def _factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[DashboardSnapshot.__table__])
    return sessionmaker(bind=engine, expire_on_commit=False)


def _strategy() -> tuple[SchwabV2Strategy, dict[str, str]]:
    strategy = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
        )
    )
    state = strategy.watchlist_state("YYGH")
    state.fanout_segment_id = SEGMENT
    identity = strategy._fanout_identity_metadata(state, source="reactive")
    strategy._claim_fanout_webull(state, identity=identity, reason="test")
    return strategy, identity


def _outcome(
    identity: dict[str, str],
    outcome: str,
    *,
    attempt: str = "attempt-1",
    evidence: str | None = None,
) -> FanoutOutcome:
    return FanoutOutcome(
        record_id=uuid4(),
        created_at=datetime.now(UTC),
        symbol="YYGH",
        segment_id=SEGMENT,
        slot=identity["fanout_slot"],
        slot_id=identity["fanout_slot_id"],
        attempt_id=attempt,
        outcome=outcome,
        evidence_id=evidence or str(uuid4()),
    )


def test_fill_holds_without_reading_virtual_positions_and_is_monotonic() -> None:
    strategy, identity = _strategy()
    state = strategy.watchlist_state("YYGH")
    state.position_qty = 0

    assert strategy.apply_fanout_outcome(_outcome(identity, "filled")) == "consumed"
    assert state.fanout_webull_claimed is True
    assert state.fanout_claim_outcome == "filled"

    # A stale terminal observation cannot turn an authoritative fill back into a free claim.
    assert strategy.apply_fanout_outcome(_outcome(identity, "cancelled")) == "filled_wins"
    assert state.fanout_webull_claimed is True
    assert state.fanout_claim_outcome == "filled"


def test_webull_fill_then_union_zero_keeps_the_claim_held() -> None:
    """The Webull fill can terminalize its own intent before position visibility catches up."""

    strategy, identity = _strategy()
    state = strategy.watchlist_state("YYGH")
    state.position_qty = 1
    state.position_qty_held = 0

    assert strategy.apply_fanout_outcome(_outcome(identity, "filled")) == "consumed"
    strategy.update_position("YYGH", 0, held_qty=0)

    assert state.fanout_webull_claimed is True
    assert state.fanout_claim_outcome == "filled"


def test_virtual_clear_false_zero_on_held_primary_does_not_release_positive_evidence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient primary-position zero cannot outrank durable Webull evidence."""

    caplog.set_level("INFO")
    strategy, identity = _strategy()
    now = [SEGMENT]
    strategy._now_ms = lambda: now[0]
    state = strategy.watchlist_state("YYGH")
    state.position_qty = 1
    state.position_qty_held = 1
    assert strategy.apply_fanout_outcome(_outcome(identity, "working")) == "held"

    strategy.update_position("YYGH", 0, held_qty=0)

    assert state.fanout_webull_claimed is True
    assert state.fanout_claim_outcome == "held"
    assert "release_allowed=0 released=0 held=1" in caplog.text
    assert "outcome=held spurious=0" in caplog.text

    # The primary row returns before the measured bound. The hold is cancelled, while the
    # positive Webull claim remains authoritative.
    now[0] += 10_000
    strategy.update_position("YYGH", 1, held_qty=1)
    assert state.fanout_webull_claimed is True
    assert state.fanout_zero_hold_started_ms == 0
    assert "restored=1 hold_cancelled=1" in caplog.text


def test_spurious_union_zero_cannot_release_a_durable_fill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The existing spurious classification must gate release, not merely annotate it."""

    caplog.set_level("INFO")
    strategy, identity = _strategy()
    state = strategy.watchlist_state("YYGH")
    state.position_qty = 1
    state.position_qty_held = 0
    assert strategy.apply_fanout_outcome(_outcome(identity, "filled")) == "consumed"

    strategy.update_position("YYGH", 0, held_qty=0)

    assert state.fanout_webull_claimed is True
    assert state.fanout_claim_slot_id == identity["fanout_slot_id"]
    assert "release_allowed=0 released=0 held=1" in caplog.text
    assert "outcome=filled spurious=1" in caplog.text


@pytest.mark.parametrize(
    ("elapsed_ms", "expected_claimed", "expected_release_allowed"),
    [
        (FANOUT_POSITIVE_ZERO_HOLD_MS - 1, True, 0),
        # DB2 (2026-08-31): the expired hold used to RELEASE here — asserting the pre-#843
        # premise that union-zero could mean "a Webull fill terminalized its own intent".
        # Post-#843 the union is Schwab-scoped and a FILLED claim is a live position at its
        # own venue: expiry now VETOES the release (claim kept, timer cleared). The release
        # polarity for a non-filled claim lives in
        # test_v2_fanout_zero_hold_mirror_scope.py::test_expired_hold_releases_a_held_claim.
        (FANOUT_POSITIVE_ZERO_HOLD_MS, True, 0),
    ],
)
def test_positive_evidence_union_zero_hold_is_bounded(
    caplog: pytest.LogCaptureFixture,
    elapsed_ms: int,
    expected_claimed: bool,
    expected_release_allowed: int,
) -> None:
    """One elapsed-time variable reaches both hold and legitimate release polarities."""

    caplog.set_level("INFO")
    strategy, identity = _strategy()
    now = [SEGMENT]
    strategy._now_ms = lambda: now[0]
    persisted: list[tuple[str, str]] = []
    strategy.configure_fanout_outcome_persistence(
        lambda _metadata, _symbol, outcome, reason: persisted.append((outcome, reason))
    )
    state = strategy.watchlist_state("YYGH")
    state.position_qty = 1
    state.position_qty_held = 1
    assert strategy.apply_fanout_outcome(_outcome(identity, "filled")) == "consumed"

    strategy.update_position("YYGH", 0, held_qty=0)
    now[0] += elapsed_ms
    strategy.update_position("YYGH", 0, held_qty=0)

    assert state.fanout_webull_claimed is expected_claimed
    assert state.cw_v2_emit_claimed is False
    assert state.cw_v2_bars_since_exit == 0
    assert f"release_allowed={expected_release_allowed}" in caplog.text
    if expected_claimed:
        assert state.fanout_claim_outcome == "filled"
        assert "hold_expired=0" in caplog.text
        assert persisted == []
        if elapsed_ms >= FANOUT_POSITIVE_ZERO_HOLD_MS:
            # The expiry was reached and the release was VETOED, not merely not-yet-due.
            assert "[V2-FANOUT-CLAIM-ZERO-HOLD-VETOED]" in caplog.text
            assert state.fanout_zero_hold_started_ms == 0
    else:
        assert state.fanout_claim_outcome == "released"
        assert "reason=schwab_union_zero_positive_evidence_hold_expired" in caplog.text
        assert "hold_expired=1" in caplog.text
        assert persisted == [
            ("strategy_release", "schwab_union_zero_positive_evidence_hold_expired")
        ]


def test_positive_terminal_no_fill_releases_only_the_exact_attempt() -> None:
    strategy, identity = _strategy()
    state = strategy.watchlist_state("YYGH")
    strategy.apply_fanout_outcome(_outcome(identity, "queued", attempt="attempt-2"))

    assert strategy.apply_fanout_outcome(
        _outcome(identity, "cancelled", attempt="attempt-1")
    ) == "wrong_slot"
    assert state.fanout_webull_claimed is True

    assert strategy.apply_fanout_outcome(
        _outcome(identity, "cancelled", attempt="attempt-2")
    ) == "released"
    assert state.fanout_webull_claimed is False


def test_terminal_for_another_slot_cannot_release_the_current_claim() -> None:
    strategy, identity = _strategy()
    other = dict(identity)
    other["fanout_slot"] = "resting"
    other["fanout_slot_id"] = str(uuid4())

    assert strategy.apply_fanout_outcome(_outcome(other, "rejected_venue")) == "wrong_slot"
    assert strategy.watchlist_state("YYGH").fanout_webull_claimed is True


def test_positive_evidence_for_another_slot_cannot_hold_the_current_claim() -> None:
    strategy, identity = _strategy()
    state = strategy.watchlist_state("YYGH")
    current_slot_id = state.fanout_claim_slot_id
    other = dict(identity)
    other["fanout_slot"] = "resting"
    other["fanout_slot_id"] = str(uuid4())

    assert strategy.apply_fanout_outcome(_outcome(other, "working")) == "wrong_slot"
    assert strategy.apply_fanout_outcome(_outcome(other, "filled")) == "wrong_slot"
    assert state.fanout_claim_slot_id == current_slot_id
    assert state.fanout_claim_outcome == "queued"


def test_same_evidence_is_idempotent() -> None:
    strategy, identity = _strategy()
    row = _outcome(identity, "submitted", evidence="event-1")
    assert strategy.apply_fanout_outcome(row) == "held"
    assert strategy.watchlist_state("YYGH").fanout_claim_attempt_id == "attempt-1"
    assert strategy.apply_fanout_outcome(row) == "duplicate"


def _resting_cross_strategy() -> tuple[SchwabV2Strategy, object, list[int]]:
    strategy = SchwabV2Strategy(
        Settings(
            strategy_schwab_1m_v2_confirmed_window_enabled=True,
            strategy_schwab_1m_v2_cw_v2_enabled=True,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
            strategy_schwab_1m_v2_webull_fanout_claim_grace_secs=30.0,
        )
    )
    now = [SEGMENT]
    strategy._now_ms = lambda: now[0]
    strategy._resting_session_is_eh = lambda _now=None: False
    state = strategy.watchlist_state("YYGH")
    state.resting_active = True
    state.resting_level = 1.25
    state.bars.append(
        OHLCVBar(
            timestamp_ms=SEGMENT,
            open=1.20,
            high=1.26,
            low=1.19,
            close=1.25,
            volume=25_000,
        )
    )
    return strategy, state, now


def test_absent_positive_evidence_releases_after_grace_and_can_emit_again() -> None:
    strategy, state, now = _resting_cross_strategy()
    quote = Quote("YYGH", 1.24, 1.26, 1.25, SEGMENT, 0)

    strategy._fanout_rth_resting_cross(state, quote)
    first = strategy.drain_webull_fanout_intents()
    assert len(first) == 1
    assert state.fanout_claim_outcome == "queued"

    now[0] += 31_000
    strategy._fanout_rth_resting_cross(state, quote)

    assert len(strategy.drain_webull_fanout_intents()) == 1
    assert state.fanout_webull_claimed is True


def test_positive_working_evidence_holds_beyond_grace_and_suppresses_duplicate() -> None:
    strategy, state, now = _resting_cross_strategy()
    quote = Quote("YYGH", 1.24, 1.26, 1.25, SEGMENT, 0)
    strategy._fanout_rth_resting_cross(state, quote)
    strategy.drain_webull_fanout_intents()
    identity = {
        "fanout_slot": state.fanout_claim_slot,
        "fanout_slot_id": state.fanout_claim_slot_id,
    }
    assert strategy.apply_fanout_outcome(_outcome(identity, "working")) == "held"

    now[0] += 31_000
    strategy._fanout_rth_resting_cross(state, quote)

    assert strategy.drain_webull_fanout_intents() == []
    assert state.fanout_webull_claimed is True
    assert state.fanout_claim_outcome == "held"


def test_broker_rejection_provenance_has_both_polarities() -> None:
    assert broker_outcome("rejected", "client") == "rejected_client_abort"
    assert broker_outcome("rejected", "broker") == "rejected_venue"
    assert broker_outcome("rejected", "unknown") == "rejected_unclassified"
    assert broker_outcome("filled", "unknown") == "filled"


def test_journal_replays_only_the_active_segment_and_advances_a_durable_cursor() -> None:
    factory = _factory()
    journal = FanoutOutcomeJournal(factory)
    identity = {
        "fanout_leg": "webull",
        "fanout_segment_id": str(SEGMENT),
        "fanout_slot": "reclaim",
        "fanout_slot_id": str(uuid4()),
        "fanout_attempt_id": "attempt-1",
    }
    stale = {**identity, "fanout_segment_id": str(SEGMENT - 1), "fanout_slot_id": str(uuid4())}
    assert journal.record(
        metadata=stale,
        symbol="YYGH",
        outcome="filled",
        evidence_id="stale",
    )
    assert journal.record(
        metadata=identity,
        symbol="YYGH",
        outcome="submitted",
        evidence_id="active",
    )

    applied: list[FanoutOutcome] = []
    assert journal.bootstrap(applied.append, active_segments={"YYGH": SEGMENT}) == 1
    assert [row.evidence_id for row in applied] == ["active"]
    assert journal.poll(applied.append) == 0

    with factory() as session:
        assert len(
            session.scalars(
                select(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE
                )
            ).all()
        ) == 2


def test_non_webull_or_incomplete_identity_never_enters_the_outcome_journal() -> None:
    journal = FanoutOutcomeJournal(_factory())
    assert not journal.record(
        metadata={"fanout_segment_id": str(SEGMENT), "fanout_slot_id": "x"},
        symbol="YYGH",
        outcome="queued",
        evidence_id="bad",
    )


@pytest.mark.asyncio
async def test_poll_reads_and_cursors_off_thread_but_applies_on_strategy_thread() -> None:
    """A DB worker must never mutate SymbolState beside quote/bar callbacks."""

    strategy, identity = _strategy()
    row = _outcome(identity, "submitted")
    event_loop_thread = threading.get_ident()
    threads: dict[str, int] = {}

    class _Journal:
        def read_pending(self) -> list[FanoutOutcome]:
            threads["read"] = threading.get_ident()
            return [row]

        def advance(self, rows: list[FanoutOutcome]) -> int:
            threads["advance"] = threading.get_ident()
            return len(rows)

    original_apply = strategy.apply_fanout_outcome

    def _apply(record: FanoutOutcome) -> str:
        threads["apply"] = threading.get_ident()
        return original_apply(record)

    strategy.apply_fanout_outcome = _apply  # type: ignore[method-assign]
    harness = SimpleNamespace(
        fanout_outcome_journal=_Journal(),
        strategy=strategy,
        _fanout_outcome_evaluations=0,
    )

    await SchwabV2BotService._fanout_outcome_pass(harness)  # type: ignore[arg-type]

    assert threads["read"] != event_loop_thread
    assert threads["apply"] == event_loop_thread
    assert threads["advance"] != event_loop_thread
    assert harness._fanout_outcome_evaluations == 1
