from __future__ import annotations

from datetime import datetime, timedelta

from project_mai_tai.strategy_core.feed_retention import (
    FeedRetentionConfig,
    FeedRetentionMetrics,
    FeedRetentionPolicy,
)


def test_feed_retention_moves_active_symbol_into_cooldown_after_structure_break_and_decay() -> None:
    policy = FeedRetentionPolicy(
        FeedRetentionConfig(
            structure_bars=3,
            no_activity_minutes=5,
            cooldown_volume_ratio=0.5,
            cooldown_max_5m_range_pct=1.5,
            resume_hold_bars=2,
            resume_min_5m_range_pct=2.5,
            resume_min_5m_volume_ratio=1.2,
            resume_min_5m_volume_abs=100_000,
            drop_cooldown_minutes=10,
            drop_max_5m_range_pct=1.0,
            drop_max_5m_volume_abs=50_000,
        )
    )
    now = datetime(2026, 4, 17, 10, 0)
    state = policy.promote(
        "EFOI",
        now,
        FeedRetentionMetrics(
            price=7.0,
            vwap=6.8,
            ema20=6.7,
            rolling_5m_volume=300_000,
            rolling_5m_range_pct=4.0,
            bar_timestamp=1_000.0,
        ),
    )

    for idx in range(3):
        state = policy.evaluate(
            state,
            symbol="EFOI",
            now=now + timedelta(minutes=6, seconds=30 * idx),
            is_confirmed=False,
            metrics=FeedRetentionMetrics(
                price=6.2,
                vwap=6.6,
                ema20=6.4,
                rolling_5m_volume=100_000,
                rolling_5m_range_pct=1.0,
                bar_timestamp=1_001.0 + idx,
            ),
        )

    assert state is not None
    assert state.state == "cooldown"
    assert state.blocks_entries() is True
    assert state.keeps_feed() is True


def test_feed_retention_resumes_after_reclaim_and_sustained_expansion() -> None:
    policy = FeedRetentionPolicy(
        FeedRetentionConfig(
            structure_bars=3,
            no_activity_minutes=5,
            cooldown_volume_ratio=0.5,
            cooldown_max_5m_range_pct=1.5,
            resume_hold_bars=2,
            resume_min_5m_range_pct=2.5,
            resume_min_5m_volume_ratio=1.2,
            resume_min_5m_volume_abs=100_000,
            drop_cooldown_minutes=10,
            drop_max_5m_range_pct=1.0,
            drop_max_5m_volume_abs=50_000,
        )
    )
    now = datetime(2026, 4, 17, 10, 0)
    state = policy.promote(
        "EFOI",
        now,
        FeedRetentionMetrics(
            price=7.0,
            vwap=6.8,
            ema20=6.7,
            rolling_5m_volume=300_000,
            rolling_5m_range_pct=4.0,
            bar_timestamp=1_000.0,
        ),
    )
    state.state = "cooldown"
    state.cooldown_started_at = now + timedelta(minutes=6)
    state.state_changed_at = now + timedelta(minutes=6)

    probe = policy.evaluate(
        state,
        symbol="EFOI",
        now=now + timedelta(minutes=12),
        is_confirmed=False,
        metrics=FeedRetentionMetrics(
            price=6.95,
            vwap=6.8,
            ema20=6.75,
            rolling_5m_volume=420_000,
            rolling_5m_range_pct=3.4,
            bar_timestamp=1_010.0,
        ),
    )

    assert probe is not None
    assert probe.state == "resume_probe"
    assert probe.blocks_entries() is True

    active = policy.evaluate(
        probe,
        symbol="EFOI",
        now=now + timedelta(minutes=12, seconds=30),
        is_confirmed=False,
        metrics=FeedRetentionMetrics(
            price=7.05,
            vwap=6.82,
            ema20=6.78,
            rolling_5m_volume=430_000,
            rolling_5m_range_pct=3.2,
            bar_timestamp=1_011.0,
        ),
    )

    assert active is not None
    assert active.state == "active"
    assert active.blocks_entries() is False


def test_feed_retention_drops_dead_tape_after_long_cooldown() -> None:
    policy = FeedRetentionPolicy(
        FeedRetentionConfig(
            structure_bars=3,
            no_activity_minutes=5,
            cooldown_volume_ratio=0.5,
            cooldown_max_5m_range_pct=1.5,
            resume_hold_bars=2,
            resume_min_5m_range_pct=2.5,
            resume_min_5m_volume_ratio=1.2,
            resume_min_5m_volume_abs=100_000,
            drop_cooldown_minutes=10,
            drop_max_5m_range_pct=1.0,
            drop_max_5m_volume_abs=50_000,
        )
    )
    now = datetime(2026, 4, 17, 10, 0)
    state = policy.promote(
        "EFOI",
        now,
        FeedRetentionMetrics(
            price=7.0,
            vwap=6.8,
            ema20=6.7,
            rolling_5m_volume=300_000,
            rolling_5m_range_pct=4.0,
            bar_timestamp=1_000.0,
        ),
    )
    state.state = "cooldown"
    state.cooldown_started_at = now
    state.state_changed_at = now

    dropped = policy.evaluate(
        state,
        symbol="EFOI",
        now=now + timedelta(minutes=11),
        is_confirmed=False,
        metrics=FeedRetentionMetrics(
            price=6.05,
            vwap=6.15,
            ema20=6.10,
            rolling_5m_volume=30_000,
            rolling_5m_range_pct=0.8,
            bar_timestamp=1_020.0,
        ),
    )

    assert dropped is not None
    assert dropped.state == "dropped"
    assert dropped.keeps_feed() is False


def test_feed_retention_enters_degraded_mode_after_four_of_six_score_holds() -> None:
    policy = FeedRetentionPolicy(
        FeedRetentionConfig(
            degraded_enabled=True,
            degraded_warmup_bars=5,
            degraded_enter_score=4,
            degraded_enter_hold_bars=4,
        )
    )
    now = datetime(2026, 4, 17, 10, 0)
    state = policy.promote(
        "ENVB",
        now,
        FeedRetentionMetrics(
            price=4.4,
            ema9=4.35,
            vwap=4.30,
            ema20=4.28,
            rolling_5m_volume=300_000,
            rolling_5m_range_pct=4.0,
            avg_bar_volume_5=60_000,
            avg_bar_volume_20=40_000,
            total_bars=5,
            bar_timestamp=1_000.0,
        ),
    )

    for idx in range(4):
        state = policy.evaluate(
            state,
            symbol="ENVB",
            now=now + timedelta(minutes=1, seconds=30 * idx),
            is_confirmed=False,
            metrics=FeedRetentionMetrics(
                price=4.10,
                ema9=4.18,
                vwap=4.20,
                ema20=4.16,
                rolling_5m_volume=180_000,
                rolling_5m_range_pct=1.2,
                avg_bar_volume_5=18_000,
                avg_bar_volume_20=30_000,
                ema9_falling=True,
                lower_highs_or_closes=True,
                total_bars=20,
                bar_timestamp=1_010.0 + idx,
            ),
        )

    assert state is not None
    assert state.degraded_mode is True
    assert state.degraded_score >= 4
    assert state.degraded_since is not None


def test_feed_retention_exits_degraded_mode_after_looser_recovery_hold() -> None:
    policy = FeedRetentionPolicy(
        FeedRetentionConfig(
            degraded_enabled=True,
            degraded_warmup_bars=5,
            degraded_enter_score=4,
            degraded_enter_hold_bars=4,
            degraded_exit_score=3,
            degraded_exit_hold_bars=4,
        )
    )
    now = datetime(2026, 4, 17, 10, 0)
    state = policy.promote(
        "SST",
        now,
        FeedRetentionMetrics(
            price=2.8,
            ema9=2.75,
            vwap=2.72,
            ema20=2.70,
            rolling_5m_volume=280_000,
            rolling_5m_range_pct=3.0,
            avg_bar_volume_5=56_000,
            avg_bar_volume_20=42_000,
            total_bars=5,
            bar_timestamp=2_000.0,
        ),
    )
    state.degraded_mode = True
    state.degraded_since = now

    for idx in range(4):
        state = policy.evaluate(
            state,
            symbol="SST",
            now=now + timedelta(minutes=5, seconds=30 * idx),
            is_confirmed=False,
            metrics=FeedRetentionMetrics(
                price=2.92,
                ema9=2.88,
                vwap=2.84,
                ema20=2.83,
                rolling_5m_volume=260_000,
                rolling_5m_range_pct=2.8,
                avg_bar_volume_5=52_000,
                avg_bar_volume_20=45_000,
                ema9_rising=True,
                higher_highs_or_closes=True,
                total_bars=30,
                bar_timestamp=2_010.0 + idx,
            ),
        )

    assert state is not None
    assert state.degraded_mode is False
    assert state.degraded_since is None


def test_feed_retention_degraded_overlay_disabled_by_default() -> None:
    policy = FeedRetentionPolicy(FeedRetentionConfig())
    now = datetime(2026, 4, 17, 10, 0)
    state = policy.promote(
        "ENVB",
        now,
        FeedRetentionMetrics(
            price=4.4,
            ema9=4.35,
            vwap=4.30,
            ema20=4.28,
            rolling_5m_volume=300_000,
            rolling_5m_range_pct=4.0,
            avg_bar_volume_5=60_000,
            avg_bar_volume_20=40_000,
            total_bars=25,
            bar_timestamp=1_000.0,
        ),
    )

    for idx in range(6):
        state = policy.evaluate(
            state,
            symbol="ENVB",
            now=now + timedelta(minutes=1, seconds=30 * idx),
            is_confirmed=False,
            metrics=FeedRetentionMetrics(
                price=4.10,
                ema9=4.18,
                vwap=4.20,
                ema20=4.16,
                rolling_5m_volume=180_000,
                rolling_5m_range_pct=1.2,
                avg_bar_volume_5=18_000,
                avg_bar_volume_20=30_000,
                ema9_falling=True,
                lower_highs_or_closes=True,
                total_bars=25,
                bar_timestamp=1_010.0 + idx,
            ),
        )

    assert state is not None
    assert state.degraded_mode is False
    assert state.degraded_score == 0
