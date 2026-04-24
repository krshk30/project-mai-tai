from __future__ import annotations

import asyncio

from project_mai_tai.services.trade_coach_app import TradeCoachApp


SERVICE_NAME = "trade-coach"


async def main() -> None:
    app = TradeCoachApp()
    await app.run()


def run() -> None:
    asyncio.run(main())
