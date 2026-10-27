"""Service-owned prewire callback adapter; no shared service/emitter edits.

Main injects one instance's bind/refused into both selected-account emitters.
There is no global context, background task, model/migration or provider call.
"""

import asyncio

from project_mai_tai.brokerdecides1_prewire import PublicationBoundary, commit_refusal
from project_mai_tai.brokerdecides1_sync import enabled


class PublicationCallbacks:
    def __init__(self, service, *, run_sync=None):
        self.service = service
        self.run_sync = asyncio.to_thread if run_sync is None else run_sync

    def bind(self, draft, account):
        if not enabled(self.service.settings):
            raise ValueError("prewire_callback_flag_OFF")
        if self.service.session_factory is None:
            raise ValueError("prewire_callback_storage_UNKNOWN")
        boundary = PublicationBoundary(draft, account, tuple(self.service.strategy._removed_wait_accounts()))
        boundary._callback_owner = self
        return boundary

    async def refused(self, boundary, proof):
        if (not isinstance(boundary, PublicationBoundary) or boundary.refusal is not proof
                or getattr(boundary, "_callback_owner", None) is not self
                or boundary.identity.accounts != tuple(self.service.strategy._removed_wait_accounts())):
            raise ValueError("prewire_callback_scope_UNKNOWN")
        proof.validate()
        # Capture BEFORE the storage await, never clear a subsequently replaced
        # consumer memory. The receipt remains discoverable after restart.
        memory = getattr(self.service.strategy, "_brokerdecides1_memory", None)
        committed = await self.run_sync(commit_refusal, self.service.session_factory, proof)
        if memory is None or getattr(self.service.strategy, "_brokerdecides1_memory", None) is not memory:
            return "JOURNAL_COMMITTED_MEMORY_REPLACED"
        released = boundary.release_after_commit(memory, committed)
        return "JOURNAL_COMMITTED_EXACT_RESERVATION_RELEASED" if released else "JOURNAL_COMMITTED_NO_EXACT_RESERVATION"
