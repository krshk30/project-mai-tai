from __future__ import annotations

import asyncio

from project_mai_tai.oms.service import OmsRiskService


SERVICE_NAME = "oms-risk"


async def main() -> None:
    service = OmsRiskService()
    await service.run()


def run() -> None:
    asyncio.run(main())
