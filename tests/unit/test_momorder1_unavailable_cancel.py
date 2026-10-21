"""Mandatory UNKNOWN: exact known identity permits cancel, never a second SELL.

The normalized broker-read port injects transport/budget outcomes. Production
fresh-read classification, cancellation latches, release and DB writes stay real.
"""

from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live.admission import Refusal
from tests.unit.test_momorder1_reads import body as webull_body
from tests.unit.test_momorder1_runtime_db import held_case, seed
from tests.unit.test_webull_adapter import _FakeClient
from tests.unit.test_webull_adapter import fake_sdk as fake_sdk
from tests.unit.test_webull_list_primary import Clock, primary


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["webull", "schwab"])
@pytest.mark.parametrize("reason", ["hard_stop_15pct", "time_300s", "KILLSW1_STOP"])
@pytest.mark.parametrize("unavailable", ["budget", "transport"])
async def test_mandatory_unavailable_fresh_read_cancels_exact_owned_id_once_without_releasing(
    tmp_path, monkeypatch, fake_sdk, provider, reason, unavailable,
):
    h = await seed(tmp_path, provider=provider)
    try:
        position, handle = await held_case(h)
        # Both venue sessions are open at 08:00 ET; no pre-07 Schwab STOP waiver.
        h.now[0] = h.now[0].replace(hour=12, minute=0, second=0)
        h.service._latest_quotes_by_symbol["AIXI"] = {
            "bid": D("2.63"), "ask": D("2.64"), "received_at": h.now[0]}
        root, _ = await h.runtime.backend.root(handle, h.runtime)
        fresh_attempts = []
        if provider == "webull":
            client = _FakeClient({"detail": webull_body(root["request"], status="SUBMITTED", filled="0")})
            leaf = primary(client, Clock())
            monkeypatch.setattr(h.venue, "webull", leaf)
            if unavailable == "budget":
                leaf._query_budget.claim("detail", "ATR-terminal", strict=True)
                leaf._query_budget.claim("detail", "ORB-terminal", strict=True)
            else:
                client.raises["detail"] = TimeoutError("controlled broker transport unavailable")
            claim = leaf._query_budget.claim

            def attempted_claim(endpoint, owner, *, strict=False):
                fresh_attempts.append(handle.base_coid)
                return claim(endpoint, owner, strict=strict)

            monkeypatch.setattr(leaf._query_budget, "claim", attempted_claim)
            original = h.venue.fetch_order_update

            async def ordinary_unknown(request):
                return None

            monkeypatch.setattr(h.venue, "fetch_order_update", ordinary_unknown)
        else:
            leaf = h.venue.schwab
            original = leaf._authorized_request_json
            ordinary = leaf._fetch_order

            async def unavailable_http(method, path, *, body=None):
                assert method == "GET" and path.endswith("venue-" + handle.base_coid)
                fresh_attempts.append(handle.base_coid)
                if unavailable == "budget":
                    return 429, {}, {}  # Typed HTTP unavailability, not a forged broker state.
                raise TimeoutError("controlled broker transport unavailable")

            async def ordinary_unknown(*args):
                return None

            monkeypatch.setattr(leaf, "_authorized_request_json", unavailable_http)
            monkeypatch.setattr(leaf, "_fetch_order", ordinary_unknown)
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
        if provider == "webull":
            monkeypatch.setattr(h.venue, "fetch_order_update", original)
        else:
            monkeypatch.setattr(leaf, "_authorized_request_json", original)
            monkeypatch.setattr(leaf, "_fetch_order", ordinary)
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
    tmp_path, monkeypatch, fake_sdk, provider, defect,
):
    h = await seed(tmp_path, provider=provider)
    try:
        position, handle = await held_case(h)
        root, _ = await h.runtime.backend.root(handle, h.runtime)
        request = root["request"]
        if provider == "webull":
            raw = webull_body(request, status="SUBMITTED", filled="0")
            if defect in {"coid", "broker_id"}:
                raw["client_order_id" if defect == "coid" else "order_id"] = "FOREIGN"
            else:
                key, value = {"symbol": ("symbol", "FOREIGN"), "side": ("side", "BUY"),
                    "overfill": ("filled_qty", "32")}[defect]
                raw["items"][0][key] = value
            monkeypatch.setattr(h.venue, "webull", primary(_FakeClient({"detail": raw}), Clock()))
        else:
            _, _, raw = await h.venue.http("GET", "/orders/venue-" + handle.base_coid)
            if defect in {"coid", "broker_id"}:
                # Schwab has no client-id field on this endpoint: wrong native ID
                # is the corresponding identity conflict for either replay label.
                raw["orderId"] = "FOREIGN"
            elif defect == "symbol":
                raw["orderLegCollection"][0]["instrument"]["symbol"] = "FOREIGN"
            elif defect == "side":
                raw["orderLegCollection"][0]["instruction"] = "BUY"
            else:
                raw["filledQuantity"] = "32"

            async def wrong_http(method, path, *, body=None):
                assert method == "GET"
                return 200, {}, raw

            monkeypatch.setattr(h.venue.schwab, "_authorized_request_json", wrong_http)
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
