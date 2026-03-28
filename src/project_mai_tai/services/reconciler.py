from __future__ import annotations

import asyncio

from project_mai_tai.services.runtime import run_placeholder_worker


SERVICE_NAME = "reconciler"


async def main() -> None:
    await run_placeholder_worker(
        service_name=SERVICE_NAME,
        description="Compares broker truth to OMS truth and records findings without mutating runtime state directly.",
        topics=["order-events", "heartbeats"],
    )


def run() -> None:
    asyncio.run(main())
