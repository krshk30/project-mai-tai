"""Mandatory UNKNOWN: exact known identity permits cancel, never a second SELL.

The normalized broker-read port injects transport/budget outcomes. Production
fresh-read classification, cancellation latches, release and DB writes stay real.
"""

from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.broker_adapters.webull_order_reads import QueryBudgetUnavailable
from project_mai_tai.momentum_live.admission import Refusal
from tests.unit.test_momorder1_runtime_db import held_case, seed


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("reason", ["hard_stop_15pct", "time_300s", "KILLSW1_STOP"])
@pytest.mark.parametrize("unavailable", ["budget", "transport"])
async def test_mandatory_unavailable_fresh_read_cancels_exact_owned_id_once_without_releasing(
    tmp_path, monkeypatch, provider, reason, unavailable,
):
    h = await seed(tmp_path, provider=provider)
    try:
        position, handle = await held_case(h)
        # Both venue sessions are open at 08:00 ET; no pre-07 Schwab STOP waiver.
        h.now[0] = h.now[0].replace(hour=12, minute=0, second=0)
        h.service._latest_quotes_by_symbol["AIXI"] = {
            "bid": D("2.63"), "ask": D("2.64"), "received_at": h.now[0]}
        original = h.runtime.backend._fetch_entry_update
        fresh_attempts = []

        async def unavailable_read(request, runtime):
            if request.metadata.get("momlive_fresh_order_read") == "true":
                fresh_attempts.append(request.client_order_id)
                if unavailable == "budget":
                    raise QueryBudgetUnavailable("detail query budget unavailable")
                raise TimeoutError("controlled broker transport unavailable")
            return None  # Ordinary polling also has no affirmative terminal proof.

        monkeypatch.setattr(h.runtime.backend, "_fetch_entry_update", unavailable_read)
        if reason == "KILLSW1_STOP":
            # Native release must finish BEFORE the genuine STOP receipt gate.
            monkeypatch.setattr(h.runtime, "_control_receipt_ready", AsyncMock(return_value=False))
        await h.runtime._exit(position, reason)
        assert fresh_attempts and set(fresh_attempts) == {handle.base_coid}
        assert [(kind, coid) for kind, coid in h.venue.calls if kind == "cancel"] == [("cancel", handle.base_coid)]
        assert (await h.runtime._inspect(h.identity))["exited"] == 0
        assert position.release_unknown and position.remaining == 31
        assert not [row for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]
        await h.runtime._exit(position, reason)
        assert sum(kind == "cancel" for kind, _ in h.venue.calls) == 1
        assert not [row for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]

        # Actual terminal proof, not the cancel ACK, finally releases the target.
        monkeypatch.setattr(h.runtime.backend, "_fetch_entry_update", original)
        observed = await h.runtime._observe(h.identity)
        assert not observed.position.release_unknown
        if reason != "KILLSW1_STOP":
            await h.runtime._exit(observed.position, reason)
            exits = [row for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]
            assert len(exits) == 1 and exits[0].request.quantity == 31
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("defect", ["coid", "broker_id", "symbol", "side", "overfill"])
async def test_mandatory_exit_cannot_turn_conflicting_fresh_identity_into_unavailable_permission(
    tmp_path, monkeypatch, provider, defect,
):
    h = await seed(tmp_path, provider=provider)
    try:
        position, handle = await held_case(h)
        request = h.venue.orders[handle.base_coid].request
        report = h.venue.report(request)
        mutation = {"coid": {"client_order_id": "FOREIGN"},
            "broker_id": {"broker_order_id": "FOREIGN"}, "symbol": {"symbol": "FOREIGN"},
            "side": {"side": "buy"}, "overfill": {"event_type": "filled", "filled_quantity": D(32),
                                                      "fill_price": D("3.27")}}[defect]
        bad = replace(report, **mutation)

        async def wrong_read(request, runtime):
            return bad if request.metadata.get("momlive_fresh_order_read") == "true" else None

        monkeypatch.setattr(h.runtime.backend, "_fetch_entry_update", wrong_read)
        h.runtime.runtime[h.identity]["soft_exit_reason"] = "hard_stop_15pct"
        with pytest.raises(Refusal):
            await h.runtime.backend.cancel(handle, h.runtime)
        assert not [coid for kind, coid in h.venue.calls if kind == "cancel"]
        assert (await h.runtime._inspect(h.identity))["exited"] == 0
        assert not [row for row in h.venue.orders.values() if row.request.metadata.get("exit_reason")]
    finally:
        h.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
async def test_missing_owned_root_never_authorizes_mandatory_cancel(tmp_path, provider):
    h = await seed(tmp_path, provider=provider)
    try:
        _, handle = await held_case(h)
        h.runtime.runtime[h.identity]["soft_exit_reason"] = "time_300s"
        missing = replace(handle, base_coid="missing-original-order")
        with pytest.raises(Refusal, match="native_durable_root_identity_UNKNOWN"):
            await h.runtime.backend.cancel(missing, h.runtime)
        assert not [coid for kind, coid in h.venue.calls if kind == "cancel"]
    finally:
        h.engine.dispose()
