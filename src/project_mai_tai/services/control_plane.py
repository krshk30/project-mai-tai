from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from html import escape
import json
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from redis.asyncio import Redis
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session, sessionmaker
import uvicorn

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    Fill,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import (
    HeartbeatEvent,
    MarketDataSubscriptionEvent,
    SnapshotBatchEvent,
    stream_name,
)
from project_mai_tai.log import configure_logging
from project_mai_tai.settings import Settings, get_settings


SERVICE_NAME = "control-plane"


def utcnow() -> datetime:
    return datetime.now(UTC)


class ControlPlaneRepository:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: sessionmaker[Session],
        redis: Redis,
    ):
        self.settings = settings
        self.session_factory = session_factory
        self.redis = redis

    async def load_dashboard_data(self) -> dict[str, Any]:
        db_state = self._load_database_state()
        stream_state = await self._load_stream_state()

        overall_status = "healthy"
        if db_state["errors"] or stream_state["errors"]:
            overall_status = "degraded"
        elif any(service["status"] not in {"healthy", "starting"} for service in stream_state["services"]):
            overall_status = "degraded"

        return {
            "generated_at": utcnow().isoformat(),
            "status": overall_status,
            "environment": self.settings.environment,
            "domain": "project-mai-tai.live",
            "control_plane_url": self.settings.control_plane_base_url,
            "provider": self.settings.broker_default_provider,
            "oms_adapter": self.settings.oms_adapter,
            "streams": {
                "market_data": stream_name(self.settings.redis_stream_prefix, "market-data"),
                "snapshot_batches": stream_name(self.settings.redis_stream_prefix, "snapshot-batches"),
                "market_data_subscriptions": stream_name(
                    self.settings.redis_stream_prefix,
                    "market-data-subscriptions",
                ),
                "strategy_intents": stream_name(self.settings.redis_stream_prefix, "strategy-intents"),
                "order_events": stream_name(self.settings.redis_stream_prefix, "order-events"),
                "heartbeats": stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            },
            "counts": db_state["counts"],
            "services": stream_state["services"],
            "market_data": stream_state["market_data"],
            "recent_intents": db_state["recent_intents"],
            "recent_orders": db_state["recent_orders"],
            "recent_fills": db_state["recent_fills"],
            "virtual_positions": db_state["virtual_positions"],
            "account_positions": db_state["account_positions"],
            "incidents": db_state["incidents"],
            "errors": db_state["errors"] + stream_state["errors"],
        }

    async def load_health(self) -> dict[str, Any]:
        overview = await self.load_dashboard_data()
        return {
            "status": overview["status"],
            "service": SERVICE_NAME,
            "timestamp": overview["generated_at"],
            "environment": overview["environment"],
            "database_connected": not any(error.startswith("database:") for error in overview["errors"]),
            "redis_connected": not any(error.startswith("redis:") for error in overview["errors"]),
            "counts": overview["counts"],
            "services": overview["services"],
        }

    def _load_database_state(self) -> dict[str, Any]:
        errors: list[str] = []
        counts = {
            "strategies": 0,
            "broker_accounts": 0,
            "pending_intents": 0,
            "recent_fills": 0,
            "open_virtual_positions": 0,
            "open_account_positions": 0,
            "open_incidents": 0,
        }
        recent_intents: list[dict[str, Any]] = []
        recent_orders: list[dict[str, Any]] = []
        recent_fills: list[dict[str, Any]] = []
        virtual_positions: list[dict[str, Any]] = []
        account_positions: list[dict[str, Any]] = []
        incidents: list[dict[str, Any]] = []

        try:
            with self.session_factory() as session:
                session.execute(text("SELECT 1"))

                strategies = session.scalars(select(Strategy)).all()
                broker_accounts = session.scalars(select(BrokerAccount)).all()
                strategy_lookup = {strategy.id: strategy for strategy in strategies}
                account_lookup = {account.id: account for account in broker_accounts}

                counts["strategies"] = len(strategies)
                counts["broker_accounts"] = len(broker_accounts)
                counts["pending_intents"] = int(
                    session.scalar(
                        select(func.count()).select_from(TradeIntent).where(
                            TradeIntent.status.in_(["pending", "submitted", "accepted"])
                        )
                    )
                    or 0
                )
                counts["recent_fills"] = int(session.scalar(select(func.count()).select_from(Fill)) or 0)
                counts["open_virtual_positions"] = int(
                    session.scalar(
                        select(func.count()).select_from(VirtualPosition).where(VirtualPosition.quantity > 0)
                    )
                    or 0
                )
                counts["open_account_positions"] = int(
                    session.scalar(
                        select(func.count()).select_from(AccountPosition).where(AccountPosition.quantity > 0)
                    )
                    or 0
                )
                counts["open_incidents"] = int(
                    session.scalar(
                        select(func.count()).select_from(SystemIncident).where(SystemIncident.status != "closed")
                    )
                    or 0
                )

                for intent in session.scalars(
                    select(TradeIntent).order_by(desc(TradeIntent.updated_at)).limit(10)
                ).all():
                    strategy = strategy_lookup.get(intent.strategy_id)
                    account = account_lookup.get(intent.broker_account_id)
                    recent_intents.append(
                        {
                            "strategy_code": strategy.code if strategy else str(intent.strategy_id),
                            "broker_account_name": account.name if account else str(intent.broker_account_id),
                            "symbol": intent.symbol,
                            "side": intent.side,
                            "intent_type": intent.intent_type,
                            "quantity": _decimal_str(intent.quantity),
                            "status": intent.status,
                            "reason": intent.reason,
                            "updated_at": _datetime_str(intent.updated_at),
                        }
                    )

                for order in session.scalars(
                    select(BrokerOrder).order_by(desc(BrokerOrder.updated_at)).limit(10)
                ).all():
                    strategy = strategy_lookup.get(order.strategy_id)
                    account = account_lookup.get(order.broker_account_id)
                    recent_orders.append(
                        {
                            "strategy_code": strategy.code if strategy else str(order.strategy_id),
                            "broker_account_name": account.name if account else str(order.broker_account_id),
                            "symbol": order.symbol,
                            "side": order.side,
                            "quantity": _decimal_str(order.quantity),
                            "status": order.status,
                            "client_order_id": order.client_order_id,
                            "broker_order_id": order.broker_order_id or "",
                            "updated_at": _datetime_str(order.updated_at),
                        }
                    )

                for fill in session.scalars(select(Fill).order_by(desc(Fill.filled_at)).limit(10)).all():
                    strategy = strategy_lookup.get(fill.strategy_id)
                    account = account_lookup.get(fill.broker_account_id)
                    recent_fills.append(
                        {
                            "strategy_code": strategy.code if strategy else str(fill.strategy_id),
                            "broker_account_name": account.name if account else str(fill.broker_account_id),
                            "symbol": fill.symbol,
                            "side": fill.side,
                            "quantity": _decimal_str(fill.quantity),
                            "price": _decimal_str(fill.price),
                            "filled_at": _datetime_str(fill.filled_at),
                        }
                    )

                for position in session.scalars(
                    select(VirtualPosition)
                    .where(VirtualPosition.quantity > 0)
                    .order_by(desc(VirtualPosition.updated_at))
                    .limit(20)
                ).all():
                    strategy = strategy_lookup.get(position.strategy_id)
                    account = account_lookup.get(position.broker_account_id)
                    virtual_positions.append(
                        {
                            "strategy_code": strategy.code if strategy else str(position.strategy_id),
                            "broker_account_name": account.name if account else str(position.broker_account_id),
                            "symbol": position.symbol,
                            "quantity": _decimal_str(position.quantity),
                            "average_price": _decimal_str(position.average_price),
                            "realized_pnl": _decimal_str(position.realized_pnl),
                            "updated_at": _datetime_str(position.updated_at),
                        }
                    )

                for position in session.scalars(
                    select(AccountPosition)
                    .where(AccountPosition.quantity > 0)
                    .order_by(desc(AccountPosition.updated_at))
                    .limit(20)
                ).all():
                    account = account_lookup.get(position.broker_account_id)
                    account_positions.append(
                        {
                            "broker_account_name": account.name if account else str(position.broker_account_id),
                            "symbol": position.symbol,
                            "quantity": _decimal_str(position.quantity),
                            "average_price": _decimal_str(position.average_price),
                            "market_value": _decimal_str(position.market_value),
                            "updated_at": _datetime_str(position.updated_at),
                        }
                    )

                for incident in session.scalars(
                    select(SystemIncident).order_by(desc(SystemIncident.opened_at)).limit(10)
                ).all():
                    incidents.append(
                        {
                            "service_name": incident.service_name or "system",
                            "severity": incident.severity,
                            "title": incident.title,
                            "status": incident.status,
                            "opened_at": _datetime_str(incident.opened_at),
                        }
                    )
        except Exception as exc:
            errors.append(f"database:{exc}")

        return {
            "counts": counts,
            "recent_intents": recent_intents,
            "recent_orders": recent_orders,
            "recent_fills": recent_fills,
            "virtual_positions": virtual_positions,
            "account_positions": account_positions,
            "incidents": incidents,
            "errors": errors,
        }

    async def _load_stream_state(self) -> dict[str, Any]:
        errors: list[str] = []
        services: list[dict[str, Any]] = []
        market_data = {
            "latest_snapshot_batch": None,
            "active_subscription_symbols": 0,
            "subscription_symbols": [],
        }

        try:
            heartbeats = await self._read_stream_events("heartbeats", limit=50)
            latest_by_service: dict[str, dict[str, Any]] = {}
            for event in heartbeats:
                payload = HeartbeatEvent.model_validate(event).payload
                if payload.service_name in latest_by_service:
                    continue
                latest_by_service[payload.service_name] = {
                    "service_name": payload.service_name,
                    "instance_name": payload.instance_name,
                    "status": payload.status,
                    "details": payload.details,
                    "observed_at": _datetime_str(HeartbeatEvent.model_validate(event).produced_at),
                }
            services = sorted(latest_by_service.values(), key=lambda item: item["service_name"])
        except Exception as exc:
            errors.append(f"redis:heartbeats:{exc}")

        try:
            snapshot_events = await self._read_stream_events("snapshot-batches", limit=1)
            if snapshot_events:
                event = SnapshotBatchEvent.model_validate(snapshot_events[0])
                market_data["latest_snapshot_batch"] = {
                    "snapshot_count": len(event.payload.snapshots),
                    "reference_count": len(event.payload.reference_data),
                    "completed_at": _datetime_str(event.payload.completed_at),
                }
        except Exception as exc:
            errors.append(f"redis:snapshot-batches:{exc}")

        try:
            subscription_events = await self._read_stream_events("market-data-subscriptions", limit=1)
            if subscription_events:
                event = MarketDataSubscriptionEvent.model_validate(subscription_events[0])
                market_data["active_subscription_symbols"] = len(event.payload.symbols)
                market_data["subscription_symbols"] = event.payload.symbols
        except Exception as exc:
            errors.append(f"redis:market-data-subscriptions:{exc}")

        return {
            "services": services,
            "market_data": market_data,
            "errors": errors,
        }

    async def _read_stream_events(self, topic: str, *, limit: int) -> list[dict[str, Any]]:
        stream = stream_name(self.settings.redis_stream_prefix, topic)
        entries = await self.redis.xrevrange(stream, count=limit)
        payloads: list[dict[str, Any]] = []
        for _message_id, fields in entries:
            data = fields.get("data")
            if data:
                payloads.append(json.loads(data))
        return payloads


def build_app(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    redis_client: Redis | None = None,
) -> FastAPI:
    active_settings = settings or get_settings()
    active_session_factory = session_factory or build_session_factory(active_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        redis = redis_client or Redis.from_url(active_settings.redis_url, decode_responses=True)
        app.state.repository = ControlPlaneRepository(
            active_settings,
            session_factory=active_session_factory,
            redis=redis,
        )
        yield
        if redis_client is None:
            await redis.aclose()

    app = FastAPI(
        title="Project Mai Tai Control Plane",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return await app.state.repository.load_health()

    @app.get("/meta")
    async def meta() -> dict[str, Any]:
        return {
            "app_name": active_settings.app_name,
            "domain": "project-mai-tai.live",
            "legacy_api_base_url": active_settings.legacy_api_base_url,
            "oms_adapter": active_settings.oms_adapter,
            "streams": {
                "market_data": stream_name(active_settings.redis_stream_prefix, "market-data"),
                "snapshot_batches": stream_name(active_settings.redis_stream_prefix, "snapshot-batches"),
                "market_data_subscriptions": stream_name(
                    active_settings.redis_stream_prefix,
                    "market-data-subscriptions",
                ),
                "strategy_intents": stream_name(active_settings.redis_stream_prefix, "strategy-intents"),
                "order_events": stream_name(active_settings.redis_stream_prefix, "order-events"),
                "heartbeats": stream_name(active_settings.redis_stream_prefix, "heartbeats"),
            },
        }

    @app.get("/api/overview")
    async def overview() -> dict[str, Any]:
        return await app.state.repository.load_dashboard_data()

    @app.get("/api/orders")
    async def orders() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "recent_intents": data["recent_intents"],
            "recent_orders": data["recent_orders"],
            "recent_fills": data["recent_fills"],
        }

    @app.get("/api/positions")
    async def positions() -> dict[str, Any]:
        data = await app.state.repository.load_dashboard_data()
        return {
            "virtual_positions": data["virtual_positions"],
            "account_positions": data["account_positions"],
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        data = await app.state.repository.load_dashboard_data()
        return _render_dashboard(data)

    return app


app = build_app()


def run() -> None:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    uvicorn.run(
        "project_mai_tai.services.control_plane:app",
        host=settings.control_plane_host,
        port=settings.control_plane_port,
        reload=False,
    )


def _render_dashboard(data: dict[str, Any]) -> str:
    refresh_seconds = 5
    errors_html = "".join(
        f'<div class="alert">{escape(error)}</div>' for error in data["errors"]
    ) or '<div class="ok-banner">No current control-plane read errors.</div>'

    services_rows = "".join(
        f"""
        <tr>
          <td>{escape(service["service_name"])}</td>
          <td>{_status_badge(service["status"])}</td>
          <td>{escape(service["instance_name"])}</td>
          <td>{escape(service["observed_at"])}</td>
          <td>{escape(", ".join(f"{key}={value}" for key, value in service["details"].items()) or "-")}</td>
        </tr>
        """
        for service in data["services"]
    ) or _empty_row(5, "No service heartbeats yet")

    intents_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["intent_type"])}</td>
          <td>{escape(item["side"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{_status_badge(item["status"])}</td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["recent_intents"]
    ) or _empty_row(7, "No trade intents recorded yet")

    orders_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["side"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{_status_badge(item["status"])}</td>
          <td><code>{escape(item["client_order_id"])}</code></td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["recent_orders"]
    ) or _empty_row(7, "No broker orders recorded yet")

    fills_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["side"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{escape(item["price"])}</td>
          <td>{escape(item["filled_at"])}</td>
        </tr>
        """
        for item in data["recent_fills"]
    ) or _empty_row(6, "No fills recorded yet")

    virtual_positions_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["strategy_code"])}</td>
          <td>{escape(item["broker_account_name"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{escape(item["average_price"])}</td>
          <td>{escape(item["realized_pnl"])}</td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["virtual_positions"]
    ) or _empty_row(7, "No virtual positions open")

    account_positions_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["broker_account_name"])}</td>
          <td>{escape(item["symbol"])}</td>
          <td>{escape(item["quantity"])}</td>
          <td>{escape(item["average_price"])}</td>
          <td>{escape(item["market_value"] or "-")}</td>
          <td>{escape(item["updated_at"])}</td>
        </tr>
        """
        for item in data["account_positions"]
    ) or _empty_row(6, "No account positions open")

    incidents_rows = "".join(
        f"""
        <tr>
          <td>{escape(item["service_name"])}</td>
          <td>{_status_badge(item["severity"])}</td>
          <td>{escape(item["title"])}</td>
          <td>{_status_badge(item["status"])}</td>
          <td>{escape(item["opened_at"])}</td>
        </tr>
        """
        for item in data["incidents"]
    ) or _empty_row(5, "No incidents logged")

    latest_snapshot = data["market_data"]["latest_snapshot_batch"] or {}
    snapshot_summary = (
        f'{latest_snapshot.get("snapshot_count", 0)} snapshots / '
        f'{latest_snapshot.get("reference_count", 0)} refs'
        if latest_snapshot
        else "No snapshot batches yet"
    )
    subscription_symbols = data["market_data"]["subscription_symbols"][:12]
    subscription_summary = ", ".join(subscription_symbols) or "No dynamic subscriptions yet"

    return f"""
    <html>
      <head>
        <title>Project Mai Tai Control Plane</title>
        <meta http-equiv="refresh" content="{refresh_seconds}">
        <style>
          :root {{
            --ink: #122433;
            --muted: #61758a;
            --line: rgba(18, 36, 51, 0.12);
            --panel: rgba(255, 255, 255, 0.9);
            --bg-top: #f6efe1;
            --bg-bottom: #edf7fb;
            --accent: #0f7f66;
            --warn: #d48000;
            --danger: #c0392b;
          }}
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            color: var(--ink);
            font-family: Georgia, "Times New Roman", serif;
            background:
              radial-gradient(circle at top right, rgba(15, 127, 102, 0.12), transparent 28%),
              linear-gradient(180deg, var(--bg-top), var(--bg-bottom));
          }}
          .shell {{
            max-width: 1280px;
            margin: 0 auto;
            padding: 24px;
          }}
          .hero, .section, .table-card {{
            background: var(--panel);
            border: 1px solid var(--line);
            border-radius: 22px;
            box-shadow: 0 18px 42px rgba(18, 36, 51, 0.08);
          }}
          .hero {{
            padding: 28px;
            margin-bottom: 20px;
          }}
          .eyebrow {{
            color: var(--accent);
            font-size: 13px;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            margin-bottom: 10px;
          }}
          h1, h2 {{
            margin: 0 0 10px 0;
          }}
          p {{
            margin: 0;
            color: var(--muted);
            line-height: 1.45;
          }}
          .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 14px;
            margin-top: 22px;
          }}
          .card {{
            background: rgba(255, 255, 255, 0.8);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 16px;
          }}
          .label {{
            font-size: 12px;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: var(--muted);
            margin-bottom: 6px;
          }}
          .value {{
            font-size: 28px;
            font-weight: bold;
          }}
          .grid-2 {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 16px;
            margin-top: 16px;
          }}
          .section {{
            padding: 20px;
          }}
          .section-header {{
            display: flex;
            justify-content: space-between;
            align-items: baseline;
            gap: 12px;
            margin-bottom: 12px;
          }}
          .sub {{
            color: var(--muted);
            font-size: 14px;
          }}
          .table-card {{
            overflow: hidden;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
          }}
          th, td {{
            padding: 12px 14px;
            border-bottom: 1px solid var(--line);
            text-align: left;
            vertical-align: top;
          }}
          th {{
            background: rgba(18, 36, 51, 0.04);
            color: var(--muted);
            font-size: 12px;
            letter-spacing: 0.06em;
            text-transform: uppercase;
          }}
          tr:last-child td {{
            border-bottom: none;
          }}
          .pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 4px 10px;
            border-radius: 999px;
            font-size: 12px;
            font-weight: bold;
          }}
          .status-healthy, .status-filled, .status-pass, .status-open {{
            background: rgba(15, 127, 102, 0.12);
            color: var(--accent);
          }}
          .status-starting, .status-accepted, .status-submitted, .status-warning {{
            background: rgba(212, 128, 0, 0.12);
            color: var(--warn);
          }}
          .status-rejected, .status-degraded, .status-error, .status-closed, .status-critical {{
            background: rgba(192, 57, 43, 0.12);
            color: var(--danger);
          }}
          .status-pending, .status-cancelled {{
            background: rgba(18, 36, 51, 0.1);
            color: var(--ink);
          }}
          .muted-box {{
            color: var(--muted);
            font-size: 14px;
            line-height: 1.5;
          }}
          .alert {{
            padding: 10px 12px;
            border-left: 4px solid var(--danger);
            background: rgba(192, 57, 43, 0.08);
            margin-bottom: 8px;
            border-radius: 10px;
          }}
          .ok-banner {{
            padding: 10px 12px;
            border-left: 4px solid var(--accent);
            background: rgba(15, 127, 102, 0.08);
            border-radius: 10px;
          }}
          code {{
            background: rgba(18, 36, 51, 0.08);
            padding: 2px 6px;
            border-radius: 6px;
          }}
        </style>
      </head>
      <body>
        <div class="shell">
          <section class="hero">
            <div class="eyebrow">Project Mai Tai Operator View</div>
            <h1>Parallel Live-Trading Rebuild</h1>
            <p>
              This control plane is reading the new platform's durable OMS state and live stream
              health so you can validate it beside the legacy system before cutover.
            </p>
            <div class="cards">
              <div class="card">
                <div class="label">Platform Status</div>
                <div class="value">{data["status"].upper()}</div>
                <p>{escape(data["environment"])} / {escape(data["provider"])} / {escape(data["oms_adapter"])}</p>
              </div>
              <div class="card">
                <div class="label">Open Virtual Positions</div>
                <div class="value">{data["counts"]["open_virtual_positions"]}</div>
                <p>Strategy-attributed positions inside shared accounts.</p>
              </div>
              <div class="card">
                <div class="label">Pending Intents</div>
                <div class="value">{data["counts"]["pending_intents"]}</div>
                <p>Open, submitted, or accepted intents waiting on broker lifecycle.</p>
              </div>
              <div class="card">
                <div class="label">Latest Snapshot</div>
                <div class="value">{escape(snapshot_summary)}</div>
                <p>{escape(latest_snapshot.get("completed_at", "No snapshot timestamp yet"))}</p>
              </div>
              <div class="card">
                <div class="label">Active Market Symbols</div>
                <div class="value">{data["market_data"]["active_subscription_symbols"]}</div>
                <p>{escape(subscription_summary)}</p>
              </div>
              <div class="card">
                <div class="label">Control Plane</div>
                <div class="value"><code>{escape(data["control_plane_url"])}</code></div>
                <p>{escape(data["generated_at"])}</p>
              </div>
            </div>
          </section>

          <div class="grid-2">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Service Health</h2>
                  <div class="sub">Latest heartbeat per service from Redis streams.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Service</th><th>Status</th><th>Instance</th><th>Observed</th><th>Details</th></tr>
                  </thead>
                  <tbody>{services_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Control Plane Notes</h2>
                  <div class="sub">Fast checks and current read-model diagnostics.</div>
                </div>
              </div>
              <div class="muted-box">
                <p><strong>Domain:</strong> {escape(data["domain"])}</p>
                <p><strong>Redis Prefix:</strong> <code>{escape(data["streams"]["heartbeats"].split(":")[0])}</code></p>
                <p><strong>Broker Accounts:</strong> {data["counts"]["broker_accounts"]}</p>
                <p><strong>Strategies:</strong> {data["counts"]["strategies"]}</p>
                <p><strong>Open Incidents:</strong> {data["counts"]["open_incidents"]}</p>
                <p><strong>Refresh:</strong> Every {refresh_seconds}s</p>
              </div>
              <div style="margin-top: 16px;">{errors_html}</div>
            </section>
          </div>

          <div class="grid-2">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Recent Intents</h2>
                  <div class="sub">Latest strategy decisions accepted by the event bus.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Symbol</th><th>Type</th><th>Side</th><th>Qty</th><th>Status</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{intents_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Recent Orders</h2>
                  <div class="sub">Durable OMS order state keyed by client order id.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Client Id</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{orders_rows}</tbody>
                </table>
              </div>
            </section>
          </div>

          <div class="grid-2">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Recent Fills</h2>
                  <div class="sub">Execution reports persisted by the OMS layer.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Symbol</th><th>Side</th><th>Qty</th><th>Price</th><th>Filled</th></tr>
                  </thead>
                  <tbody>{fills_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Incidents</h2>
                  <div class="sub">Any control-plane or runtime issues that have been logged.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Service</th><th>Severity</th><th>Title</th><th>Status</th><th>Opened</th></tr>
                  </thead>
                  <tbody>{incidents_rows}</tbody>
                </table>
              </div>
            </section>
          </div>

          <div class="grid-2">
            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Virtual Positions</h2>
                  <div class="sub">Strategy-attributed holdings inside each broker account.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Strategy</th><th>Account</th><th>Symbol</th><th>Qty</th><th>Avg Px</th><th>Realized PnL</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{virtual_positions_rows}</tbody>
                </table>
              </div>
            </section>

            <section class="section">
              <div class="section-header">
                <div>
                  <h2>Account Positions</h2>
                  <div class="sub">Broker-account level holdings for reconciliation and operator checks.</div>
                </div>
              </div>
              <div class="table-card">
                <table>
                  <thead>
                    <tr><th>Account</th><th>Symbol</th><th>Qty</th><th>Avg Px</th><th>Market Value</th><th>Updated</th></tr>
                  </thead>
                  <tbody>{account_positions_rows}</tbody>
                </table>
              </div>
            </section>
          </div>
        </div>
      </body>
    </html>
    """


def _status_badge(status: str) -> str:
    normalized = status.lower().replace(" ", "_")
    return f'<span class="pill status-{escape(normalized)}">{escape(status.upper())}</span>'


def _empty_row(columns: int, message: str) -> str:
    return f'<tr><td colspan="{columns}" style="color:#61758a;">{escape(message)}</td></tr>'


def _decimal_str(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value.normalize() if value != 0 else Decimal("0"), "f")


def _datetime_str(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
