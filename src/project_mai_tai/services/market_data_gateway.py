from __future__ import annotations

import asyncio

from project_mai_tai.market_data.gateway import MarketDataGatewayService


SERVICE_NAME = "market-data-gateway"


async def main() -> None:
    service = MarketDataGatewayService()
    await service.run()


def run() -> None:
    asyncio.run(main())
