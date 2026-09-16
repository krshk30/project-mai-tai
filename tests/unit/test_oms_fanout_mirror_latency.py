from __future__ import annotations

from datetime import UTC, datetime, timedelta
import inspect
from types import SimpleNamespace

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.oms.service import OmsRiskService


class _Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message, *args) -> None:
        self.lines.append(message % args)


def _event(*, leg: str) -> SimpleNamespace:
    metadata = {
        "atr_variant": "CW-v2-resting" if not leg else "CW-v2-fanout",
        "resting_entry": "true",
        "fanout_segment_id": "1789565403195",
        "fanout_slot_id": "d2551f46-de22-5d59-95fd-329181455f4f",
    }
    if leg:
        metadata.update(
            {
                "fanout_leg": leg,
                "fanout_source": "rth_resting_mirror",
                "order_type": "STOP_LIMIT",
            }
        )
    return SimpleNamespace(
        payload=SimpleNamespace(
            strategy_code="schwab_1m_v2",
            intent_type="open",
            side="buy",
            symbol="MEDS",
            metadata=metadata,
        )
    )


def _service(now: datetime) -> OmsRiskService:
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = SimpleNamespace(
        oms_v2_eh_resting_entry_quote_max_age_ms=2000,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
    )
    service.logger = _Capture()
    service._latest_quotes_by_symbol = {
        "MEDS": {"ask": 4.31, "received_at": now - timedelta(milliseconds=100)}
    }
    service._latest_trades_by_symbol = {}
    service._fanout_primary_dispatch_at = {}
    return service


def test_meds_timeline_stamps_the_fresh_ask_and_logs_subsecond_wire_lag() -> None:
    primary_at = datetime.now(UTC)
    wire_at = primary_at + timedelta(milliseconds=420)
    service = _service(primary_at)
    mirror = _event(leg="webull")
    key = service._resting_fanout_pair_key(mirror)
    assert key is not None
    service._fanout_primary_dispatch_at[key] = primary_at

    service._stamp_webull_resting_mirror_market(mirror)
    service._emit_fanout_mirror_lag(
        event=mirror,
        reports=[
            ExecutionReport(
                event_type="accepted",
                client_order_id="mirror",
                metadata={
                    **mirror.payload.metadata,
                    "webull_wire_submitted_at_utc": wire_at.isoformat(),
                    "webull_resting_mirror_shape": "converted_to_limit",
                },
            )
        ],
    )

    assert mirror.payload.metadata["webull_shape_market_price"] == "4.31"
    assert mirror.payload.metadata["webull_shape_market_source"] == "ask"
    assert any(
        "[OMS-FANOUT-MIRROR-LAG]" in line
        and "lag_ms=420" in line
        and "shape=converted_to_limit" in line
        for line in service.logger.lines
    )


def test_only_the_schwab_primary_skips_inline_reconciliation() -> None:
    service = _service(datetime.now(UTC))
    assert service._is_resting_fanout_primary(_event(leg="")) is True
    assert service._is_resting_fanout_primary(_event(leg="webull")) is False


def test_process_wiring_keeps_the_primary_reconcile_off_the_serial_lane() -> None:
    source = inspect.getsource(OmsRiskService.process_trade_intent)
    assert "self._stamp_webull_resting_mirror_market(event)" in source
    assert "self._emit_fanout_mirror_lag(event=event, reports=reports)" in source
    assert "if not self._is_resting_fanout_primary(event):" in source
    assert "await self._reconcile_after_intent(event.payload.broker_account_name)" in source


def test_control_loop_remains_single_consumer_and_serial() -> None:
    source = inspect.getsource(OmsRiskService._run_control_loop)
    assert "await self._handle_stream_message(fields)" in source
    assert "create_task(self._handle_stream_message" not in source


def test_limit_and_market_fanout_shapes_are_out_of_scope() -> None:
    service = _service(datetime.now(UTC))
    for order_type in ("LIMIT", "MARKET"):
        event = _event(leg="webull")
        event.payload.metadata["order_type"] = order_type
        event.payload.metadata["fanout_source"] = "reactive"
        before = dict(event.payload.metadata)
        service._stamp_webull_resting_mirror_market(event)
        assert event.payload.metadata == before
