from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from project_mai_tai.events import StrategyStateSnapshotEvent, StrategyStateSnapshotPayload
from project_mai_tai.services.tradingview_alerts_app import (
    TradingViewAlertService,
    TradingViewAlertStateStore,
    build_tradingview_alert_operator,
    build_alert_sync_plan,
    build_app,
)
from project_mai_tai.services.tradingview_playwright import (
    build_chart_url,
    describe_message_template,
    render_alert_message,
    render_alert_name,
)
from project_mai_tai.settings import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict[str, str]]] = []
        self.messages: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
        self.reverse_messages: list[tuple[str, dict[str, str]]] = []

    async def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        del kwargs
        self.xadd_calls.append((stream, dict(fields)))
        return "1-0"

    async def xread(self, offsets, **kwargs):
        del offsets, kwargs
        if self.messages:
            return [self.messages.pop(0)]
        await asyncio.sleep(0)
        return []

    async def xrevrange(self, stream: str, count: int = 1):
        del stream, count
        if self.reverse_messages:
            return [self.reverse_messages[0]]
        return []

    async def aclose(self) -> None:
        return None


class FakeOperator:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.removed: list[str] = []

    async def add_alert(self, symbol: str) -> None:
        self.added.append(symbol)

    async def remove_alert(self, symbol: str) -> None:
        self.removed.append(symbol)

    async def status(self) -> dict[str, object]:
        return {"operator": "fake", "ready": True, "auth_required": False, "auth_reason": None}

    async def close(self) -> None:
        return None


class AuthRequiredOperator(FakeOperator):
    async def add_alert(self, symbol: str) -> None:
        del symbol
        raise RuntimeError("TradingView automation profile is not signed in")

    async def status(self) -> dict[str, object]:
        return {
            "operator": "fake",
            "ready": False,
            "auth_required": True,
            "auth_reason": "TradingView automation profile is not signed in",
        }


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_relogin_required(self, *, reason: str, operator_status: dict[str, object]) -> None:
        self.messages.append({"reason": reason, "operator_status": dict(operator_status)})

    async def status(self) -> dict[str, object]:
        return {"provider": "fake", "enabled": True}


class RemoveFailsOperator(FakeOperator):
    async def remove_alert(self, symbol: str) -> None:
        self.removed.append(symbol)
        raise RuntimeError(f"TradingView alert still present after remove attempt: {symbol}")


def test_build_alert_sync_plan_calculates_add_remove_sets() -> None:
    plan = build_alert_sync_plan(
        desired_symbols=["UGRO", "SBET", "UGRO"],
        current_symbols=["CYCN", "SBET"],
    )

    assert plan.desired_symbols == ["SBET", "UGRO"]
    assert plan.symbols_to_add == ["UGRO"]
    assert plan.symbols_to_remove == ["CYCN"]
    assert plan.unchanged_symbols == ["SBET"]


def test_render_alert_helpers_expand_symbol_and_token() -> None:
    settings = Settings(
        tradingview_alerts_webhook_url="https://hook.project-mai-tai.live/webhook",
        tradingview_alerts_webhook_token="secret-token",
        tradingview_alerts_message_template_json='{"ticker":"{{SYMBOL}}","token":"{{WEBHOOK_TOKEN}}"}',
    )

    assert render_alert_name("ugro", prefix="MAI_TAI") == "MAI_TAI:UGRO"
    assert build_chart_url("https://www.tradingview.com/chart/abcd/", "UGRO").endswith("symbol=UGRO")
    assert render_alert_message(settings, "ugro") == '{"ticker":"UGRO","token":"secret-token"}'
    assert describe_message_template(settings)["valid_json"] is True


def test_build_operator_uses_log_only_by_default() -> None:
    operator = build_tradingview_alert_operator(Settings())

    assert operator.__class__.__name__ == "LoggingTradingViewAlertOperator"


def test_build_operator_uses_playwright_when_configured() -> None:
    operator = build_tradingview_alert_operator(Settings(tradingview_alerts_operator="playwright"))

    assert operator.__class__.__name__ == "PlaywrightTradingViewAlertOperator"


def test_sync_watchlist_updates_store_and_operator(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = FakeOperator()
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)

    plan = asyncio.run(service.sync_watchlist(["UGRO", "SBET"], source="unit-test"))

    assert plan.symbols_to_add == ["SBET", "UGRO"]
    assert operator.added == ["SBET", "UGRO"]
    assert operator.removed == []
    payload = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert payload["managed_symbols"] == ["SBET", "UGRO"]
    assert payload["desired_symbols"] == ["SBET", "UGRO"]
    assert payload["last_source"] == "unit-test"


def test_strategy_state_event_syncs_watchlist(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = FakeOperator()
    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(
            watchlist=["UGRO", "SBET"],
            top_confirmed=[{"ticker": "UGRO"}, {"ticker": "SBET"}],
        ),
    )
    redis.messages.append(
        (
            "mai_tai:strategy-state",
            [("1-0", {"data": event.model_dump_json()})],
        )
    )
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)

    async def run_once() -> None:
        await service.start()
        await asyncio.sleep(0.05)
        await service.stop()

    asyncio.run(run_once())

    assert operator.added == ["SBET", "UGRO"]
    assert service.state.managed_symbols == ["SBET", "UGRO"]
    assert service.state.last_strategy_event_id == str(event.event_id)


def test_strategy_state_protects_symbols_with_active_trade_state(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = FakeOperator()
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)

    asyncio.run(service.sync_watchlist(["UGRO"], source="seed"))
    assert service.state.managed_symbols == ["UGRO"]
    operator.added.clear()

    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(
            watchlist=[],
            bots=[
                {
                    "strategy_code": "macd_30s",
                    "account_name": "paper:macd_30s",
                    "positions": [{"ticker": "UGRO", "quantity": 10, "entry_price": 2.1}],
                    "pending_open_symbols": [],
                    "pending_close_symbols": [],
                    "pending_scale_levels": [],
                }
            ],
        ),
    )
    redis.messages.append(
        (
            "mai_tai:strategy-state",
            [("2-0", {"data": event.model_dump_json()})],
        )
    )

    async def run_once() -> None:
        await service.start()
        await asyncio.sleep(0.05)
        await service.stop()

    asyncio.run(run_once())

    assert operator.removed == []
    assert service.state.managed_symbols == ["UGRO"]
    assert service.state.requested_symbols == []
    assert service.state.protected_symbols == ["UGRO"]


def test_strategy_state_keeps_symbols_sticky_for_current_session(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = FakeOperator()
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)

    asyncio.run(service.sync_watchlist(["UGRO"], source="strategy-state:strategy-engine"))
    operator.added.clear()

    plan = asyncio.run(service.sync_watchlist([], source="strategy-state:strategy-engine"))

    assert plan.symbols_to_remove == []
    assert plan.unchanged_symbols == ["UGRO"]
    assert operator.removed == []
    assert service.state.managed_symbols == ["UGRO"]
    assert service.state.desired_symbols == ["UGRO"]
    assert service.state.requested_symbols == []


def test_prior_session_managed_symbols_can_be_removed_on_new_session(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = FakeOperator()
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)

    asyncio.run(service.sync_watchlist(["UGRO"], source="strategy-state:strategy-engine"))
    operator.added.clear()
    service.state.session_start_utc = "2000-01-01T00:00:00+00:00"

    plan = asyncio.run(service.sync_watchlist([], source="strategy-state:strategy-engine"))

    assert plan.symbols_to_remove == ["UGRO"]
    assert operator.removed == ["UGRO"]
    assert service.state.managed_symbols == []


def test_service_bootstraps_from_latest_strategy_state_snapshot(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = FakeOperator()
    event = StrategyStateSnapshotEvent(
        source_service="strategy-engine",
        payload=StrategyStateSnapshotPayload(
            watchlist=["PBM", "UGRO"],
            top_confirmed=[{"ticker": "PBM"}, {"ticker": "UGRO"}],
        ),
    )
    redis.reverse_messages.append(("9-0", {"data": event.model_dump_json()}))
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)

    async def run_once() -> None:
        await service.start()
        await asyncio.sleep(0.05)
        await service.stop()

    asyncio.run(run_once())

    assert operator.added == ["PBM", "UGRO"]
    assert service.state.managed_symbols == ["PBM", "UGRO"]
    assert service.state.requested_symbols == ["PBM", "UGRO"]
    assert service.state.protected_symbols == []
    assert service.state.last_source == "strategy-state-bootstrap:strategy-engine"
    assert service.state.last_strategy_event_id == str(event.event_id)


def test_alert_service_http_api_supports_add_and_remove(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = FakeOperator()
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)
    app = build_app(settings=settings, service=service)

    with TestClient(app) as client:
        add_response = client.post("/alerts/add", json={"symbol": "UGRO", "source": "api-test"})
        remove_response = client.post("/alerts/remove", json={"symbol": "UGRO", "source": "api-test"})
        status_response = client.get("/alerts/status")

    assert add_response.status_code == 200
    assert add_response.json()["symbols_to_add"] == ["UGRO"]
    assert remove_response.status_code == 200
    assert remove_response.json()["symbols_to_remove"] == ["UGRO"]
    assert status_response.status_code == 200
    assert status_response.json()["provider"] == settings.tradingview_alerts_operator
    assert operator.added == ["UGRO"]
    assert operator.removed == ["UGRO"]


def test_relogin_notification_fires_once_when_operator_requires_auth(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
        tradingview_alerts_notification_cooldown_minutes=240,
    )
    redis = FakeRedis()
    operator = AuthRequiredOperator()
    notifier = FakeNotifier()
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator, notifier=notifier)

    for _ in range(2):
        try:
            asyncio.run(service.sync_watchlist(["UGRO"], source="unit-test"))
        except RuntimeError:
            pass

    assert len(notifier.messages) == 1
    payload = json.loads((tmp_path / "alerts.json").read_text(encoding="utf-8"))
    assert payload["last_relogin_notification_at"]


def test_sync_watchlist_preserves_managed_symbol_when_remove_fails(tmp_path: Path) -> None:
    settings = Settings(
        tradingview_alerts_state_path=str(tmp_path / "alerts.json"),
        tradingview_alerts_enabled=True,
    )
    redis = FakeRedis()
    operator = RemoveFailsOperator()
    service = TradingViewAlertService(settings=settings, redis=redis, operator=operator)

    asyncio.run(service.sync_watchlist(["UGRO"], source="seed"))

    try:
        asyncio.run(service.sync_watchlist([], source="drop"))
    except RuntimeError as exc:
        assert "still present after remove attempt" in str(exc)
    else:
        raise AssertionError("expected remove failure")

    assert service.state.managed_symbols == ["UGRO"]
    assert service.state.desired_symbols == []
    assert service.state.requested_symbols == []
    assert service.state.last_error is not None


def test_state_store_loads_missing_file_as_empty(tmp_path: Path) -> None:
    store = TradingViewAlertStateStore(tmp_path / "missing.json")

    state = store.load()

    assert state.managed_symbols == []
    assert state.desired_symbols == []
