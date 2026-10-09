"""Duplicate quotes do not decode a reading while the existing durable CAS is pending."""

import pytest

from tests.unit.test_nfq2_eh_fresh_price import biya, hold
from tests.unit.test_nfq2_eh_fresh_price import service as _recorded_service

service = _recorded_service


@pytest.mark.asyncio
async def test_duplicate_quotes_with_pending_save_do_not_read_or_copy(service, monkeypatch):
    active = hold(service, biya())
    key = service._nfq2_key(active.event)
    service.__dict__["_eh_price_hold_inflight"] = {key}

    def forbidden(*args, **kwargs):
        pytest.fail("duplicate quote decoded/copied/persisted an already pending hold")

    monkeypatch.setattr(service, "_nfq2_reading", forbidden)
    monkeypatch.setattr(service, "_nfq2_copy", forbidden)
    monkeypatch.setattr(service, "session_factory", forbidden)
    for _ in range(1000):
        await service._evaluate_nfq2_holds("BIYA")
    assert service._nfq2_holds[key] is active
    assert active.phase == "held"
    assert not service.redis.entries
