from __future__ import annotations

import asyncio

from project_mai_tai.log import configure_logging
from project_mai_tai.market_data.snapshot_worker import SnapshotWorkerService
from project_mai_tai.settings import get_settings


async def main():
    await SnapshotWorkerService().run()


def run():
    settings = get_settings()
    logger = configure_logging("market-data-snapshot-worker", settings.log_level)
    logger.info("[MARKET-DATA-SNAPSHOT-WORKER] process starting")
    asyncio.run(main())


if __name__ == "__main__":
    run()
