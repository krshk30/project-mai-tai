from __future__ import annotations

import asyncio
from datetime import timedelta

from project_mai_tai.ai_trade_coach import TradeCoachClient
from project_mai_tai.ai_trade_coach import TradeCoachConfig
from project_mai_tai.ai_trade_coach import TradeCoachRepository
from project_mai_tai.ai_trade_coach import TradeCoachService
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.log import configure_logging
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.services.strategy_engine_app import current_scanner_session_start_utc
from project_mai_tai.settings import Settings, get_settings


SERVICE_NAME = "trade-coach"


def _build_rulebook(settings: Settings) -> dict[str, object]:
    return {
        "scope": ["macd_30s", "webull_30s"],
        "notes": [
            "Post-trade coaching only in this phase.",
            "Do not treat paper and live execution as interchangeable.",
            "Judge setup quality separately from execution quality.",
            "Do not assume a losing trade is bad or a winning trade is good without evidence from the captured episode.",
            "Use concrete trade facts from the episode when explaining why a verdict was assigned.",
        ],
        "rubric": {
            "verdict_meaning": {
                "good": "The setup and execution both matched the rulebook well, even if the trade lost.",
                "mixed": "Some parts were valid, but the setup quality or execution quality was meaningfully weak.",
                "bad": "The trade should not have been taken as executed, or the execution materially violated the rulebook.",
                "skip": "No reliable review can be formed from the captured facts.",
            },
            "requirements": [
                "Keep setup quality distinct from trade outcome.",
                "If a loss is still labeled good, name the specific evidence that makes it good.",
                "If the trade was avoidable, say so clearly in rule_violations or next_time.",
                "Avoid generic praise without citing a concrete path, timing, scale, stop, or bar-context fact.",
            ],
        },
        "strategy_accounts": [
            {
                "strategy_code": "macd_30s",
                "broker_account_name": settings.strategy_macd_30s_account_name,
                "execution_mode": settings.execution_mode_for_provider(
                    settings.provider_for_strategy("macd_30s")
                ),
                "live_aggregate_bars_enabled": settings.strategy_macd_30s_live_aggregate_bars_enabled,
            },
            {
                "strategy_code": "webull_30s",
                "broker_account_name": settings.strategy_webull_30s_account_name,
                "execution_mode": settings.execution_mode_for_provider(
                    settings.provider_for_strategy("webull_30s")
                ),
                "live_aggregate_bars_enabled": settings.strategy_webull_30s_live_aggregate_bars_enabled,
            },
        ],
    }


class TradeCoachApp:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.logger = configure_logging(SERVICE_NAME, self.settings.log_level)
        config = TradeCoachConfig(
            provider=self.settings.trade_coach_provider,
            model=self.settings.trade_coach_model,
            base_url=self.settings.trade_coach_base_url,
            request_timeout_seconds=self.settings.trade_coach_request_timeout_seconds,
            context_bars=self.settings.trade_coach_context_bars,
            review_bars_after_exit=self.settings.trade_coach_review_bars_after_exit,
            max_similar_trades=self.settings.trade_coach_max_similar_trades,
        )
        self.repository = TradeCoachRepository(
            session_factory=build_session_factory(self.settings),
            config=config,
        )
        self.service = TradeCoachService(
            repository=self.repository,
            coach_client=TradeCoachClient(
                api_key=self.settings.trade_coach_api_key or "",
                config=config,
            ),
            rulebook=_build_rulebook(self.settings),
            review_limit=self.settings.trade_coach_review_limit,
        )

    async def run(self) -> None:
        if not self.settings.trade_coach_enabled:
            self.logger.info("trade coach disabled; exiting")
            return
        if not (self.settings.trade_coach_api_key or "").strip():
            self.logger.warning("trade coach enabled but no API key configured; exiting")
            return

        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        strategy_accounts = [
            ("macd_30s", self.settings.strategy_macd_30s_account_name),
            ("webull_30s", self.settings.strategy_webull_30s_account_name),
        ]
        self.logger.info("trade coach starting for %s", ", ".join(code for code, _ in strategy_accounts))

        while not stop_event.is_set():
            session_start = current_scanner_session_start_utc()
            session_end = session_start + timedelta(days=1)
            try:
                await self.service.run_review_cycle(
                    strategy_accounts=strategy_accounts,
                    session_start=session_start,
                    session_end=session_end,
                )
            except Exception:
                self.logger.exception("trade coach review cycle failed")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.settings.trade_coach_review_poll_seconds)
            except TimeoutError:
                continue

        self.logger.info("trade coach stopping")
