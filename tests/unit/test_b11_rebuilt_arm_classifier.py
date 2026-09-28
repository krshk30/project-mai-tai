from datetime import UTC, datetime, timedelta

from scripts.b11_rebuilt_arm_classifier import classify_age, direct_arm, parse_events


def _line(at: datetime, body: str) -> str:
    return f"{at:%Y-%m-%d %H:%M:%S,%f} INFO {body}\n"


def _ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def test_whlr_rebuilt_readd_arm_does_not_label_next_live_arm() -> None:
    old_at = datetime(2026, 9, 25, 15, 24, 25, 962000, tzinfo=UTC)
    live_at = datetime(2026, 9, 25, 15, 25, 3, 177000, tzinfo=UTC)
    old_bar = 1790339880000
    live_bar = _ms(live_at - timedelta(seconds=63))
    arms, places = parse_events(
        [
            _line(old_at, "[V2-DB-SEED-GAP] WHLR dropped 171 of 250 seed bars"),
            _line(old_at, f"[V2-CW-ARM] WHLR armed bar_ts={old_bar} trig=4.8600"),
            _line(old_at, f"[V2-CW-SEED-CAP] WHLR reconstructed armed segment capped arm_bar_ts={old_bar}"),
            _line(old_at + timedelta(seconds=2), "[V2-CW-DISARM] WHLR reason=flip"),
            _line(live_at, f"[V2-CW-ARM] WHLR armed bar_ts={live_bar} trig=5.2000"),
            _line(live_at + timedelta(milliseconds=1), "[V2-RESTING-PLACE] WHLR slot=first trigger=5.20"),
        ]
    )
    assert [(arm.classification, arm.seed_capped) for arm in arms] == [
        ("REBUILT", True),
        ("LIVE", False),
    ]
    assert places[0].arm_line == arms[1].source_line
    assert direct_arm(arms, symbol="WHLR", arm_bar_ts_ms=live_bar) == arms[1]


def test_same_timestamp_order_is_causal_for_readd_churn() -> None:
    at = datetime(2026, 9, 25, 15, 24, 25, 962000, tzinfo=UTC)
    bar = _ms(at - timedelta(seconds=61))
    arms, _ = parse_events(
        [
            _line(at, f"[V2-CW-ARM] GYGY armed bar_ts={bar} trig=2.00"),
            _line(at, "[V2-DB-SEED-GAP] GYGY dropped 10 of 250 seed bars"),
            _line(at, "[V2-CW-DISARM] GYGY reason=flip"),
            _line(at, f"[V2-CW-ARM] GYGY armed bar_ts={bar + 1} trig=2.10"),
        ]
    )
    assert [arm.classification for arm in arms] == ["LIVE", "REBUILT"]


def test_ambiguous_direct_join_and_disarmed_place_are_unknown() -> None:
    at = datetime(2026, 9, 25, 15, 24, 25, tzinfo=UTC)
    bar = _ms(at - timedelta(seconds=60))
    arms, places = parse_events(
        [
            _line(at, f"[V2-CW-ARM] GYGY armed bar_ts={bar} trig=2.00"),
            _line(at + timedelta(seconds=1), "[V2-CW-DISARM] GYGY reason=flip"),
            _line(at + timedelta(seconds=2), "[V2-RESTING-PLACE] GYGY slot=first trigger=2.00"),
            _line(at + timedelta(seconds=3), f"[V2-CW-ARM] GYGY armed bar_ts={bar} trig=2.00"),
        ]
    )
    assert places[0].join_method == "UNKNOWN"
    assert direct_arm(arms, symbol="GYGY", arm_bar_ts_ms=bar) is None


def test_age_boundaries_are_explicit() -> None:
    at = datetime(2026, 9, 25, 15, 24, 25, tzinfo=UTC)
    assert classify_age(at, _ms(at - timedelta(seconds=120)))[1] == "LIVE"
    assert classify_age(at, _ms(at - timedelta(seconds=300)))[1] == "UNKNOWN"
    assert classify_age(at, _ms(at - timedelta(seconds=301)))[1] == "REBUILT"
