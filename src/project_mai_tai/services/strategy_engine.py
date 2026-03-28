from __future__ import annotations

import asyncio

from project_mai_tai.services.runtime import run_placeholder_worker


SERVICE_NAME = "strategy-engine"


async def main() -> None:
    await run_placeholder_worker(
        service_name=SERVICE_NAME,
        description="Consumes normalized events and emits strategy intents for 30s, 1m, TOS, and Runner.",
        topics=["market-data", "strategy-intents", "heartbeats"],
    )


def run() -> None:
    asyncio.run(main())
