from __future__ import annotations

from datetime import UTC, datetime

from project_mai_tai.strategy_core.catalyst import CatalystConfig, CatalystEngine
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
    assert result["catalyst_status"] == "generic_only"
    assert "does not qualify for Path A" in result["reason"]


def test_catalyst_engine_surfaces_no_articles_reason_for_path_a_miss() -> None:
    engine = CatalystEngine(
        api_key="test-key",
        secret_key="test-secret",
        now_provider=lambda: datetime(2026, 4, 14, 11, 2, tzinfo=UTC),
    )

    result = engine._classify_symbol_articles("BTBD", [])

    assert result["article_count"] == 0
    assert result["news_fetch_status"] == "ok"
    assert result["catalyst_status"] == "no_articles"
    assert result["path_a_eligible"] is False
    assert "No company-specific Alpaca news article has been returned yet for BTBD" in result["reason"]


def test_catalyst_engine_retries_empty_news_results_quickly() -> None:
    current_time = datetime(2026, 4, 14, 11, 1, tzinfo=UTC)

    class FakeCatalystEngine(CatalystEngine):
        def __init__(self) -> None:
            super().__init__(
                api_key="test-key",
                secret_key="test-secret",
                config=CatalystConfig(cache_ttl_minutes=15, empty_cache_ttl_seconds=60),
                now_provider=lambda: current_time,
            )
            self.fetch_calls = 0

        def _fetch_alpaca_articles_by_symbol(
            self,
            tickers: list[str],
        ) -> tuple[dict[str, list[dict[str, object]]], set[str]]:
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return ({ticker: [] for ticker in tickers}, set())
            return (
                {
                    ticker: [
                        {
                            "created_at": "2026-04-14T11:01:45Z",
                            "headline": "BTBD wins contract with regional hospital network",
                            "summary": "The company secured a multiyear purchase order.",
                            "url": "https://example.com/btbd-contract",
                            "symbols": [ticker],
                        }
                    ]
                    for ticker in tickers
                },
                set(),
            )

    engine = FakeCatalystEngine()

    first = engine.get_catalyst("BTBD")
    assert first["catalyst_status"] == "no_articles"
    assert engine.fetch_calls == 1

    current_time = datetime(2026, 4, 14, 11, 1, 30, tzinfo=UTC)
    second = engine.get_catalyst("BTBD")
    assert second["catalyst_status"] == "no_articles"
    assert engine.fetch_calls == 1

    current_time = datetime(2026, 4, 14, 11, 2, 2, tzinfo=UTC)
    third = engine.get_catalyst("BTBD")
    assert third["has_real_catalyst"] is True
    assert third["article_count"] == 1
    assert engine.fetch_calls == 2


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


def test_catalyst_engine_attaches_ai_shadow_fields_without_promoting_rule_result() -> None:
    class FakeAiEvaluator:
        def evaluate(self, *, ticker: str, articles: list[dict[str, object]], rule_result: dict[str, object]) -> dict[str, object]:
            assert ticker == "ROLR"
            assert len(articles) == 1
            assert rule_result["path_a_eligible"] is False
            return {
                "status": "ok",
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "direction": "bullish",
                "category": "DEAL/CONTRACT",
                "confidence": 0.93,
                "has_real_catalyst": True,
                "is_generic_roundup": False,
                "is_company_specific": True,
                "path_a_eligible": True,
                "reason": "AI sees a fresh company-specific partnership catalyst.",
                "headline_basis": "High Roller inks Crypto.com deal",
                "positive_phrases": ["inks deal", "launch", "new revenue streams"],
            }

    engine = CatalystEngine(
        api_key="test-key",
        secret_key="test-secret",
        now_provider=lambda: datetime(2026, 4, 14, 12, 30, tzinfo=UTC),
        ai_evaluator=FakeAiEvaluator(),
        promote_ai_result=False,
    )

    result = engine._classify_symbol_articles(
        "ROLR",
        [
            {
                "created_at": "2026-04-14T12:12:54Z",
                "headline": "High Roller inks Crypto.com deal to launch U.S. event-based prediction markets",
                "summary": "The company said the agreement expands new revenue streams.",
                "url": "https://example.com/rolr",
            }
        ],
    )

    assert result["path_a_eligible"] is False
    assert result["ai_shadow_status"] == "ok"
    assert result["ai_shadow_category"] == "DEAL/CONTRACT"
    assert result["ai_shadow_path_a_eligible"] is True
    assert result["ai_shadow_reason"] == "AI sees a fresh company-specific partnership catalyst."
    assert result["ai_shadow_positive_phrases"] == ["inks deal", "launch", "new revenue streams"]


def test_catalyst_engine_can_promote_ai_shadow_result_when_enabled() -> None:
    class FakeAiEvaluator:
        def evaluate(self, *, ticker: str, articles: list[dict[str, object]], rule_result: dict[str, object]) -> dict[str, object]:
            del ticker, articles, rule_result
            return {
                "status": "ok",
                "provider": "openai",
                "model": "gpt-4.1-mini",
                "direction": "bullish",
                "category": "DEAL/CONTRACT",
                "confidence": 0.91,
                "has_real_catalyst": True,
                "is_generic_roundup": False,
                "is_company_specific": True,
                "path_a_eligible": True,
                "reason": "AI sees a qualifying contract-style catalyst.",
                "headline_basis": "Company inks defense deal",
                "positive_phrases": ["inks deal"],
            }

    engine = CatalystEngine(
        api_key="test-key",
        secret_key="test-secret",
        now_provider=lambda: datetime(2026, 4, 14, 12, 30, tzinfo=UTC),
        ai_evaluator=FakeAiEvaluator(),
        promote_ai_result=True,
    )

    result = engine._classify_symbol_articles(
        "ROLR",
        [
            {
                "created_at": "2026-04-14T12:12:54Z",
                "headline": "High Roller inks Crypto.com deal to launch U.S. event-based prediction markets",
                "summary": "The company said the agreement expands new revenue streams.",
                "url": "https://example.com/rolr",
            }
        ],
    )

    assert result["path_a_eligible"] is True
    assert result["catalyst_status"] == "ai_shadow_promoted"
    assert result["has_real_catalyst"] is True
    assert result["confidence"] == 0.91
