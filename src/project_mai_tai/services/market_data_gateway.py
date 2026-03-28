from __future__ import annotations

import asyncio

from project_mai_tai.services.runtime import run_placeholder_worker


SERVICE_NAME = "market-data-gateway"


async def main() -> None:
    await run_placeholder_worker(
        service_name=SERVICE_NAME,
        description="Owns Massive/Polygon ingestion and publishes normalized market events.",
        topics=["market-data", "heartbeats"],
    )


def run() -> None:
    asyncio.run(main())
