from __future__ import annotations

import ast
import sys
from types import SimpleNamespace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from project_mai_tai.db.base import Base
from project_mai_tai.momentum_paper.conditions import build_condition_snapshot
from project_mai_tai.momentum_paper.engine import MomentumPaperEngine
from project_mai_tai.momentum_paper.models import MomentumTapeRecord, TradePrint
from project_mai_tai.momentum_paper.store import MomentumPaperStore, session_record
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
            "c": [1],
        },
        conditions=_condition_snapshot(),
    )

    assert trade is not None
    assert trade.sip_ts_ms == 1_789_555_200_123
    assert trade.participant_ts_ms == 1_789_555_100_999
    assert trade.trade_id == "wire-id"
    assert trade.eligible is True
    assert trade.payload()["sip_at_utc"].endswith("+00:00")
    assert trade.payload()["sip_at_et"].endswith("-04:00")


def test_raw_websocket_payload_is_decoded_without_losing_participant_time() -> None:
    rows = decode_raw_messages(
        b'[{"ev":"T","sym":"ABCD","t":1789555200123,"y":1789555100999,'
        b'"p":2.5,"s":40,"i":"wire-id","c":[1]}]'
    )

    assert len(rows) == 1
    trade = normalize_raw_trade(rows[0], conditions=_condition_snapshot())
    assert trade is not None
    assert trade.sip_ts_ms == 1_789_555_200_123
    assert trade.participant_ts_ms == 1_789_555_100_999


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


def test_detector_suspect_is_health_only_and_starts_above_ten() -> None:
    assert detector_health_status(10) == "HEALTHY"
    assert detector_health_status(11) == "DETECTOR_SUSPECT"


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
