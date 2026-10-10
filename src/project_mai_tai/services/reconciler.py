from __future__ import annotations

import asyncio

from project_mai_tai.log import configure_logging
from project_mai_tai.reconciliation import ReconciliationService
from project_mai_tai.settings import get_settings


SERVICE_NAME = "reconciler"


async def main() -> None:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    service = ReconciliationService(settings=settings)
    await service.run()


def run() -> None:
    asyncio.run(main())
