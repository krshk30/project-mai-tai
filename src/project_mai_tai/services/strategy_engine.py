from __future__ import annotations

import asyncio

from project_mai_tai.services.strategy_engine_app import StrategyEngineService


SERVICE_NAME = "strategy-engine"


async def main() -> None:
    service = StrategyEngineService()
    service.logger.info(
        "[STARTUP-FLAGS] orb_country_semantic_admission_enabled=%s scanner_confirmed_capture_session_day_enabled=%s keepwatch1=%s brokerdecides1=%s",
        service.settings.orb_country_semantic_admission_enabled,
        service.settings.scanner_confirmed_capture_session_day_enabled,
        service.settings.strategy_schwab_1m_v2_keepwatch1_enabled,
        service.settings.strategy_schwab_1m_v2_brokerdecides1_enabled,
    )
    await service.run()


def run() -> None:
    asyncio.run(main())
