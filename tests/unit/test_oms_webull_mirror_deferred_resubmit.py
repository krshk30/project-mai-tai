from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import inspect
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.broker_adapters.protocols import ExecutionReport
from project_mai_tai.broker_adapters.simulated import SimulatedBrokerAdapter
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerOrder, DashboardSnapshot, TradeIntent
from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.fanout_outcome_consumer import OUTCOME_SNAPSHOT_TYPE
from project_mai_tai.oms import service as service_module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy


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


class _FakeRedis:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, object]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs: object) -> str:
        del kwargs
        self.entries.append((stream, json.loads(fields["data"])))
        return "1-0"

    async def get(self, key: str) -> None:
        del key
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        del key, value, ex
        return True

    async def xread(self, offsets: object, block: int = 0, count: int = 0) -> list[object]:
        del offsets, block, count
        return []

    async def aclose(self) -> None:
        return None


class _RecordingAdapter(SimulatedBrokerAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[object] = []

    async def submit_order(self, request):  # type: ignore[no-untyped-def]
        self.requests.append(request)
        return await super().submit_order(request)


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


def _session_factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _integrated_service(
    factory: sessionmaker[Session],
    *,
    enabled: bool,
) -> tuple[OmsRiskService, _RecordingAdapter]:
    adapter = _RecordingAdapter()
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            oms_v2_webull_mirror_deferred_resubmit_enabled=enabled,
            strategy_schwab_1m_v2_dual_broker_fanout_enabled=True,
            strategy_schwab_1m_v2_webull_account_name=ACCOUNT,
            strategy_schwab_1m_v2_webull_resting_mirror_enabled=True,
        ),
        redis_client=_FakeRedis(),
        session_factory=factory,
        broker_adapter=adapter,
    )
    service._fanout_webull_collision_reason = lambda **kwargs: None  # type: ignore[method-assign]
    return service, adapter


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
        "fanout_slot": "resting",
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


def _stamp_market(
    service: OmsRiskService,
    event: TradeIntentEvent,
    *,
    price: Decimal,
    age: timedelta = timedelta(milliseconds=100),
) -> None:
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": price,
        "received_at": datetime.now(UTC) - age,
    }
    service._stamp_webull_resting_mirror_market(event)


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
async def test_precheck_defers_a_twelve_percent_distant_mirror_before_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _session_factory()
    service, adapter = _integrated_service(factory, enabled=True)
    event = _mirror_event()
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("8.8000"),
        "received_at": datetime.now(UTC) - timedelta(milliseconds=100),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(OmsRiskService, "_market_is_fillable", lambda self, now=None: True)

    results = await service.process_trade_intent(event)

    assert adapter.requests == []
    assert results[-1].payload.reason == "webull_mirror_precheck_deferred"
    state = service._webull_mirror_deferred_by_slot[SLOT]
    assert state.attempts == 0
    assert state.queued is False
    with factory() as session:
        intent = session.scalar(select(TradeIntent))
        assert intent is not None
        assert intent.payload["refusal_origin"] == "skipped_before_submit"
        assert intent.payload["refusal_code"] == "webull_mirror_precheck_deferred"
        assert session.scalars(select(BrokerOrder)).all() == []
        outcomes = session.scalars(
            select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == OUTCOME_SNAPSHOT_TYPE
            )
        ).all()
        deferred = [
            row
            for row in outcomes
            if row.payload.get("reason") == "webull_mirror_precheck_deferred"
        ]
        assert len(deferred) == 1
        assert deferred[0].payload["event_source"] == "client"
        assert deferred[0].payload["outcome"] == "queued"


@pytest.mark.asyncio
async def test_precheck_is_byte_identical_when_the_pa1_flag_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = _session_factory()
    service, adapter = _integrated_service(factory, enabled=False)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("8.8000"),
        "received_at": datetime.now(UTC) - timedelta(milliseconds=100),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    monkeypatch.setattr(OmsRiskService, "_market_is_fillable", lambda self, now=None: True)

    await service.process_trade_intent(_mirror_event())

    assert len(adapter.requests) == 1
    assert service.__dict__.get("_webull_mirror_deferred_by_slot", {}) == {}


@pytest.mark.parametrize(
    "stamped_at",
    [
        None,
        (datetime.now(UTC) - timedelta(seconds=3)).isoformat(),
    ],
    ids=["missing", "stale"],
)
def test_precheck_does_not_defer_without_a_fresh_market(stamped_at: str | None) -> None:
    service = _service()
    event = _mirror_event()
    if stamped_at is not None:
        event.payload.metadata["webull_shape_market_price"] = "8.8000"
        event.payload.metadata["webull_shape_market_at_utc"] = stamped_at

    assert service._defer_webull_resting_mirror_before_submit(event) is False
    assert service.__dict__.get("_webull_mirror_deferred_by_slot", {}) == {}


def test_precheck_deferred_slot_is_forgotten_by_the_v2_cancel() -> None:
    service = _service()
    event = _mirror_event()
    _stamp_market(service, event, price=Decimal("8.8000"))
    assert service._defer_webull_resting_mirror_before_submit(event) is True

    service._observe_webull_mirror_deferred_intent(_mirror_event(intent_type="cancel"))

    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=v2_cancel_intent" in line for line in service.logger.lines)


def test_precheck_uses_the_same_exact_eight_percent_boundary_as_resubmit() -> None:
    at_boundary = _service()
    boundary_event = _mirror_event()
    _stamp_market(at_boundary, boundary_event, price=Decimal("9.2000"))
    assert at_boundary._defer_webull_resting_mirror_before_submit(boundary_event) is False

    outside_boundary = _service()
    outside_event = _mirror_event()
    _stamp_market(outside_boundary, outside_event, price=Decimal("9.1999"))
    assert outside_boundary._defer_webull_resting_mirror_before_submit(outside_event) is True


@pytest.mark.asyncio
async def test_cancel_ahead_of_precheck_deferred_retry_leaves_no_working_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    event = _mirror_event()
    _stamp_market(service, event, price=Decimal("8.8000"))
    assert service._defer_webull_resting_mirror_before_submit(event) is True
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.5000"),
        "received_at": datetime.now(UTC),
    }
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
    queued = _queued_events(service)[0]

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
async def test_precheck_resubmit_attempts_never_reset_and_stop_after_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    clock = [100.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)

    original = _mirror_event()
    _stamp_market(service, original, price=Decimal("8.8000"))
    assert service._defer_webull_resting_mirror_before_submit(original) is True
    assert service._webull_mirror_deferred_by_slot[SLOT].attempts == 0

    queued_count = 0
    observed_attempts: list[int] = []
    for _ in range(12):
        if SLOT not in service._webull_mirror_deferred_by_slot:
            continue
        clock[0] += 5.0
        service._latest_quotes_by_symbol[SYMBOL] = {
            "ask": Decimal("9.2500"),
            "received_at": datetime.now(UTC),
        }
        await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
        queued = _queued_events(service)
        if len(queued) == queued_count:
            continue
        queued_count = len(queued)
        retry = queued[-1]
        attempt = int(retry.payload.metadata["webull_deferred_resubmit_attempt"])
        assert service._claim_webull_mirror_deferred_resubmit(retry) is True

        _stamp_market(service, retry, price=Decimal("9.1000"))
        assert service._defer_webull_resting_mirror_before_submit(retry) is True
        service._finish_webull_mirror_deferred_resubmit(retry)
        observed_attempts.append(attempt)
        if SLOT in service._webull_mirror_deferred_by_slot:
            assert service._webull_mirror_deferred_by_slot[SLOT].attempts == attempt

    assert observed_attempts == [1, 2, 3]
    assert SLOT not in service._webull_mirror_deferred_by_slot
    assert any("reason=attempt_cap_reached" in line for line in service.logger.lines)


@pytest.mark.asyncio
async def test_precheck_resubmit_waits_five_seconds_before_rearming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    clock = [100.0]
    monkeypatch.setattr(service_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(service_module, "_is_regular_market_session", lambda now=None: True)
    _defer(service)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.2500"),
        "received_at": datetime.now(UTC),
    }
    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
    retry = _queued_events(service)[0]
    assert retry.payload.metadata["webull_deferred_resubmit_attempt"] == "1"

    _stamp_market(service, retry, price=Decimal("9.1000"))
    assert service._defer_webull_resting_mirror_before_submit(retry) is True
    service._finish_webull_mirror_deferred_resubmit(retry)
    service._latest_quotes_by_symbol[SYMBOL] = {
        "ask": Decimal("9.2500"),
        "received_at": datetime.now(UTC),
    }

    clock[0] += 4.999
    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
    assert len(_queued_events(service)) == 1

    clock[0] += 0.001
    await service._evaluate_webull_mirror_deferred_resubmits(SYMBOL)
    assert [
        event.payload.metadata["webull_deferred_resubmit_attempt"]
        for event in _queued_events(service)
    ] == ["1", "2"]


def test_precheck_cannot_decrease_an_existing_slot_attempt() -> None:
    service = _service()
    event = _mirror_event()
    event.payload.metadata.update(
        {
            "webull_deferred_resubmit": "true",
            "webull_deferred_resubmit_attempt": "1",
        }
    )
    service._remember_webull_mirror_deferred(
        event=event,
        segment_id=SEGMENT,
        slot_id=SLOT,
        stop_price=Decimal("10"),
        attempts=2,
    )
    service._remember_webull_mirror_deferred(
        event=event,
        segment_id=SEGMENT,
        slot_id=SLOT,
        stop_price=Decimal("10"),
        attempts=1,
    )
    assert service._webull_mirror_deferred_by_slot[SLOT].attempts == 2

    _stamp_market(service, event, price=Decimal("9.1000"))

    assert service._defer_webull_resting_mirror_before_submit(event) is True
    assert service._webull_mirror_deferred_by_slot[SLOT].attempts == 2


def test_v2_claim_expiry_cannot_emit_a_second_leg_while_the_mirror_is_active() -> None:
    release = inspect.getsource(SchwabV2Strategy._release_fanout_webull_claim)
    cross = inspect.getsource(SchwabV2Strategy._fanout_rth_resting_cross)
    on_fill = inspect.getsource(SchwabV2Strategy.update_position)

    assert "webull_resting_active" not in release
    after_claim_gate = cross.split(
        "if state.position_qty != 0 or state.fanout_webull_claimed:",
        maxsplit=1,
    )[1]
    assert after_claim_gate.index("if state.webull_resting_active:") < after_claim_gate.index(
        "self._claim_fanout_webull("
    )
    assert "and not state.webull_resting_active" in on_fill


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
