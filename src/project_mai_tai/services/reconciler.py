from __future__ import annotations

import asyncio

from project_mai_tai.reconciliation import ReconciliationService


SERVICE_NAME = "reconciler"


async def main() -> None:
    service = ReconciliationService()
    await service.run()


def run() -> None:
    asyncio.run(main())
