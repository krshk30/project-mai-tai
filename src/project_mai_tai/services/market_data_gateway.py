from __future__ import annotations

import asyncio

from project_mai_tai.log import configure_logging
from project_mai_tai.market_data.gateway import MarketDataGatewayService
from project_mai_tai.settings import get_settings


SERVICE_NAME = "market-data-gateway"


async def main() -> None:
    service = MarketDataGatewayService()
    await service.run()


def run() -> None:
    settings = get_settings()
    logger = configure_logging(SERVICE_NAME, settings.log_level)
    logger.info("[MARKET-DATA] process starting")
    asyncio.run(main())
