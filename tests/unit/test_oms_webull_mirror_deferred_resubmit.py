from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings


SEGMENT = "1789655403195"
SLOT = "d2551f46-de22-5d59-95fd-329181455f4f"
SYMBOL = "MEDS"
ACCOUNT = "live:orb"


class _Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, message, *args) -> None:
        self.lines.append(message % args)

    def warning(self, message, *args) -> None:
        self.lines.append(message % args)

    def exception(self, message, *args) -> None:
        self.lines.append(message % args)


class _CaptureRedis:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict[str, str], dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        self.rows.append((stream, fields, kwargs))
        return "1-0"


def _settings(*, enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        oms_v2_webull_mirror_deferred_resubmit_enabled=enabled,
        oms_v2_eh_resting_entry_quote_max_age_ms=2000,
        redis_stream_prefix="mai-tai",
        redis_strategy_intent_stream_maxlen=1000,
        provider_for_account=lambda account: "webull" if account == ACCOUNT else "schwab",
    )


def _service(*, enabled: bool = True) -> OmsRiskService:
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = _settings(enabled=enabled)
    service.logger = _Capture()
    service.redis = _CaptureRedis()
    service._latest_quotes_by_symbol = {}
    service._latest_trades_by_symbol = {}
    return service


def _queued_events(service: OmsRiskService) -> list[TradeIntentEvent]:
    return [
        TradeIntentEvent.model_validate_json(fields["data"])
        for _, fields, _ in service.redis.rows
    ]


def _mirror_event(
    *,
    slot_id: str = SLOT,
    segment_id: str = SEGMENT,
    account: str = ACCOUNT,
    intent_type: str = "open",
    include_slot: bool = True,
) -> TradeIntentEvent:
    metadata = {
        "path": "ATR Flip",
        "atr_variant": "CW-v2-fanout",
        "fanout_leg": "webull",
        "fanout_source": "rth_resting_mirror",
        "resting_entry": "true",
        "fanout_segment_id": segment_id,
        "order_type": "STOP_LIMIT",
        "stop_price": "10.0000",
        "limit_price": "10.0500",
        "reference_price": "10.0000",
    }
    if include_slot:
        metadata["fanout_slot_id"] = slot_id
    if intent_type == "cancel":
        metadata["resting_entry_cancel"] = "true"
    return TradeIntentEvent(
        source_service="schwab-1m-v2",
        payload=TradeIntentPayload(
            strategy_code="schwab_1m_v2",
            broker_account_name=account,
            symbol=SYMBOL,
            side="buy",
            quantity=Decimal("1"),
            intent_type=intent_type,  # type: ignore[arg-type]
            reason="webull resting mirror",
            metadata=metadata,
        ),
    )


def _primary_event(*, account: str = "live:schwab_1m_v2") -> TradeIntentEvent:
    event = _mirror_event(account=account)
    event.payload.metadata.pop("fanout_leg")
    return event


def _report(
    event_type: str,
    *,
    error_code: str = "",
    reason: str = "",
) -> ExecutionReport:
    metadata = {"webull_error_code": error_code} if error_code else {}
    return ExecutionReport(
        event_type=event_type,  # type: ignore[arg-type]
        client_order_id="wire-1",
        symbol=SYMBOL,
        side="buy",
        intent_type="open",
        quantity=Decimal("1"),
        filled_quantity=Decimal("1") if event_type == "filled" else Decimal("0"),
        fill_price=Decimal("10") if event_type == "filled" else None,
        reason=reason,
        metadata=metadata,
        origin="broker",
    )


def _price_reject() -> ExecutionReport:
    return _report(
        "rejected",
        error_code="ORDER_RISK_RULE_PRICE_AGGRESSIVE",
        reason="Webull order rejected: ORDER_RISK_RULE_PRICE_AGGRESSIVE (http 417)",
    )


def _defer(service: OmsRiskService, event: TradeIntentEvent | None = None) -> TradeIntentEvent:
    mirror = event or _mirror_event()
    service._observe_webull_mirror_deferred_reports(event=mirror, reports=[_price_reject()])
    return mirror


def test_pa1_defaults_off_and_reason_text_without_structured_code_is_not_a_trigger() -> None:
    assert Settings().oms_v2_webull_mirror_deferred_resubmit_enabled is False

    disabled = _service(enabled=False)
    _defer(disabled)
    assert disabled.__dict__.get("_webull_mirror_deferred_by_slot", {}) == {}

    enabled = _service()
    enabled._observe_webull_mirror_deferred_reports(
        event=_mirror_event(),
        reports=[
            _report(
                "rejected",
                reason="Webull order rejected: ORDER_RISK_RULE_PRICE_AGGRESSIVE (http 417)",
            )
        ],
    )
    assert enabled.__dict__.get("_webull_mirror_deferred_by_slot", {}) == {}


@pytest.mark.asyncio
async def test_price_aggressive_mirror_queues_the_serial_pipeline_inside_eight_percent(
    monkeypatch,
) -> None:
    service = _service()
    original = _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.2000"),
        "received_at": datetime.now(UTC) - timedelta(milliseconds=100),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)

    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)

    queued = _queued_events(service)
    assert len(queued) == 1
    assert queued[0].event_id != original.event_id
    assert queued[0].payload.metadata["stop_price"] == "10.0000"
    assert queued[0].payload.metadata["limit_price"] == "10.0500"
    assert queued[0].payload.metadata["webull_deferred_resubmit"] == "true"
    assert queued[0].payload.metadata["webull_deferred_resubmit_attempt"] == "1"
    assert service._webull_mirror_deferred_by_slot[SLOT].queued is True
    assert any("decision=queued" in line for line in service.logger.lines)


@pytest.mark.asyncio
async def test_market_more_than_eight_percent_below_the_stop_does_not_resubmit(
    monkeypatch,
) -> None:
    service = _service()
    _defer(service)
    service._latest_trades_by_symbol[SYMBOL] = {
        "price": Decimal("9.1999"),
        "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)

    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)

    assert _queued_events(service) == []
    assert SLOT in service._webull_mirror_deferred_by_slot


@pytest.mark.asyncio
async def test_market_older_than_two_seconds_does_not_resubmit(monkeypatch) -> None:
    service = _service()
    _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.5"),
        "received_at": datetime.now(UTC) - timedelta(seconds=3),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)

    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)

    assert _queued_events(service) == []
    assert SLOT in service._webull_mirror_deferred_by_slot


@pytest.mark.asyncio
async def test_resubmits_are_capped_at_three_per_slot(monkeypatch) -> None:
    service = _service()
    _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.5"),
        "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)

    for _ in range(3):
        service._latest_quotes_by_symbol[SYMBOL]["received_at"] = datetime.now(UTC)
        await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
        queued = _queued_events(service)[-1]
        service._observe_webull_mirror_deferred_reports(
            event=queued,
            reports=[_price_reject()],
        )

    assert [
        event.payload.metadata["webull_deferred_resubmit_attempt"]
        for event in _queued_events(service)
    ] == [
        "1",
        "2",
        "3",
    ]
    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=attempt_cap_reached" in line for line in service.logger.lines)


@pytest.mark.asyncio
async def test_repeated_ticks_enqueue_only_once_until_the_serial_lane_finishes(
    monkeypatch,
) -> None:
    service = _service()
    _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.5"),
        "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)

    for _ in range(3):
        service._latest_quotes_by_symbol[SYMBOL]["received_at"] = datetime.now(UTC)
        await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)

    assert len(_queued_events(service)) == 1


@pytest.mark.asyncio
async def test_cancel_ahead_of_a_queued_resubmit_prevents_the_broker_submit(
    monkeypatch,
) -> None:
    service = _service()
    _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.5"),
        "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
    queued = _queued_events(service)[0]

    # Both events share the serial lane. If the cancel wins, it retires the claim before the
    # queued retry can enter the broker pipeline.
    service._observe_webull_mirror_deferred_intent(_mirror_event(intent_type="cancel"))
    submitted: list[TradeIntentEvent] = []

    async def _process(event: TradeIntentEvent):
        submitted.append(event)
        return []

    service.process_trade_intent = _process  # type: ignore[method-assign]
    await service._handle_stream_message({"data": queued.model_dump_json()})

    assert submitted == []
    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=slot_claim_no_longer_current" in line for line in service.logger.lines)


@pytest.mark.asyncio
async def test_non_price_aggressive_resubmit_outcome_forgets_the_claimed_slot(
    monkeypatch,
) -> None:
    service = _service()
    _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.5"),
        "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
    queued = _queued_events(service)[0]

    async def _process(event: TradeIntentEvent):
        service._observe_webull_mirror_deferred_reports(
            event=event,
            reports=[
                _report(
                    "rejected",
                    error_code="TOO_MANY_REQUESTS",
                    reason="Webull order rejected: too many requests (http 429)",
                )
            ],
        )
        return []

    service.process_trade_intent = _process  # type: ignore[method-assign]
    await service._handle_stream_message({"data": queued.model_dump_json()})

    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any(
        "reason=resubmit_finished_without_price_aggressive_reject" in line
        for line in service.logger.lines
    )


def test_cancel_for_the_slot_forgets_even_when_the_broker_target_is_missing() -> None:
    service = _service()
    _defer(service)

    service._observe_webull_mirror_deferred_intent(_mirror_event(intent_type="cancel"))

    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=v2_cancel_intent" in line for line in service.logger.lines)


def test_unbound_cancel_forgets_every_deferred_slot_for_the_symbol() -> None:
    service = _service()
    _defer(service)

    service._observe_webull_mirror_deferred_intent(
        _mirror_event(intent_type="cancel", include_slot=False)
    )

    assert service._webull_mirror_deferred_by_slot == {}
    assert any("reason=v2_cancel_intent_unbound" in line for line in service.logger.lines)


@pytest.mark.parametrize("account", ["live:schwab_1m_v2", ACCOUNT])
def test_a_fill_on_either_account_forgets_the_deferred_slot(account: str) -> None:
    service = _service()
    _defer(service)

    service._observe_webull_mirror_deferred_reports(
        event=_primary_event(account=account),
        reports=[_report("filled")],
    )

    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=slot_filled" in line for line in service.logger.lines)


def test_a_new_segment_or_slot_for_the_symbol_forgets_the_old_slot() -> None:
    service = _service()
    _defer(service)

    service._observe_webull_mirror_deferred_intent(
        _mirror_event(slot_id="replacement-slot", segment_id="1789659999000")
    )

    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=new_segment_or_slot" in line for line in service.logger.lines)


@pytest.mark.asyncio
async def test_end_of_the_resting_window_forgets_without_resubmitting(monkeypatch) -> None:
    service = _service()
    _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.5"),
        "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: False)

    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)

    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=resting_window_ended" in line for line in service.logger.lines)


def test_a_new_service_process_cannot_see_deferred_state_from_the_old_process() -> None:
    old_process = _service()
    _defer(old_process)

    new_process = _service()

    assert SLOT in old_process._webull_mirror_deferred_by_slot
    assert new_process._webull_mirror_deferred_by_slot == {}


def test_an_accepted_resubmit_forgets_before_another_quote_can_duplicate_it() -> None:
    service = _service()
    event = _defer(service)

    service._observe_webull_mirror_deferred_reports(
        event=event,
        reports=[_report("accepted")],
    )

    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=resubmit_accepted" in line for line in service.logger.lines)


def test_a_primary_acceptance_cannot_forget_a_rejected_webull_mirror() -> None:
    service = _service()
    _defer(service)

    service._observe_webull_mirror_deferred_reports(
        event=_primary_event(),
        reports=[_report("accepted")],
    )

    assert SLOT in service._webull_mirror_deferred_by_slot


def test_pa1_is_wired_to_intents_reports_and_both_market_tick_paths() -> None:
    assert "self._observe_webull_mirror_deferred_intent(event)" in inspect.getsource(
        OmsRiskService.process_trade_intent
    )
    assert "self._observe_webull_mirror_deferred_reports(" in inspect.getsource(
        OmsRiskService._record_order_reports
    )
    assert "self._observe_webull_mirror_deferred_reports(" in inspect.getsource(
        OmsRiskService.sync_broker_orders
    )
    assert "await self._evaluate_webull_mirror_deferred_resubmits(symbol)" in inspect.getsource(
        OmsRiskService._handle_quote_tick_event
    )
    assert "await self._evaluate_webull_mirror_deferred_resubmits(symbol)" in inspect.getsource(
        OmsRiskService._handle_trade_tick_event
    )
    evaluator = inspect.getsource(OmsRiskService._evaluate_webull_mirror_deferred_resubmits)
    assert "self.redis.xadd(" in evaluator
    assert "process_trade_intent" not in evaluator
    assert "self._claim_webull_mirror_deferred_resubmit(event)" in inspect.getsource(
        OmsRiskService._handle_stream_message
    )


def test_reconciliation_builds_the_pa1_observer_after_ledger_writes() -> None:
    source = inspect.getsource(OmsRiskService.sync_broker_orders)
    observed = source.index("observed_event = TradeIntentEvent(")

    assert source.index("self.store.update_order_from_report(") < observed
    assert source.index("self.store.mark_intent_from_report(intent, report)") < observed
