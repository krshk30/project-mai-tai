from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable

from project_mai_tai.events import stream_name
from project_mai_tai.log import configure_logging
from project_mai_tai.settings import Settings, get_settings


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            continue


async def run_placeholder_worker(
    *,
    service_name: str,
    description: str,
    topics: list[str],
    settings: Settings | None = None,
    on_start: Callable[[Settings], None] | None = None,
) -> None:
    active_settings = settings or get_settings()
    logger = configure_logging(service_name, active_settings.log_level)
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    if on_start is not None:
        on_start(active_settings)

    logger.info("%s starting", service_name)
    logger.info("scope: %s", description)
    logger.info(
        "streams: %s",
        ", ".join(stream_name(active_settings.redis_stream_prefix, topic) for topic in topics),
    )

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=active_settings.service_heartbeat_interval_seconds,
            )
        except TimeoutError:
            logger.debug("%s heartbeat", service_name)

    logger.info("%s stopping", service_name)
