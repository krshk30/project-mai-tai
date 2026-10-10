"""Synthetic real-method reader trace. No live data, wire calls, SQL or orders."""
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from project_mai_tai.broker_adapters.protocols import BrokerPositionSnapshot
from project_mai_tai.oms import orb_schwab_eod, wbquiet_shadow as shadow
from project_mai_tai.oms.service import OmsRiskService

WB, SCHWAB = "live:webull_1m_v2", "live:schwab_1m_v2"
AT = datetime(2026, 10, 8, 14, tzinfo=UTC)


async def replay():
    clock, pulls = [100.0], []
    service = OmsRiskService.__new__(OmsRiskService)
    service.logger = Mock()
    service._wbquiet_shadow = shadow.ShadowObserver()
    service.broker_adapter = SimpleNamespace(
        provider_by_account={WB: "webull", SCHWAB: "schwab"}, _adapters_by_provider={},
    )
    async def positions(name):
        pulls.append({"account": name, "observed_time": clock[0]})
        return [BrokerPositionSnapshot(broker_account_name=name, symbol="IPDN",
                                       quantity=Decimal("2"), average_price=Decimal("5"), as_of=AT)]
    service.broker_adapter.list_account_positions = AsyncMock(side_effect=positions)
    service.store = SimpleNamespace(sync_account_positions=Mock(),
                                    get_account_position=Mock(return_value=SimpleNamespace(quantity=Decimal("2"))))
    service.session_factory = Mock(side_effect=AssertionError("unexpected SQL"))
    with patch.object(shadow.time, "monotonic", lambda: clock[0]):
        frame = shadow.Frame(71, AT, clock[0], {WB: {"known": True, "fresh": True}})
        token = shadow.CURRENT.set(frame)
        try:
            await service.broker_adapter.list_account_positions(WB)
            shadow.note_read(WB, "returned_not_wire_proof")
            # Synthetic transaction-success input, not a claimed database commit.
            shadow.note_committed([WB])
        finally:
            shadow.CURRENT.reset(token)
        await service._wbquiet_shadow.finish(service, frame, "synthetic_ok")
        clock[0] = 159
        reconcile = await service._broker_symbol_position_state(WB, "IPDN")
        refresh = await service._refresh_broker_position_quantity(
            session=object(), broker_account_id=uuid4(), broker_account_name=WB, symbol="IPDN",
        )
        target = SimpleNamespace(account=SCHWAB, symbol="IPDN", quantity=Decimal("2"))
        closes = [await orb_schwab_eod._broker_quantity(service, target) for _ in range(2)]
        clock[0] = 160
        await service._broker_symbol_position_state(WB, "IPDN")
        clock[0] = 160.001
        await service._broker_symbol_position_state(WB, "IPDN")
    receipts = [json.loads(c.args[1]) for c in service.logger.info.call_args_list
                if c.args[0] in {"[WBQUIET-SHADOW] %s", "[WBQUIET-READER] %s"}]
    service.session_factory.assert_not_called()
    assert reconcile.value == "held" and refresh == 2 and closes == [2, 2]
    assert len(pulls) == 7 and len(receipts) == 6
    return {"kind": "synthetic_real_method_trace_not_historical_or_live_replay",
            "import_path": shadow.__file__, "fixture_as_of": AT.isoformat(),
            "real_network_pulls": 0, "production_db_or_redis_writes": 0, "broker_orders": 0,
            "synthetic_adapter_calls": pulls, "receipts": receipts,
            "after_window": "adapter_read_proceeds_without_reader_receipt_not_zero_evidence",
            "production_reader_coverage": "PARTIAL", "wire_calls_saved": "UNMEASURED"}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(replay()), indent=2, sort_keys=True))
