"""C8 diagnostics do not change the existing unsent refusal/cleanup path."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import logging
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live import execution
from project_mai_tai.momentum_live.admission import Refusal
from tests.unit.test_momlive1_execution import setup


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["timeout", "quote_missing_future_or_stale",
    "rounded_ask_above_cap", "webull_subdollar_quantity_below_100"])
async def test_no_wire_refusal_keeps_actual_reason_and_quote_inputs(reason, monkeypatch, caplog):
    env = setup(is_us=False)
    env.ports.request_quote_owner = AsyncMock()
    env.ports.wait_quote = None
    env.signal = replace(env.signal, signal_price=Decimal("1.9198"))  # PRICE1 ($1.00 buy floor, 10-27) refuses sub-dollar first
    env.quote.update(ask="1.950", received_at=env.now[0]-timedelta(milliseconds=125))
    if reason == "timeout":
        env.ports.request_quote_owner.side_effect = [TimeoutError(), None]
    elif reason == "quote_missing_future_or_stale":
        env.quote.clear()
    elif reason == "webull_subdollar_quantity_below_100":
        # $100/limit<1 always yields >=100 shares today. Exercise the existing
        # refusal's diagnostic path explicitly, not a fabricated natural reject.
        def refuse(*args, **kwargs):
            raise Refusal(reason)
        monkeypatch.setattr(execution, "price_entry", refuse)
    with caplog.at_level(logging.INFO, logger="momentum-live"):
        with pytest.raises(Refusal, match="^quote_wait_expired_or_refused_no_wire$"):
            await env.executor.admit(env.signal)
    rows = [record.getMessage() for record in caplog.records if "[MOMLIVE1-REFUSED]" in record.getMessage()]
    assert len(rows) == 1
    line = rows[0]
    assert f"symbol=SCNI reason={reason} " in line
    assert "signal=1.9198 cap=1.9485970" in line
    assert "route=webull no_wire=true" in line
    if reason in {"timeout", "quote_missing_future_or_stale"}:
        assert "ask=UNKNOWN ask_age_ms=UNKNOWN" in line
        assert "limit=UNKNOWN" in line
    else:
        expected_age = (int(env.now[0].timestamp()*1e9)
                        - int(env.quote["received_at"].timestamp()*1e9))/1_000_000
        assert f"ask=1.950 ask_age_ms={expected_age}" in line
        assert "limit=1.95 " in line
    assert not env.sent
    env.ports.durable_order.assert_not_awaited()
    env.ports.record.assert_not_awaited()
    env.ports.record_route.assert_not_awaited()
    env.ports.foreign_owner.assert_awaited_once_with("live:orb", "SCNI")
    assert env.ports.durable_claim.await_count == 1
    assert env.ports.request_quote_owner.await_count == 2
    assert env.ports.save_episode.await_count == 1
    assert env.book._active_count == 0


@pytest.mark.asyncio
async def test_diagnostic_cap_overflow_does_not_replace_timeout_refusal(caplog):
    env = setup(is_us=False)
    env.signal = replace(env.signal, signal_price=Decimal("1e999999999"))
    env.ports.request_quote_owner = AsyncMock(side_effect=[TimeoutError(), None])
    with caplog.at_level(logging.INFO, logger="momentum-live"):
        with pytest.raises(Refusal, match="^quote_wait_expired_or_refused_no_wire$"):
            await env.executor.admit(env.signal)
    assert "reason=timeout" in caplog.text and "cap=UNKNOWN" in caplog.text
    assert not env.sent and env.book._active_count == 0
