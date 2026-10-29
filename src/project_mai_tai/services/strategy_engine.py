from __future__ import annotations

import asyncio

from project_mai_tai.services.strategy_engine_app import StrategyEngineService


SERVICE_NAME = "strategy-engine"


async def main() -> None:
    service = StrategyEngineService()
    await service.run()


def run() -> None:
    asyncio.run(main())
