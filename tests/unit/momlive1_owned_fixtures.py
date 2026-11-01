"""Opt-in positive-exposure setup, never an admission or native-proof shortcut.

Do not use for zero-fill, refused/unsent, no-read or unknown-entry controls. An
unreadable protection child stays unreadable: the real actor still owns every
observation, attribution, save and protection decision.
"""

import asyncio


async def complete_owned_observation(runtime, identity):
    """Refresh one stale setup read before asserting an owned filled position."""
    before = runtime.now_ns()
    async with runtime.state_lock:
        assert identity in runtime.actor.work.owners
        runtime.actor._observations.pop(identity, None)
    await asyncio.wait_for(runtime.actor._service_owned(identity), 10)
    await asyncio.wait_for(runtime.actor.work.queue.join(), 10)
    assert runtime.now_ns() == before
    assert runtime.book.episodes[identity].filled > 0

