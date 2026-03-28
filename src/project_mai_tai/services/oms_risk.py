from __future__ import annotations

import asyncio

from project_mai_tai.services.runtime import run_placeholder_worker


SERVICE_NAME = "oms-risk"


async def main() -> None:
    await run_placeholder_worker(
        service_name=SERVICE_NAME,
        description="Validates intents, owns order submission, and derives positions from broker events.",
        topics=["strategy-intents", "order-events", "heartbeats"],
    )


def run() -> None:
    asyncio.run(main())
