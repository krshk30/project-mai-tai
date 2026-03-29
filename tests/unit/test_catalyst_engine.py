from __future__ import annotations

from datetime import UTC, datetime

from project_mai_tai.strategy_core.catalyst import CatalystEngine
from project_mai_tai.strategy_core.config import MomentumConfirmedConfig
from project_mai_tai.strategy_core.momentum_confirmed import MomentumConfirmedScanner
from project_mai_tai.strategy_core.models import ReferenceData


def test_catalyst_engine_uses_previous_market_close_window() -> None:
    engine = CatalystEngine(
        api_key="test-key",
        secret_key="test-secret",
        now_provider=lambda: datetime(2026, 3, 30, 13, 0, tzinfo=UTC),
    )

    result = engine._classify_symbol_articles(
        "UGRO",
        [
            {
                "created_at": "2026-03-27T19:30:00Z",
                "headline": "UGRO announces pre-close partnership tease",
                "summary": "Released before the market close and should be excluded.",
                "url": "https://example.com/too-early",
            },
            {
                "created_at": "2026-03-27T20:05:00Z",
                "headline": "UGRO wins contract with regional hospital network",
                "summary": "The company secured a multiyear purchase order.",
                "url": "https://example.com/contract",
            },
            {
                "created_at": "2026-03-28T13:15:00Z",
                "headline": "UGRO signs strategic partnership for product distribution",
                "summary": "Management announced a strategic partnership expansion.",
                "url": "https://example.com/partnership",
            },
            {
                "created_at": "2026-03-29T23:05:00Z",
                "headline": "UGRO enters definitive agreement to be acquired",
                "summary": "The company entered into a definitive agreement with an acquirer.",
                "url": "https://example.com/acquired",
            },
        ],
    )

    assert result["article_count"] == 3
    assert result["window_start_label"] == "03/27 04:00PM ET"
    assert result["has_real_catalyst"] is True
    assert result["path_a_eligible"] is True
    assert result["sentiment"] == "bullish"
    assert result["headline"] == "UGRO enters definitive agreement to be acquired"


def test_catalyst_engine_rejects_roundup_only_news_for_path_a() -> None:
    engine = CatalystEngine(
        api_key="test-key",
        secret_key="test-secret",
        now_provider=lambda: datetime(2026, 3, 30, 13, 0, tzinfo=UTC),
    )

    result = engine._classify_symbol_articles(
        "UGRO",
        [
            {
                "created_at": "2026-03-27T20:05:00Z",
                "headline": "Why UGRO stock jumped on unusual volume Friday",
                "summary": "Shares rose in premarket movers coverage amid heavy volume.",
                "url": "https://example.com/roundup",
            }
        ],
    )

    assert result["article_count"] == 1
    assert result["has_real_catalyst"] is False
    assert result["is_generic_roundup"] is True
    assert result["path_a_eligible"] is False
    assert result["sentiment"] == "neutral"


def test_confirmed_scanner_path_a_requires_strict_catalyst_eligibility() -> None:
    reference = {"UGRO": ReferenceData(shares_outstanding=50_000, avg_daily_volume=390_000)}
    snapshot_lookup = {}
    alerts = [
        {
            "ticker": "UGRO",
            "type": "VOLUME_SPIKE",
            "price": 2.20,
            "volume": 12_000,
            "time": "09:55:00 AM ET",
            "bid": 2.19,
            "ask": 2.20,
            "float": 50_000,
        },
        {
            "ticker": "UGRO",
            "type": "SQUEEZE_5MIN",
            "price": 2.35,
            "volume": 18_000,
            "time": "10:00:00 AM ET",
            "bid": 2.34,
            "ask": 2.35,
            "float": 50_000,
            "details": {"change_pct": 6.0},
        },
    ]

    strict_scanner = MomentumConfirmedScanner(
        MomentumConfirmedConfig(confirmed_min_volume=1_000, confirmed_max_float=1_000_000)
    )
    strict_scanner.set_catalyst_engine(
        lambda ticker: {
            "ticker": ticker,
            "sentiment": "bullish",
            "ai_confidence": 0.99,
            "path_a_eligible": False,
            "reason": "Only generic roundup coverage in the current news window.",
        }
    )

    assert strict_scanner.process_alerts(alerts, reference, snapshot_lookup) == []

    eligible_scanner = MomentumConfirmedScanner(
        MomentumConfirmedConfig(confirmed_min_volume=1_000, confirmed_max_float=1_000_000)
    )
    eligible_scanner.set_catalyst_engine(
        lambda ticker: {
            "ticker": ticker,
            "sentiment": "bullish",
            "ai_confidence": 0.91,
            "path_a_eligible": True,
            "catalyst": "DEAL/CONTRACT",
            "headline": "UGRO wins contract with regional hospital network",
            "published": "03/27 05:05PM ET",
            "url": "https://example.com/contract",
            "article_count": 2,
            "real_catalyst_article_count": 2,
            "freshness_minutes": 55,
            "has_real_catalyst": True,
            "is_generic_roundup": False,
            "reason": "Bullish DEAL/CONTRACT catalyst across 2 article(s), latest 55m old.",
        }
    )

    confirmed = eligible_scanner.process_alerts(alerts, reference, snapshot_lookup)

    assert len(confirmed) == 1
    assert confirmed[0]["confirmation_path"] == "PATH_A_NEWS"
    assert confirmed[0]["path_a_eligible"] is True
    assert confirmed[0]["article_count"] == 2
