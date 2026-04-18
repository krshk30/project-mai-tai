from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

EASTERN_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class CatalystConfig:
    session_start_hour_et: int = 16
    cache_ttl_minutes: int = 15
    empty_cache_ttl_seconds: int = 60
    request_timeout_seconds: int = 5
    max_articles_per_symbol: int = 20
    batch_size: int = 5
    path_a_min_confidence: float = 0.85


@dataclass(frozen=True)
class CatalystRule:
    category: str
    direction: str
    weight: float
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class CatalystAiConfig:
    provider: str = "openai"
    model: str = "gpt-4.1-mini"
    base_url: str = "https://api.openai.com/v1"
    request_timeout_seconds: int = 8
    max_articles: int = 3
    max_summary_chars: int = 280


class CatalystAiEvaluator:
    def __init__(
        self,
        *,
        api_key: str,
        config: CatalystAiConfig | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.config = config or CatalystAiConfig()

    def evaluate(
        self,
        *,
        ticker: str,
        articles: list[dict[str, object]],
        rule_result: dict[str, object],
    ) -> dict[str, object]:
        if not self.api_key:
            return {
                "status": "disabled",
                "provider": self.config.provider,
                "model": self.config.model,
                "reason": "AI catalyst evaluator is disabled because no API key is configured.",
            }

        payload = self._build_payload(ticker=ticker, articles=articles, rule_result=rule_result)
        try:
            request = Request(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
            )
            request.add_header("Authorization", f"Bearer {self.api_key}")
            request.add_header("Content-Type", "application/json")
            with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                body = json.loads(response.read())
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:400]
            logger.warning("AI catalyst evaluation failed for %s with HTTP %s", ticker, exc.code)
            return {
                "status": "error",
                "provider": self.config.provider,
                "model": self.config.model,
                "reason": f"AI catalyst evaluator request failed with HTTP {exc.code}.",
                "detail": detail,
            }
        except URLError:
            logger.warning("AI catalyst evaluation failed for %s due to network error", ticker)
            return {
                "status": "error",
                "provider": self.config.provider,
                "model": self.config.model,
                "reason": "AI catalyst evaluator request failed due to a network error.",
            }
        except Exception:
            logger.exception("AI catalyst evaluation failed for %s", ticker)
            return {
                "status": "error",
                "provider": self.config.provider,
                "model": self.config.model,
                "reason": "AI catalyst evaluator request failed unexpectedly.",
            }

        try:
            content = body["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception:
            logger.warning("AI catalyst evaluation returned unparsable output for %s", ticker)
            return {
                "status": "error",
                "provider": self.config.provider,
                "model": self.config.model,
                "reason": "AI catalyst evaluator returned invalid JSON.",
            }

        return self._normalize_result(parsed)

    def _build_payload(
        self,
        *,
        ticker: str,
        articles: list[dict[str, object]],
        rule_result: dict[str, object],
    ) -> dict[str, object]:
        trimmed_articles: list[dict[str, object]] = []
        for article in articles[: self.config.max_articles]:
            trimmed_articles.append(
                {
                    "headline": str(article.get("headline", ""))[:180],
                    "summary": str(article.get("summary", ""))[: self.config.max_summary_chars],
                    "published": str(article.get("published_label", "")),
                    "url": str(article.get("url", "")),
                }
            )

        system_prompt = (
            "You are a market news catalyst classifier for small-cap momentum trading. "
            "Return strict JSON only. Focus on whether the article set contains a real, "
            "company-specific bullish catalyst for the ticker, not generic market chatter."
        )
        user_prompt = {
            "ticker": ticker,
            "rule_engine_snapshot": {
                "headline": rule_result.get("headline", ""),
                "catalyst": rule_result.get("catalyst", ""),
                "direction": rule_result.get("direction", ""),
                "confidence": rule_result.get("confidence", 0),
                "path_a_eligible": rule_result.get("path_a_eligible", False),
                "reason": rule_result.get("reason", ""),
            },
            "articles": trimmed_articles,
            "instructions": {
                "return_fields": [
                    "direction",
                    "category",
                    "confidence",
                    "has_real_catalyst",
                    "is_generic_roundup",
                    "is_company_specific",
                    "path_a_eligible",
                    "reason",
                    "headline_basis",
                    "positive_phrases",
                ],
                "path_a_guidance": (
                    "path_a_eligible should be true only for fresh, company-specific bullish catalysts "
                    "like contracts, partnerships, mergers, regulatory wins, or strong data, and false "
                    "for generic roundup coverage, recycled commentary, or unclear articles."
                ),
            },
        }
        return {
            "model": self.config.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_prompt)},
            ],
        }

    def _normalize_result(self, parsed: dict[str, object]) -> dict[str, object]:
        def _as_bool(value: object) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"true", "1", "yes"}
            return bool(value)

        try:
            confidence = float(parsed.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))

        positive_phrases = parsed.get("positive_phrases", [])
        if not isinstance(positive_phrases, list):
            positive_phrases = []

        return {
            "status": "ok",
            "provider": self.config.provider,
            "model": self.config.model,
            "direction": str(parsed.get("direction", "neutral") or "neutral").strip().lower(),
            "category": str(parsed.get("category", "NEWS") or "NEWS").strip(),
            "confidence": confidence,
            "has_real_catalyst": _as_bool(parsed.get("has_real_catalyst", False)),
            "is_generic_roundup": _as_bool(parsed.get("is_generic_roundup", False)),
            "is_company_specific": _as_bool(parsed.get("is_company_specific", False)),
            "path_a_eligible": _as_bool(parsed.get("path_a_eligible", False)),
            "reason": str(parsed.get("reason", "") or "").strip(),
            "headline_basis": str(parsed.get("headline_basis", "") or "").strip(),
            "positive_phrases": [str(item).strip() for item in positive_phrases if str(item).strip()],
        }


BULLISH_RULES: tuple[CatalystRule, ...] = (
    CatalystRule(
        category="FDA/BIOTECH",
        direction="bullish",
        weight=1.6,
        keywords=(
            "fda approval",
            "fda approves",
            "fda clearance",
            "fda clears",
            "fda clear",
            "breakthrough therapy",
            "breakthrough designation",
            "fast track designation",
            "fast track",
            "positive phase 3",
            "positive phase 2",
            "met primary endpoint",
            "met secondary endpoint",
            "positive topline",
            "positive top-line",
            "successful trial",
            "trial success",
            "favorable data",
            "positive data",
            "positive results",
            "eua",
            "emergency use authorization",
            "510(k) clearance",
            "de novo clearance",
        ),
    ),
    CatalystRule(
        category="DEAL/CONTRACT",
        direction="bullish",
        weight=1.4,
        keywords=(
            "awarded contract",
            "wins contract",
            "secured contract",
            "secures contract",
            "partnership agreement",
            "strategic partnership",
            "collaboration agreement",
            "license agreement",
            "distribution agreement",
            "purchase order",
            "merger agreement",
            "to be acquired",
            "definitive agreement",
        ),
    ),
    CatalystRule(
        category="EARNINGS/GUIDANCE",
        direction="bullish",
        weight=1.3,
        keywords=(
            "beats estimates",
            "beat estimates",
            "tops estimates",
            "raises guidance",
            "raises forecast",
            "record revenue",
            "record sales",
            "profitability",
            "returns to profitability",
            "cash flow positive",
            "backlog growth",
        ),
    ),
    CatalystRule(
        category="COMPLIANCE RECOVERY",
        direction="bullish",
        weight=0.9,
        keywords=(
            "regains compliance",
            "regain compliance",
            "meets listing requirements",
            "compliance restored",
        ),
    ),
    CatalystRule(
        category="BUYBACK/INSIDER",
        direction="bullish",
        weight=1.0,
        keywords=(
            "share repurchase",
            "stock repurchase",
            "share buyback",
            "stock buyback",
            "tender offer",
            "insider purchase",
            "insider buy",
            "director purchase",
            "ceo purchase",
        ),
    ),
)


BEARISH_RULES: tuple[CatalystRule, ...] = (
    CatalystRule(
        category="OFFERING/DILUTION",
        direction="bearish",
        weight=1.8,
        keywords=(
            "registered direct offering",
            "public offering",
            "private placement",
            "at-the-market",
            "atm offering",
            "shelf registration",
            "equity raise",
            "stock offering",
            "dilution",
            "common stock offering",
            "pre-funded warrants",
        ),
    ),
    CatalystRule(
        category="FDA/BIOTECH",
        direction="bearish",
        weight=1.6,
        keywords=(
            "complete response letter",
            "crl",
            "clinical hold",
            "fda rejection",
            "trial failure",
            "failed to meet primary endpoint",
            "failed to meet endpoint",
            "negative data",
            "negative results",
            "adverse event",
            "safety concern",
        ),
    ),
    CatalystRule(
        category="COMPLIANCE/DISTRESS",
        direction="bearish",
        weight=1.5,
        keywords=(
            "reverse stock split",
            "reverse split",
            "delisting notice",
            "nasdaq deficiency",
            "non-compliance",
            "below listing requirements",
            "going concern",
        ),
    ),
    CatalystRule(
        category="EARNINGS/GUIDANCE",
        direction="bearish",
        weight=1.3,
        keywords=(
            "misses estimates",
            "missed estimates",
            "lowers guidance",
            "cuts guidance",
            "cuts forecast",
            "weak results",
            "revenue decline",
            "loss widens",
            "warning on revenue",
        ),
    ),
    CatalystRule(
        category="LEGAL/RISK",
        direction="bearish",
        weight=1.1,
        keywords=(
            "sec investigation",
            "sec inquiry",
            "lawsuit",
            "class action",
            "downgrade",
            "underperform",
            "sell rating",
            "trading halt",
        ),
    ),
)


GENERIC_ROUNDUP_PATTERNS: tuple[str, ...] = (
    "stock rises",
    "stock rose",
    "stock jumps",
    "stock jumped",
    "stock surges",
    "stock surged",
    "stock rallies",
    "stock rallied",
    "shares rise",
    "shares rose",
    "shares jump",
    "shares jumped",
    "shares surge",
    "shares surged",
    "why ",
    "top gainers",
    "top movers",
    "premarket movers",
    "pre-market movers",
    "after-hours movers",
    "stocks to watch",
    "market roundup",
    "small-cap movers",
    "hot stocks",
    "trending stocks",
    "on unusual volume",
    "amid heavy volume",
    "as shares",
)


class CatalystEngine:
    def __init__(
        self,
        *,
        api_key: str | None,
        secret_key: str | None,
        config: CatalystConfig | None = None,
        now_provider: Callable[[], datetime] | None = None,
        ai_evaluator: CatalystAiEvaluator | None = None,
        promote_ai_result: bool = False,
    ):
        self.api_key = api_key or ""
        self.secret_key = secret_key or ""
        self.config = config or CatalystConfig()
        self.now_provider = now_provider or (lambda: datetime.now(UTC))
        self._cache: dict[str, dict[str, object]] = {}
        self.ai_evaluator = ai_evaluator
        self.promote_ai_result = promote_ai_result

    def get_catalyst(self, ticker: str) -> dict[str, object]:
        return self.get_catalysts_batch([ticker]).get(ticker.upper(), self._empty_result())

    def get_catalysts_batch(self, tickers: Iterable[str]) -> dict[str, dict[str, object]]:
        normalized = sorted({str(ticker).upper() for ticker in tickers if ticker})
        if not normalized:
            return {}

        results: dict[str, dict[str, object]] = {}
        to_fetch: list[str] = []
        for ticker in normalized:
            cached = self._cache.get(ticker)
            if cached is not None:
                fetched_at = cached.get("fetched_at")
                if isinstance(fetched_at, datetime):
                    age_seconds = (self._current_time_utc() - fetched_at).total_seconds()
                    if age_seconds < self._cache_ttl_seconds_for_result(cached):
                        results[ticker] = cached
                        continue
            to_fetch.append(ticker)

        if to_fetch and self.api_key and self.secret_key:
            grouped_articles, failed_tickers = self._fetch_alpaca_articles_by_symbol(to_fetch)
            for ticker in to_fetch:
                if ticker in failed_tickers:
                    results[ticker] = self._empty_result(
                        source="alpaca_news",
                        news_fetch_status="error",
                        catalyst_status="fetch_failed",
                        reason=(
                            "Alpaca news request failed for this symbol just now. "
                            "Mai Tai will retry shortly."
                        ),
                    )
                else:
                    results[ticker] = self._classify_symbol_articles(ticker, grouped_articles.get(ticker, []))
                self._cache[ticker] = results[ticker]
        else:
            for ticker in to_fetch:
                results[ticker] = self._empty_result(
                    source="alpaca_news",
                    news_fetch_status="disabled",
                    catalyst_status="provider_disabled",
                    reason="Alpaca news credentials are unavailable, so Path A cannot evaluate this symbol.",
                )
                self._cache[ticker] = results[ticker]

        return results

    def _fetch_alpaca_articles_by_symbol(
        self,
        tickers: list[str],
    ) -> tuple[dict[str, list[dict[str, object]]], set[str]]:
        grouped: dict[str, list[dict[str, object]]] = {ticker: [] for ticker in tickers}
        failed_tickers: set[str] = set()

        for index in range(0, len(tickers), self.config.batch_size):
            batch = tickers[index : index + self.config.batch_size]
            url = (
                "https://data.alpaca.markets/v1beta1/news?"
                f"symbols={quote(','.join(batch))}&limit={self.config.max_articles_per_symbol}&sort=desc"
            )
            request = Request(url)
            request.add_header("APCA-API-KEY-ID", self.api_key)
            request.add_header("APCA-API-SECRET-KEY", self.secret_key)

            try:
                with urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                    payload = json.loads(response.read())
            except Exception:
                logger.exception("Failed to fetch Alpaca news batch for %s", ",".join(batch))
                failed_tickers.update(batch)
                continue

            for article in payload.get("news", []):
                symbols = [str(symbol).upper() for symbol in article.get("symbols", []) if symbol]
                for ticker in batch:
                    if ticker in symbols:
                        grouped.setdefault(ticker, []).append(article)

        return grouped, failed_tickers

    def _classify_symbol_articles(self, ticker: str, articles: list[dict[str, object]]) -> dict[str, object]:
        now_utc = self._current_time_utc()
        now_et = now_utc.astimezone(EASTERN_TZ)
        session_start_et = self._current_session_news_start(now_et)

        recent_articles: list[dict[str, object]] = []
        article_analyses: list[dict[str, object]] = []
        for article in articles:
            published_at = self._parse_timestamp(str(article.get("created_at", "") or article.get("updated_at", "")))
            if published_at is None:
                continue
            published_et = published_at.astimezone(EASTERN_TZ)
            if published_et < session_start_et:
                continue

            headline = str(article.get("headline", "")).strip()
            summary = str(article.get("summary", "")).strip()
            url = str(article.get("url", "")).strip()
            if not headline and not summary:
                continue

            analysis = self._analyze_article_text(headline=headline, summary=summary)
            record = {
                "headline": headline[:180],
                "summary": summary[:280],
                "url": url,
                "published_at": published_at,
                "published_label": published_et.strftime("%m/%d %I:%M%p ET"),
                "analysis": analysis,
            }
            recent_articles.append(record)
            article_analyses.append(analysis)

        if not recent_articles:
            base_result = self._empty_result(
                source="alpaca_news",
                news_fetch_status="ok",
                catalyst_status="no_articles",
                reason=(
                    f"No company-specific Alpaca news article has been returned yet for {ticker} "
                    "in the current catalyst window."
                ),
                window_start=session_start_et,
            )
            return self._apply_ai_overlay(
                ticker=ticker,
                recent_articles=[],
                base_result=base_result,
            )

        latest_article = recent_articles[0]
        real_articles = [record for record in recent_articles if bool(record["analysis"]["has_real_catalyst"])]
        bullish_score = sum(float(record["analysis"]["weight"]) for record in real_articles if record["analysis"]["direction"] == "bullish")
        bearish_score = sum(float(record["analysis"]["weight"]) for record in real_articles if record["analysis"]["direction"] == "bearish")
        generic_count = sum(1 for record in recent_articles if bool(record["analysis"]["is_generic_roundup"]))

        direction = "neutral"
        if bullish_score > bearish_score:
            direction = "bullish"
        elif bearish_score > bullish_score:
            direction = "bearish"

        has_real_catalyst = len(real_articles) > 0 and direction in {"bullish", "bearish"}
        dominant_article = None
        if real_articles:
            dominant_article = max(
                real_articles,
                key=lambda record: (
                    float(record["analysis"]["weight"]),
                    record["published_at"],
                ),
            )

        display_article = dominant_article or latest_article

        confidence = self._calculate_confidence(
            bullish_score=bullish_score,
            bearish_score=bearish_score,
            real_article_count=len(real_articles),
            latest_real_published=dominant_article["published_at"] if dominant_article else None,
            now_utc=now_utc,
        )
        freshness_minutes = self._freshness_minutes(
            now_utc=now_utc,
            published_at=display_article["published_at"],
        )
        is_generic_roundup = generic_count == len(recent_articles) or (
            generic_count > 0 and not has_real_catalyst
        )
        catalyst_type = (
            str(dominant_article["analysis"]["category"])
            if dominant_article is not None
            else "ROUNDUP"
            if is_generic_roundup
            else "NEWS"
        )
        reason = self._build_reason(
            ticker=ticker,
            direction=direction,
            has_real_catalyst=has_real_catalyst,
            is_generic_roundup=is_generic_roundup,
            catalyst_type=catalyst_type,
            real_article_count=len(real_articles),
            total_article_count=len(recent_articles),
            freshness_minutes=freshness_minutes,
        )

        result = {
            "catalyst": catalyst_type,
            "catalyst_type": catalyst_type,
            "headline": str(display_article["headline"]),
            "published": str(display_article["published_label"]),
            "url": str(display_article["url"]),
            "sentiment": direction,
            "direction": direction,
            "source": "alpaca_news",
            "news_fetch_status": "ok",
            "catalyst_status": self._catalyst_status(
                has_real_catalyst=has_real_catalyst,
                is_generic_roundup=is_generic_roundup,
            ),
            "confidence": confidence,
            "ai_confidence": confidence,
            "reason": reason,
            "ai_reason": reason,
            "window_start_label": session_start_et.strftime("%m/%d %I:%M%p ET"),
            "article_count": len(recent_articles),
            "news_count": len(recent_articles),
            "real_catalyst_article_count": len(real_articles),
            "freshness_minutes": freshness_minutes,
            "is_generic_roundup": is_generic_roundup,
            "has_real_catalyst": has_real_catalyst,
            "path_a_eligible": (
                has_real_catalyst
                and not is_generic_roundup
                and direction == "bullish"
                and confidence >= self.config.path_a_min_confidence
            ),
            "window_start": session_start_et.isoformat(),
            "fetched_at": now_utc,
        }
        return self._apply_ai_overlay(
            ticker=ticker,
            recent_articles=recent_articles,
            base_result=result,
        )

    def _analyze_article_text(self, *, headline: str, summary: str) -> dict[str, object]:
        text = f"{headline} {summary}".lower()
        matched_bullish = [rule for rule in BULLISH_RULES if any(keyword in text for keyword in rule.keywords)]
        matched_bearish = [rule for rule in BEARISH_RULES if any(keyword in text for keyword in rule.keywords)]
        generic_roundup = any(pattern in text for pattern in GENERIC_ROUNDUP_PATTERNS)

        if matched_bullish and not matched_bearish:
            dominant = max(matched_bullish, key=lambda rule: rule.weight)
            return {
                "direction": dominant.direction,
                "category": dominant.category,
                "weight": dominant.weight,
                "has_real_catalyst": True,
                "is_generic_roundup": False,
            }

        if matched_bearish and not matched_bullish:
            dominant = max(matched_bearish, key=lambda rule: rule.weight)
            return {
                "direction": dominant.direction,
                "category": dominant.category,
                "weight": dominant.weight,
                "has_real_catalyst": True,
                "is_generic_roundup": False,
            }

        if matched_bullish and matched_bearish:
            dominant = max(matched_bullish + matched_bearish, key=lambda rule: rule.weight)
            return {
                "direction": "neutral",
                "category": dominant.category,
                "weight": dominant.weight,
                "has_real_catalyst": True,
                "is_generic_roundup": False,
            }

        return {
            "direction": "neutral",
            "category": "ROUNDUP" if generic_roundup else "NEWS",
            "weight": 0.0,
            "has_real_catalyst": False,
            "is_generic_roundup": generic_roundup,
        }

    def _calculate_confidence(
        self,
        *,
        bullish_score: float,
        bearish_score: float,
        real_article_count: int,
        latest_real_published: datetime | None,
        now_utc: datetime,
    ) -> float:
        if real_article_count <= 0:
            return 0.0

        margin = abs(bullish_score - bearish_score)
        confidence = 0.58
        confidence += min(real_article_count - 1, 2) * 0.10
        confidence += min(margin, 2.0) * 0.08

        if latest_real_published is not None:
            freshness_minutes = self._freshness_minutes(now_utc=now_utc, published_at=latest_real_published)
            if freshness_minutes <= 60:
                confidence += 0.07
            elif freshness_minutes <= 240:
                confidence += 0.04
            elif freshness_minutes <= 960:
                confidence += 0.02

        return round(max(0.0, min(confidence, 0.95)), 2)

    def _build_reason(
        self,
        *,
        ticker: str,
        direction: str,
        has_real_catalyst: bool,
        is_generic_roundup: bool,
        catalyst_type: str,
        real_article_count: int,
        total_article_count: int,
        freshness_minutes: int | None,
    ) -> str:
        if is_generic_roundup:
            return (
                f"{ticker} only has generic roundup or price-action coverage in the current news window; "
                "that does not qualify for Path A."
            )
        if not has_real_catalyst:
            return (
                f"{ticker} has {total_article_count} recent Alpaca article(s), but none matched a qualifying "
                "Path A catalyst pattern."
            )
        freshness = f"{freshness_minutes}m old" if freshness_minutes is not None else "freshness unknown"
        return (
            f"{direction.title()} {catalyst_type} catalyst across {real_article_count} article(s), "
            f"latest {freshness}."
        )

    def _catalyst_status(self, *, has_real_catalyst: bool, is_generic_roundup: bool) -> str:
        if has_real_catalyst:
            return "real_catalyst"
        if is_generic_roundup:
            return "generic_only"
        return "non_qualifying_articles"

    def _cache_ttl_seconds_for_result(self, result: dict[str, object]) -> float:
        news_fetch_status = str(result.get("news_fetch_status", "") or "").strip().lower()
        catalyst_status = str(result.get("catalyst_status", "") or "").strip().lower()
        article_count = int(result.get("article_count", 0) or 0)
        if news_fetch_status == "ok" and catalyst_status == "no_articles" and article_count <= 0:
            return float(self.config.empty_cache_ttl_seconds)
        return float(self.config.cache_ttl_minutes * 60)

    def _current_session_news_start(self, now_et: datetime) -> datetime:
        boundary = now_et.replace(
            hour=self.config.session_start_hour_et,
            minute=0,
            second=0,
            microsecond=0,
        )
        if now_et < boundary:
            boundary -= timedelta(days=1)
        while boundary.weekday() >= 5:
            boundary -= timedelta(days=1)
        return boundary

    def _freshness_minutes(self, *, now_utc: datetime, published_at: datetime | None) -> int | None:
        if published_at is None:
            return None
        return max(0, int((now_utc - published_at).total_seconds() // 60))

    def _parse_timestamp(self, value: str) -> datetime | None:
        if not value:
            return None
        normalized = value.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _empty_result(
        self,
        *,
        source: str = "",
        news_fetch_status: str = "ok",
        catalyst_status: str = "no_articles",
        reason: str = "",
        window_start: datetime | None = None,
    ) -> dict[str, object]:
        window_start_label = ""
        window_start_value = ""
        if window_start is not None:
            window_start_label = window_start.strftime("%m/%d %I:%M%p ET")
            window_start_value = window_start.isoformat()
        return {
            "catalyst": "",
            "catalyst_type": "",
            "headline": "",
            "published": "",
            "url": "",
            "sentiment": "",
            "direction": "",
            "source": source,
            "news_fetch_status": news_fetch_status,
            "catalyst_status": catalyst_status,
            "confidence": 0.0,
            "ai_confidence": 0.0,
            "reason": reason,
            "ai_reason": reason,
            "window_start_label": window_start_label,
            "article_count": 0,
            "news_count": 0,
            "real_catalyst_article_count": 0,
            "freshness_minutes": None,
            "is_generic_roundup": False,
            "has_real_catalyst": False,
            "path_a_eligible": False,
            "window_start": window_start_value,
            "fetched_at": self._current_time_utc(),
        }

    def _apply_ai_overlay(
        self,
        *,
        ticker: str,
        recent_articles: list[dict[str, object]],
        base_result: dict[str, object],
    ) -> dict[str, object]:
        result = dict(base_result)

        if self.ai_evaluator is None:
            result.update(
                {
                    "ai_shadow_status": "disabled",
                    "ai_shadow_provider": "",
                    "ai_shadow_model": "",
                    "ai_shadow_direction": "",
                    "ai_shadow_category": "",
                    "ai_shadow_confidence": 0.0,
                    "ai_shadow_has_real_catalyst": False,
                    "ai_shadow_is_generic_roundup": False,
                    "ai_shadow_is_company_specific": False,
                    "ai_shadow_path_a_eligible": False,
                    "ai_shadow_reason": "",
                    "ai_shadow_headline_basis": "",
                    "ai_shadow_positive_phrases": [],
                }
            )
            return result

        ai_result = self.ai_evaluator.evaluate(
            ticker=ticker,
            articles=recent_articles,
            rule_result=result,
        )
        result.update(
            {
                "ai_shadow_status": str(ai_result.get("status", "")),
                "ai_shadow_provider": str(ai_result.get("provider", "")),
                "ai_shadow_model": str(ai_result.get("model", "")),
                "ai_shadow_direction": str(ai_result.get("direction", "")),
                "ai_shadow_category": str(ai_result.get("category", "")),
                "ai_shadow_confidence": float(ai_result.get("confidence", 0.0) or 0.0),
                "ai_shadow_has_real_catalyst": bool(ai_result.get("has_real_catalyst", False)),
                "ai_shadow_is_generic_roundup": bool(ai_result.get("is_generic_roundup", False)),
                "ai_shadow_is_company_specific": bool(ai_result.get("is_company_specific", False)),
                "ai_shadow_path_a_eligible": bool(ai_result.get("path_a_eligible", False)),
                "ai_shadow_reason": str(ai_result.get("reason", "")),
                "ai_shadow_headline_basis": str(ai_result.get("headline_basis", "")),
                "ai_shadow_positive_phrases": list(ai_result.get("positive_phrases", [])),
            }
        )

        if (
            self.promote_ai_result
            and str(ai_result.get("status", "")) == "ok"
            and bool(ai_result.get("path_a_eligible", False))
            and str(ai_result.get("direction", "")) == "bullish"
        ):
            result["path_a_eligible"] = True
            result["has_real_catalyst"] = bool(ai_result.get("has_real_catalyst", result.get("has_real_catalyst", False)))
            if not str(result.get("catalyst", "")).strip():
                result["catalyst"] = str(ai_result.get("category", "NEWS"))
                result["catalyst_type"] = str(ai_result.get("category", "NEWS"))
            if not str(result.get("direction", "")).strip():
                result["direction"] = "bullish"
                result["sentiment"] = "bullish"
            if float(result.get("confidence", 0) or 0) < float(ai_result.get("confidence", 0) or 0):
                promoted_confidence = float(ai_result.get("confidence", 0) or 0)
                result["confidence"] = promoted_confidence
                result["ai_confidence"] = promoted_confidence
            result["catalyst_status"] = "ai_shadow_promoted"
            if not str(result.get("reason", "")).strip():
                promoted_reason = str(ai_result.get("reason", "")).strip()
                result["reason"] = promoted_reason
                result["ai_reason"] = promoted_reason

        return result

    def _current_time_utc(self) -> datetime:
        current = self.now_provider()
        if current.tzinfo is None:
            return current.replace(tzinfo=UTC)
        return current.astimezone(UTC)
