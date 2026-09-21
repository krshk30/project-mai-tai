from __future__ import annotations

import ast
import asyncio
import json
import sys
from types import SimpleNamespace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.momentum_paper.conditions import build_condition_snapshot
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MomentumTapeRecord, TradePrint
from project_mai_tai.momentum_paper.store import MomentumPaperStore, session_record
from project_mai_tai.momentum_gateway_handoff import (
    connect_consumer_socket,
    encode_trade_frame,
    ParsedTradeFrame,
)
from project_mai_tai.runtime_registry import (
    configured_broker_account_registrations,
    configured_strategy_registrations,
    polygon_30s_runtime_enabled,
)
from project_mai_tai.services.control_plane import (
    BOT_PAGE_META,
    CONTROL_PLANE_ACTIVE_BOT_CODES,
    _compact_bot_page_url,
)
from project_mai_tai.services.momentum_paper_app import (
    MomentumPaperService,
    decode_raw_messages,
    detector_health_status,
    normalize_raw_trade,
    previous_trading_day,
)
from project_mai_tai.settings import Settings


def _et_ms(clock: str) -> int:
    observed = datetime.combine(
        date(2026, 9, 16),
        datetime.strptime(clock, "%H:%M:%S").time(),
        tzinfo=ZoneInfo("America/New_York"),
    )
    return int(observed.astimezone(UTC).timestamp() * 1000)


def _condition_snapshot():
    return build_condition_snapshot(
        [
            {
                "id": 1,
                "name": "regular",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": True,
                        "updates_open_close": True,
                        "updates_volume": True,
                    }
                },
            },
            {
                "id": 99,
                "name": "cancel",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": False,
                        "updates_open_close": False,
                        "updates_volume": False,
                    }
                },
            },
            {
                "id": 12,
                "name": "Form T/Extended Hours",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": False,
                        "updates_open_close": False,
                        "updates_volume": True,
                    }
                },
            },
            {
                "id": 13,
                "name": "Extended Hours (Sold Out Of Sequence)",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": False,
                        "updates_open_close": False,
                        "updates_volume": True,
                    }
                },
            },
            {
                "id": 14,
                "name": "Intermarket Sweep",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": True,
                        "updates_open_close": True,
                        "updates_volume": True,
                    }
                },
            },
            {
                "id": 37,
                "name": "Odd Lot Trade",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": False,
                        "updates_open_close": False,
                        "updates_volume": True,
                    }
                },
            },
            {
                "id": 41,
                "name": "Trade Thru Exempt",
                "update_rules": {
                    "consolidated": {
                        "updates_high_low": True,
                        "updates_open_close": True,
                        "updates_volume": True,
                    }
                },
            },
        ],
        retrieved_at=datetime(2026, 9, 16, 7, 55, tzinfo=UTC),
    )


def _store() -> MomentumPaperStore:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return MomentumPaperStore(sessionmaker(bind=engine, expire_on_commit=False))


def test_raw_trade_keeps_sip_and_participant_clocks_separate() -> None:
    trade = normalize_raw_trade(
        {
            "ev": "T",
            "sym": "ABCD",
            "t": 1_789_555_200_123,
            "pt": 1_789_555_100_999,
            "p": 2.5,
            "s": 40,
            "i": "wire-id",
            "x": 11,
            "trfi": 501,
            "c": [1],
        },
        conditions=_condition_snapshot(),
    )

    assert trade is not None
    assert trade.sip_ts_ms == 1_789_555_200_123
    assert trade.participant_ts_ms == 1_789_555_100_999
    assert trade.trade_id == "wire-id"
    assert trade.exchange == 11
    assert trade.trf_id == 501
    assert trade.eligible is True
    assert trade.payload()["sip_at_utc"].endswith("+00:00")
    assert trade.payload()["sip_at_et"].endswith("-04:00")


def test_raw_websocket_payload_is_decoded_without_losing_participant_time() -> None:
    rows = decode_raw_messages(
        b'[{"ev":"T","sym":"ABCD","t":1789555200123,"y":1789555100999,'
        b'"p":2.5,"s":40,"i":"wire-id","x":11,"trfi":501,"c":[1]}]'
    )

    assert len(rows) == 1
    trade = normalize_raw_trade(rows[0], conditions=_condition_snapshot())
    assert trade is not None
    assert trade.sip_ts_ms == 1_789_555_200_123
    assert trade.participant_ts_ms == 1_789_555_100_999
    assert trade.exchange == 11
    assert trade.trf_id == 501


def test_unknown_or_cancel_condition_is_excluded_before_the_engine() -> None:
    cancelled = normalize_raw_trade(
        {"ev": "T", "sym": "ABCD", "t": 1_789_555_200_123, "p": 4.0, "c": [99]},
        conditions=_condition_snapshot(),
    )
    unknown = normalize_raw_trade(
        {"ev": "T", "sym": "ABCD", "t": 1_789_555_200_124, "p": 4.0, "c": [404]},
        conditions=_condition_snapshot(),
    )

    assert cancelled is not None and cancelled.eligible is False
    assert cancelled.exclusion_reason == "not_consolidated_ohlc=99"
    assert unknown is not None and unknown.eligible is False
    assert unknown.exclusion_reason == "unknown_conditions=404"


@pytest.mark.parametrize(
    ("codes", "eligible", "reason"),
    [
        ((12,), True, "aggregate_eligible_extended_hours"),
        ((12, 14, 41), True, "aggregate_eligible_extended_hours"),
        ((12, 37), False, "not_consolidated_ohlc=37"),
        ((12, 14, 37, 41), False, "not_consolidated_ohlc=37"),
        ((13,), False, "not_consolidated_ohlc=13"),
        ((12, 404), False, "unknown_conditions=404"),
    ],
)
def test_real_extended_hours_condition_shapes_fail_closed_except_form_t(
    codes: tuple[int, ...], eligible: bool, reason: str
) -> None:
    trade = normalize_raw_trade(
        {"ev": "T", "sym": "DAIC", "t": 1_789_555_200_123, "p": 4.0, "c": codes},
        conditions=_condition_snapshot(),
    )

    assert trade is not None
    assert trade.eligible is eligible
    assert trade.exclusion_reason == reason


def test_service_disables_library_reconnects_so_feed_gaps_are_visible(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeWebSocketClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "massive",
        SimpleNamespace(WebSocketClient=FakeWebSocketClient),
    )
    service = MomentumPaperService(Settings(massive_api_key="fixture-key"))

    service._build_websocket_client()

    assert captured["max_reconnects"] == 0


def test_append_only_store_dedupes_and_names_incomplete_paths() -> None:
    store = _store()
    detected = MomentumTapeRecord(
        event_key="one:1:DETECTED",
        logical_id="one",
        strategy_code="momentum_30s",
        event_type="DETECTED",
        session_date=date(2026, 9, 16),
        symbol="ABCD",
        observed_at=datetime(2026, 9, 16, 8, 11, tzinfo=UTC),
        payload={"detect": {"sip_ts_ms": 1}},
    )

    assert store.append_many([detected, detected]) == 1
    rows = store.load_session(date(2026, 9, 16))
    assert len(rows) == 1
    assert store.incomplete_logical_ids(rows) == {"one"}


def test_session_snapshot_is_written_once_and_reused_without_rest_backfill() -> None:
    store = _store()
    snapshot = _condition_snapshot()
    ready = session_record(
        session_date=date(2026, 9, 16),
        observed_at=datetime(2026, 9, 16, 7, 55, tzinfo=UTC),
        prior_close_date=date(2026, 9, 15),
        prior_closes={"ABCD": "1.25"},
        condition_snapshot=snapshot.payload(),
    )

    assert store.append_many([ready]) == 1
    assert store.append_many([ready]) == 0
    restored = store.latest_session_ready(store.load_session(date(2026, 9, 16)))
    assert restored is not None
    assert restored.payload["prior_close_date"] == "2026-09-15"


@pytest.mark.asyncio
async def test_disabled_service_opens_no_feed_or_database() -> None:
    touched = False

    def forbidden():
        nonlocal touched
        touched = True
        raise AssertionError("disabled service touched an external dependency")

    service = MomentumPaperService(
        Settings(momentum_paper_enabled=False),
        rest_client_factory=forbidden,
        websocket_client_factory=forbidden,
    )

    await service.run()

    assert touched is False


@pytest.mark.asyncio
async def test_after_close_start_does_not_create_an_empty_paper_session() -> None:
    touched = False

    def forbidden():
        nonlocal touched
        touched = True
        raise AssertionError("after-close idle service fetched reference data")

    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        store=_store(),
        rest_client_factory=forbidden,
        clock=lambda: datetime(2026, 9, 16, 20, 0, tzinfo=UTC),
    )

    await service._tick()

    assert service._engine is None
    assert touched is False


@pytest.mark.asyncio
async def test_clean_unexpected_stream_end_opens_a_fail_closed_gap() -> None:
    class EndingWebsocket:
        closed = False

        async def connect(self, _handler) -> None:
            return

        async def close(self) -> None:
            self.closed = True

    now = datetime(2026, 9, 16, 8, 12, tzinfo=UTC)
    websocket = EndingWebsocket()
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        clock=lambda: now,
    )
    service._websocket = websocket
    service._connected = True

    await service._connect(websocket)

    assert service._connected is False
    assert service._feed_gap_started_ms == int(now.timestamp() * 1000)
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_five_consecutive_policy_closes_cool_off_for_fifteen_minutes_then_probe(
    caplog,
) -> None:
    class PolicyCloseWebsocket:
        async def connect(self, _handler) -> None:
            raise RuntimeError("received 1008 (policy violation)")

        async def close(self) -> None:
            return None

    class ProbeWebsocket:
        def __init__(self) -> None:
            self.release = asyncio.Event()
            self.subscriptions: list[str] = []

        def subscribe(self, *subscriptions: str) -> None:
            self.subscriptions.extend(subscriptions)

        async def connect(self, _handler) -> None:
            await self.release.wait()

        async def close(self) -> None:
            self.release.set()

    now = [datetime(2026, 9, 18, 10, 40, tzinfo=UTC)]
    probes: list[ProbeWebsocket] = []

    def factory() -> ProbeWebsocket:
        probe = ProbeWebsocket()
        probes.append(probe)
        return probe

    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        websocket_client_factory=factory,
        clock=lambda: now[0],
    )
    caplog.set_level("INFO")
    for _ in range(5):
        websocket = PolicyCloseWebsocket()
        service._websocket = websocket
        await service._connect(websocket)

    assert service._policy_violation_streak == 5
    assert service._policy_cooloff_until == now[0] + timedelta(minutes=15)
    assert sum("decision=cooloff" in message for message in caplog.messages) == 1

    await service._start_stream()
    assert probes == []

    now[0] += timedelta(minutes=15)
    await service._start_stream()
    assert len(probes) == 1
    assert probes[0].subscriptions == ["T.*"]
    await service._stop_stream()


@pytest.mark.asyncio
async def test_policy_cooloff_allows_one_probe_then_rearms_without_resetting_streak() -> None:
    class PolicyCloseWebsocket:
        def subscribe(self, *_subscriptions: str) -> None:
            return None

        async def connect(self, _handler) -> None:
            raise ConnectionClosedError(
                Close(code=1008, reason="policy violation"),
                Close(code=1008, reason="policy violation"),
                True,
            )

        async def close(self) -> None:
            return None

    now = datetime(2026, 9, 18, 11, 0, tzinfo=UTC)
    sockets: list[PolicyCloseWebsocket] = []

    def factory() -> PolicyCloseWebsocket:
        websocket = PolicyCloseWebsocket()
        sockets.append(websocket)
        return websocket

    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        websocket_client_factory=factory,
        clock=lambda: now,
    )
    service._policy_violation_streak = 5
    service._policy_cooloff_until = now

    await service._start_stream()
    assert service._websocket_task is not None
    await service._websocket_task
    assert len(sockets) == 1
    assert service._policy_violation_streak == 6
    assert service._policy_cooloff_until == now + timedelta(minutes=15)

    for _ in range(10):
        await service._start_stream()
    assert len(sockets) == 1


@pytest.mark.asyncio
async def test_policy_cooloff_heartbeat_is_degraded_with_the_explicit_reason() -> None:
    class RecordingRedis:
        def __init__(self) -> None:
            self.rows: list[tuple[str, dict[str, str]]] = []

        async def xadd(self, stream: str, fields: dict[str, str], **_kwargs) -> None:
            self.rows.append((stream, fields))

    redis = RecordingRedis()
    now = datetime(2026, 9, 18, 10, 40, tzinfo=UTC)
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        redis_client=redis,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    service._session_date = date(2026, 9, 18)
    service._engine = MomentumPaperEngine(
        prior_closes={"ABCD": Decimal("1")},
        condition_version="fixture",
        coverage_started_ms=_et_ms("04:00:00"),
    )
    service._connected = False
    for _ in range(5):
        service._record_policy_violation()

    await service._publish_state()

    heartbeat = json.loads(redis.rows[0][1]["data"])["payload"]
    assert heartbeat["status"] == "degraded"
    assert heartbeat["details"]["feed_reason"] == "feed_policy_violation"
    assert heartbeat["details"]["consecutive_policy_violations"] == "5"
    assert heartbeat["details"]["policy_cooloff_until"] == (now + timedelta(minutes=15)).isoformat()


@pytest.mark.asyncio
async def test_connected_feed_during_a_policy_streak_is_still_degraded() -> None:
    class RecordingRedis:
        def __init__(self) -> None:
            self.rows: list[tuple[str, dict[str, str]]] = []

        async def xadd(self, stream: str, fields: dict[str, str], **_kwargs) -> None:
            self.rows.append((stream, fields))

    redis = RecordingRedis()
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        redis_client=redis,  # type: ignore[arg-type]
    )
    service._session_date = date(2026, 9, 18)
    service._engine = MomentumPaperEngine(
        prior_closes={"ABCD": Decimal("1")},
        condition_version="fixture",
        coverage_started_ms=_et_ms("04:00:00"),
    )
    service._connected = True
    service._policy_violation_streak = 4

    await service._publish_state()

    heartbeat = json.loads(redis.rows[0][1]["data"])["payload"]
    assert heartbeat["status"] == "degraded"
    assert heartbeat["details"]["streamer_connected"] == "true"
    assert heartbeat["details"]["feed_reason"] == "feed_policy_violation"


def test_policy_state_clears_only_after_a_stable_minute() -> None:
    connected_at = datetime(2026, 9, 18, 10, 40, tzinfo=UTC)
    service = MomentumPaperService(Settings(momentum_paper_enabled=True))
    service._connected = True
    service._connected_since = connected_at
    service._policy_violation_streak = 5

    service._clear_policy_violation_after_stable_connection(connected_at + timedelta(seconds=59))
    assert service._policy_violation_streak == 5
    service._clear_policy_violation_after_stable_connection(connected_at + timedelta(seconds=60))
    assert service._policy_violation_streak == 0
    assert service._policy_cooloff_until is None


@pytest.mark.asyncio
async def test_gateway_feed_recovers_from_a_real_1008_close_without_opening_another_socket(
    tmp_path: Path,
) -> None:
    class PolicyCloseWebsocket:
        async def connect(self, _handler) -> None:
            raise ConnectionClosedError(
                Close(code=1008, reason="policy violation"),
                Close(code=1008, reason="policy violation"),
                True,
            )

        async def close(self) -> None:
            return None

    websocket_factory_calls = 0

    def forbidden_websocket_factory() -> object:
        nonlocal websocket_factory_calls
        websocket_factory_calls += 1
        raise AssertionError("gateway mode must not create a second Massive websocket")

    now = datetime(2026, 9, 21, 8, 5, tzinfo=UTC)
    del tmp_path
    socket_path = Path(f"/tmp/mt-{uuid4().hex}.sock")
    service = MomentumPaperService(
        Settings(
            momentum_paper_enabled=True,
            momentum_paper_gateway_feed_enabled=True,
            momentum_paper_gateway_socket_path=str(socket_path),
        ),
        store=_store(),
        websocket_client_factory=forbidden_websocket_factory,
        clock=lambda: now,
    )
    service._condition_snapshot = _condition_snapshot()
    service._engine = MomentumPaperEngine(
        prior_closes={"AEMD": Decimal("1")},
        condition_version="fixture",
        coverage_started_ms=0,
    )

    failed = PolicyCloseWebsocket()
    service._websocket = failed
    await service._connect(failed)
    assert service._policy_violation_streak == 1

    await service._start_stream()
    producer = connect_consumer_socket(str(socket_path))
    try:
        producer.send(
            encode_trade_frame(
                ParsedTradeFrame(
                    received_ns=1,
                    trades=(
                        {
                            "ev": "T",
                            "sym": "AEMD",
                            "p": 1.1,
                            "s": 10,
                            "t": int(now.timestamp() * 1000),
                            "c": [12],
                            "i": "trade-id",
                            "x": 11,
                            "trfi": 501,
                        },
                    ),
                )
            )
        )
        while not service._connected:
            await asyncio.sleep(0)
    finally:
        producer.close()
        await service._stop_stream()

    assert websocket_factory_calls == 0


def test_new_process_announces_stable_connection_to_clear_an_old_health_latch(
    caplog,
) -> None:
    connected_at = datetime(2026, 9, 18, 10, 40, tzinfo=UTC)
    service = MomentumPaperService(Settings(momentum_paper_enabled=True))
    service._connected = True
    service._connected_since = connected_at
    caplog.set_level("INFO")

    service._clear_policy_violation_after_stable_connection(connected_at + timedelta(seconds=60))
    service._clear_policy_violation_after_stable_connection(connected_at + timedelta(seconds=120))

    recovered = [
        message
        for message in caplog.messages
        if "decision=recovered reason=stable_connection" in message
    ]
    assert len(recovered) == 1
    assert "prior_consecutive_1008=0" in recovered[0]


@pytest.mark.asyncio
async def test_disconnected_heartbeat_reports_degraded_without_crashing() -> None:
    class RecordingRedis:
        def __init__(self) -> None:
            self.rows: list[tuple[str, dict[str, str]]] = []

        async def xadd(self, stream: str, fields: dict[str, str], **_kwargs) -> None:
            self.rows.append((stream, fields))

    redis = RecordingRedis()
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        redis_client=redis,  # type: ignore[arg-type]
    )
    service._session_date = date(2026, 9, 17)
    service._engine = MomentumPaperEngine(
        prior_closes={"ABCD": Decimal("1")},
        condition_version="fixture",
        coverage_started_ms=_et_ms("04:00:00"),
    )
    service._connected = False

    await service._publish_state()

    heartbeat = json.loads(redis.rows[0][1]["data"])
    assert heartbeat["payload"]["status"] == "degraded"
    assert len(redis.rows) == 3
    bot_state = json.loads(redis.rows[1][1]["data"])
    assert bot_state["payload"]["data_health"]["momentum_paper"]["tape_key_collisions"] == 0


@pytest.mark.asyncio
async def test_persisted_session_is_reused_without_a_rest_backfill() -> None:
    store = _store()
    ready = session_record(
        session_date=date(2026, 9, 16),
        observed_at=datetime(2026, 9, 16, 7, 55, tzinfo=UTC),
        prior_close_date=date(2026, 9, 15),
        prior_closes={"ABCD": "1.25"},
        condition_snapshot=_condition_snapshot().payload(),
    )
    store.append_many([ready])

    def forbidden():
        raise AssertionError("REST cannot replace a persisted forward-session snapshot")

    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        store=store,
        rest_client_factory=forbidden,
        clock=lambda: datetime(2026, 9, 16, 8, 0, tzinfo=UTC),
    )

    await service._prepare_session(date(2026, 9, 16))

    assert service._engine is not None
    assert service._condition_snapshot is not None


@pytest.mark.asyncio
async def test_restart_marks_an_interrupted_forward_path_unanswerable() -> None:
    store = _store()
    ready = session_record(
        session_date=date(2026, 9, 16),
        observed_at=datetime(2026, 9, 16, 7, 55, tzinfo=UTC),
        prior_close_date=date(2026, 9, 15),
        prior_closes={"ABCD": "1.25"},
        condition_snapshot=_condition_snapshot().payload(),
    )
    detected = MomentumTapeRecord(
        event_key="momentum_30s:ABCD:1:00000001:DETECTED",
        logical_id="momentum_30s:ABCD:1",
        strategy_code="momentum_30s",
        event_type="DETECTED",
        session_date=date(2026, 9, 16),
        symbol="ABCD",
        observed_at=datetime(2026, 9, 16, 8, 11, tzinfo=UTC),
        payload={"detect": {"sip_ts_ms": 1}, "symbol": "ABCD"},
    )
    store.append_many([ready, detected])
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        store=store,
        clock=lambda: datetime(2026, 9, 16, 8, 12, tzinfo=UTC),
    )

    await service._prepare_session(date(2026, 9, 16))

    rows = store.load_session(date(2026, 9, 16))
    recovered = [row for row in rows if row.event_type == "UNANSWERABLE"]
    assert len(recovered) == 1
    assert recovered[0].payload["reason"] == "service_restart_interrupted_forward_path"


@pytest.mark.asyncio
async def test_path_rows_batch_but_a_state_transition_forces_them_durable() -> None:
    class RecordingStore:
        def __init__(self) -> None:
            self.batches: list[list[MomentumTapeRecord]] = []

        def append_many(self, records) -> int:
            batch = list(records)
            self.batches.append(batch)
            return len(batch)

    store = RecordingStore()
    now = datetime(2026, 9, 16, 8, 12, tzinfo=UTC)
    service = MomentumPaperService(
        Settings(momentum_paper_enabled=True),
        store=store,  # type: ignore[arg-type]
        clock=lambda: now,
    )
    base = dict(
        logical_id="momentum_30s:ABCD:1",
        strategy_code="momentum_30s",
        session_date=date(2026, 9, 16),
        symbol="ABCD",
        observed_at=now,
        payload={},
    )
    path = MomentumTapeRecord(event_key="path", event_type="PATH_PRINT", **base)
    fill = MomentumTapeRecord(event_key="fill", event_type="FILLED", **base)

    await service._persist([path])
    assert store.batches == []

    await service._persist([fill])
    assert [[row.event_type for row in batch] for batch in store.batches] == [
        ["PATH_PRINT", "FILLED"]
    ]


def test_0930_tail_unsubscribes_global_and_keeps_only_active_symbols() -> None:
    class FakeWebsocket:
        def __init__(self) -> None:
            self.subscribed: list[str] = []
            self.unsubscribed: list[str] = []

        def subscribe(self, *channels: str) -> None:
            self.subscribed.extend(channels)

        def unsubscribe(self, *channels: str) -> None:
            self.unsubscribed.extend(channels)

    engine = MomentumPaperEngine(
        prior_closes={"ABCD": Decimal("1")},
        condition_version="fixture",
        coverage_started_ms=_et_ms("04:00:00"),
    )
    engine.ingest(TradePrint("ABCD", _et_ms("04:11:00"), Decimal("1"), 1))
    engine.ingest(TradePrint("ABCD", _et_ms("04:11:25"), Decimal("1.30"), 1))
    websocket = FakeWebsocket()
    service = MomentumPaperService(Settings(momentum_paper_enabled=True))
    service._engine = engine
    service._websocket = websocket

    service._enter_tail_mode()

    assert websocket.unsubscribed == ["T.*"]
    assert websocket.subscribed == ["T.ABCD"]


def test_previous_trading_day_skips_weekend_and_shared_holiday_calendar() -> None:
    assert previous_trading_day(date(2026, 9, 8)) == date(2026, 9, 4)


def test_detector_rate_is_uncalibrated_after_the_twenty_percent_rule_change() -> None:
    assert detector_health_status(0) == "UNCALIBRATED"
    assert detector_health_status(11) == "UNCALIBRATED"
    assert detector_health_status(10_000) == "UNCALIBRATED"


def test_momentum_registration_is_two_paper_cards_and_retires_polygon_only_when_enabled() -> None:
    active = configured_strategy_registrations(
        Settings(
            strategy_polygon_30s_enabled=True,
            momentum_paper_enabled=True,
            orb_enabled=True,
        )
    )
    by_code = {row.code: row for row in active}

    assert "polygon_30s" not in by_code
    assert {"momentum_30s", "momentum_60s", "orb"} <= set(by_code)
    assert by_code["momentum_30s"].display_name == "Momentum 30"
    assert by_code["momentum_60s"].display_name == "Momentum 60"
    assert by_code["momentum_30s"].execution_mode == "paper"
    assert by_code["momentum_60s"].metadata["isolated_service"] is True
    assert Settings().provider_for_strategy("momentum_30s") == "none"
    assert Settings().market_data_provider_for_strategy("momentum_60s") == "massive"
    assert by_code["orb"].display_name == "ORB Bot"
    assert not {
        row.name
        for row in configured_broker_account_registrations(Settings(momentum_paper_enabled=True))
    } & {"paper:momentum"}


def test_polygon_registration_remains_available_as_a_disabled_study_rollback() -> None:
    registrations = configured_strategy_registrations(
        Settings(strategy_polygon_30s_enabled=True, momentum_paper_enabled=False)
    )

    assert "polygon_30s" in {row.code for row in registrations}


def test_momentum_enablement_retires_the_polygon_runtime_not_only_its_card() -> None:
    settings = Settings(
        strategy_polygon_30s_enabled=True,
        momentum_paper_enabled=True,
    )
    source = (
        Path(__file__).parents[2] / "src/project_mai_tai/services/strategy_engine_app.py"
    ).read_text()

    assert polygon_30s_runtime_enabled(settings) is False
    assert "polygon_paper_enabled = polygon_30s_runtime_enabled(self.settings)" in source
    assert "if polygon_paper_enabled:" in source


def test_dashboard_routes_keep_momentum_cards_separate_and_paper_aware() -> None:
    assert BOT_PAGE_META["momentum_30s"]["title"] == "Momentum 30"
    assert BOT_PAGE_META["momentum_60s"]["title"] == "Momentum 60"
    assert _compact_bot_page_url("momentum_30s") == "/bot/momentum-30"
    assert _compact_bot_page_url("momentum_60s") == "/bot/momentum-60"
    assert "momentum_30s" in CONTROL_PLANE_ACTIVE_BOT_CODES
    assert "momentum_60s" in CONTROL_PLANE_ACTIVE_BOT_CODES


def test_service_import_graph_has_no_live_order_or_broker_route() -> None:
    source_path = Path(__file__).parents[2] / "src/project_mai_tai/services/momentum_paper_app.py"
    tree = ast.parse(source_path.read_text())
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {str(node.module) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert not any("broker_adapters" in value for value in imports)
    assert not any("oms" in value for value in imports)
    assert not any("trade_intent" in value for value in imports)


def test_install_requires_entitlement_proof_before_enabling_the_new_unit() -> None:
    script = (Path(__file__).parents[2] / "ops/systemd/install_momentum_paper.sh").read_text()

    runtime = script.index('pip" install -e')
    migration = script.index('alembic" upgrade head')
    proof = script.index("verify_momentum_paper_entitlement.sh")
    enable = script.index('systemctl enable --now "$UNIT"')
    assert runtime < migration < proof < enable
    assert "restart project-mai-tai" not in script


def test_entitlement_proof_requires_continued_gateway_health_not_only_same_pid() -> None:
    script = (
        Path(__file__).parents[2] / "ops/systemd/verify_momentum_paper_entitlement.sh"
    ).read_text()

    assert 'before_overview="$(gateway_overview)"' in script
    assert 'before_overview" == "$after_overview' in script
    assert "Massive websocket error|policy violation|reconnecting" in script
