from datetime import UTC, datetime, timedelta

from project_mai_tai.backtest.atr_straight_down_profile import (
    ScannerEvent,
    scanner_at_entry,
)


def _event(kind: str, minute: int, *, rank: float | None = None) -> ScannerEvent:
    return ScannerEvent(
        event_type=kind,
        event_at=datetime(2026, 8, 24, 12, minute, tzinfo=UTC),
        confirm_path="momentum" if kind == "CONFIRM" else None,
        rank_score=rank,
        force_watchlist=False,
        price=2.0,
        day_volume=100_000,
        float_used=1_000_000,
        change_pct=20.0,
        reconfirm_seq=0,
    )


def test_scanner_join_uses_confirm_that_opened_active_window() -> None:
    events = [
        _event("CONFIRM", 0, rank=40),
        _event("CONFIRM", 1, rank=50),
        _event("FADE", 10),
    ]

    confirm, removal = scanner_at_entry(
        events, datetime(2026, 8, 24, 12, 5, tzinfo=UTC)
    )

    assert confirm is not None and confirm.rank_score == 40
    assert removal is not None and removal.event_type == "FADE"


def test_scanner_join_uses_reconfirm_after_removal() -> None:
    events = [
        _event("CONFIRM", 0, rank=40),
        _event("FADE", 2),
        _event("CONFIRM", 3, rank=60),
        _event("RETENTION_DROP", 10),
    ]

    confirm, removal = scanner_at_entry(
        events, datetime(2026, 8, 24, 12, 5, tzinfo=UTC)
    )

    assert confirm is not None and confirm.rank_score == 60
    assert removal is not None and removal.event_type == "RETENTION_DROP"


def test_scanner_join_does_not_use_future_confirmation() -> None:
    confirm, removal = scanner_at_entry(
        [_event("CONFIRM", 5, rank=40)],
        datetime(2026, 8, 24, 12, 5, tzinfo=UTC) - timedelta(seconds=1),
    )

    assert confirm is None
    assert removal is None
