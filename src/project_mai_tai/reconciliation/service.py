from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import logging
import socket
from typing import Any
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    ReconciliationFinding,
    ReconciliationRun,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.events import HeartbeatEvent, HeartbeatPayload, stream_name
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "reconciler"
ACTIVE_INCIDENT_STATUSES = {"open", "acknowledged"}
ACTIVE_ORDER_STATUSES = {"pending", "submitted", "accepted", "partially_filled"}
ACTIVE_INTENT_STATUSES = {"pending", "submitted", "accepted"}


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class FindingSpec:
    finding_type: str
    severity: str
    title: str
    fingerprint: str
    symbol: str | None
    payload: dict[str, Any]
    order_id: UUID | None = None


class ReconciliationService:
    def __init__(
        self,
        settings: Settings | None = None,
        redis_client: Redis | None = None,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ):
        self.settings = settings or get_settings()
        self.redis = redis_client or Redis.from_url(self.settings.redis_url, decode_responses=True)
        self.session_factory = session_factory or build_session_factory(self.settings)
        self.instance_name = socket.gethostname()
        self.logger = logging.getLogger(SERVICE_NAME)

    async def run(self) -> None:
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        interval = max(1, self.settings.reconciliation_interval_seconds)

        await self._publish_heartbeat("starting", {})
        while not stop_event.is_set():
            heartbeat_status = "healthy"
            heartbeat_details: dict[str, str]
            try:
                result = self.run_reconciliation_cycle()
                heartbeat_status = "degraded" if result["summary"]["total_findings"] > 0 else "healthy"
                heartbeat_details = {
                    "cutover_confidence": str(result["summary"]["cutover_confidence"]),
                    "total_findings": str(result["summary"]["total_findings"]),
                    "critical_findings": str(result["summary"]["critical_findings"]),
                    "run_status": result["status"],
                }
            except Exception as exc:
                self.logger.exception("reconciliation cycle failed")
                heartbeat_status = "degraded"
                heartbeat_details = {"error": type(exc).__name__}

            await self._publish_heartbeat(heartbeat_status, heartbeat_details)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except TimeoutError:
                continue

        await self._publish_heartbeat("stopping", {})
        await self.redis.aclose()

    def run_reconciliation_cycle(self) -> dict[str, Any]:
        with self.session_factory() as session:
            run = ReconciliationRun(status="running", summary={})
            session.add(run)
            session.flush()

            findings = self._collect_findings(session)
            for finding in findings:
                session.add(
                    ReconciliationFinding(
                        reconciliation_run_id=run.id,
                        order_id=finding.order_id,
                        severity=finding.severity,
                        finding_type=finding.finding_type,
                        symbol=finding.symbol,
                        payload={
                            "title": finding.title,
                            "fingerprint": finding.fingerprint,
                            **finding.payload,
                        },
                    )
                )

            summary = self._build_summary(session, findings)
            run.status = "completed"
            run.completed_at = utcnow()
            run.summary = summary

            self._sync_incidents(session, findings)
            session.commit()

            return {
                "run_id": str(run.id),
                "status": run.status,
                "summary": summary,
            }

    async def _publish_heartbeat(self, status: str, details: dict[str, str]) -> None:
        event = HeartbeatEvent(
            source_service=SERVICE_NAME,
            payload=HeartbeatPayload(
                service_name=SERVICE_NAME,
                instance_name=self.instance_name,
                status=status,
                details=details,
            ),
        )
        await self.redis.xadd(
            stream_name(self.settings.redis_stream_prefix, "heartbeats"),
            {"data": event.model_dump_json()},
            maxlen=self.settings.redis_heartbeat_stream_maxlen,
            approximate=True,
        )

    def _collect_findings(self, session: Session) -> list[FindingSpec]:
        findings: list[FindingSpec] = []
        findings.extend(self._build_position_findings(session))
        findings.extend(self._build_stuck_order_findings(session))
        findings.extend(self._build_stuck_intent_findings(session))
        return findings

    def _build_position_findings(self, session: Session) -> list[FindingSpec]:
        tolerance = Decimal(str(self.settings.reconciliation_position_quantity_tolerance))
        avg_price_tolerance = Decimal(str(self.settings.reconciliation_average_price_tolerance))
        account_lookup = {
            account.id: account
            for account in session.scalars(select(BrokerAccount)).all()
        }
        strategy_lookup = {
            strategy.id: strategy
            for strategy in session.scalars(select(Strategy)).all()
        }

        aggregates: dict[tuple[UUID, str], dict[str, Any]] = defaultdict(
            lambda: {
                "quantity": Decimal("0"),
                "cost": Decimal("0"),
                "strategy_codes": [],
            }
        )
        virtual_positions = session.scalars(
            select(VirtualPosition).where(VirtualPosition.quantity > 0)
        ).all()
        for position in virtual_positions:
            key = (position.broker_account_id, position.symbol)
            aggregate = aggregates[key]
            aggregate["quantity"] += position.quantity
            aggregate["cost"] += position.quantity * position.average_price
            strategy = strategy_lookup.get(position.strategy_id)
            if strategy is not None:
                aggregate["strategy_codes"].append(strategy.code)

        account_positions = {
            (position.broker_account_id, position.symbol): position
            for position in session.scalars(
                select(AccountPosition).where(AccountPosition.quantity > 0)
            ).all()
        }

        findings: list[FindingSpec] = []
        keys = sorted(set(aggregates) | set(account_positions), key=lambda item: (str(item[0]), item[1]))
        for account_id, symbol in keys:
            account = account_lookup.get(account_id)
            account_name = account.name if account is not None else str(account_id)
            aggregate = aggregates.get((account_id, symbol))
            account_position = account_positions.get((account_id, symbol))

            virtual_quantity = aggregate["quantity"] if aggregate else Decimal("0")
            account_quantity = account_position.quantity if account_position is not None else Decimal("0")
            quantity_delta = abs(account_quantity - virtual_quantity)
            if quantity_delta > tolerance:
                severity = "critical" if account_quantity == 0 or virtual_quantity == 0 else "warning"
                findings.append(
                    FindingSpec(
                        finding_type="position_quantity_mismatch",
                        severity=severity,
                        title=f"Position quantity mismatch for {symbol}",
                        fingerprint=f"position-quantity:{account_name}:{symbol}",
                        symbol=symbol,
                        payload={
                            "account_name": account_name,
                            "account_quantity": str(account_quantity),
                            "virtual_quantity": str(virtual_quantity),
                            "quantity_delta": str(quantity_delta),
                            "strategy_codes": sorted(set(aggregate["strategy_codes"])) if aggregate else [],
                        },
                    )
                )

            if aggregate and account_position and virtual_quantity > tolerance and account_quantity > tolerance:
                virtual_average_price = aggregate["cost"] / virtual_quantity if virtual_quantity else Decimal("0")
                price_delta = abs(account_position.average_price - virtual_average_price)
                if price_delta > avg_price_tolerance:
                    findings.append(
                        FindingSpec(
                            finding_type="average_price_mismatch",
                            severity="warning",
                            title=f"Average price mismatch for {symbol}",
                            fingerprint=f"average-price:{account_name}:{symbol}",
                            symbol=symbol,
                            payload={
                                "account_name": account_name,
                                "account_average_price": str(account_position.average_price),
                                "virtual_average_price": str(virtual_average_price.quantize(Decimal("0.00000001"))),
                                "price_delta": str(price_delta.quantize(Decimal("0.00000001"))),
                                "strategy_codes": sorted(set(aggregate["strategy_codes"])),
                            },
                        )
                    )

        return findings

    def _build_stuck_order_findings(self, session: Session) -> list[FindingSpec]:
        cutoff = utcnow() - timedelta(seconds=self.settings.reconciliation_stuck_order_seconds)
        findings: list[FindingSpec] = []
        account_lookup = {
            account.id: account
            for account in session.scalars(select(BrokerAccount)).all()
        }
        strategy_lookup = {
            strategy.id: strategy
            for strategy in session.scalars(select(Strategy)).all()
        }
        stale_orders = session.scalars(
            select(BrokerOrder)
            .where(BrokerOrder.status.in_(sorted(ACTIVE_ORDER_STATUSES)))
            .where(BrokerOrder.updated_at < cutoff)
            .order_by(desc(BrokerOrder.updated_at))
        ).all()

        for order in stale_orders:
            account = account_lookup.get(order.broker_account_id)
            strategy = strategy_lookup.get(order.strategy_id)
            account_name = account.name if account is not None else str(order.broker_account_id)
            strategy_code = strategy.code if strategy is not None else str(order.strategy_id)
            findings.append(
                FindingSpec(
                    finding_type="stuck_order",
                    severity="warning",
                    title=f"Order stuck in {order.status} for {order.symbol}",
                    fingerprint=f"stuck-order:{order.id}",
                    symbol=order.symbol,
                    order_id=order.id,
                    payload={
                        "account_name": account_name,
                        "strategy_code": strategy_code,
                        "client_order_id": order.client_order_id,
                        "broker_order_id": order.broker_order_id,
                        "status": order.status,
                        "updated_at": order.updated_at.isoformat(),
                    },
                )
            )

        return findings

    def _build_stuck_intent_findings(self, session: Session) -> list[FindingSpec]:
        cutoff = utcnow() - timedelta(seconds=self.settings.reconciliation_stuck_intent_seconds)
        findings: list[FindingSpec] = []
        account_lookup = {
            account.id: account
            for account in session.scalars(select(BrokerAccount)).all()
        }
        strategy_lookup = {
            strategy.id: strategy
            for strategy in session.scalars(select(Strategy)).all()
        }
        stale_intents = session.scalars(
            select(TradeIntent)
            .where(TradeIntent.status.in_(sorted(ACTIVE_INTENT_STATUSES)))
            .where(TradeIntent.updated_at < cutoff)
            .order_by(desc(TradeIntent.updated_at))
        ).all()

        for intent in stale_intents:
            account = account_lookup.get(intent.broker_account_id)
            strategy = strategy_lookup.get(intent.strategy_id)
            account_name = account.name if account is not None else str(intent.broker_account_id)
            strategy_code = strategy.code if strategy is not None else str(intent.strategy_id)
            findings.append(
                FindingSpec(
                    finding_type="stuck_intent",
                    severity="warning",
                    title=f"Intent stuck in {intent.status} for {intent.symbol}",
                    fingerprint=f"stuck-intent:{intent.id}",
                    symbol=intent.symbol,
                    payload={
                        "account_name": account_name,
                        "strategy_code": strategy_code,
                        "status": intent.status,
                        "intent_type": intent.intent_type,
                        "updated_at": intent.updated_at.isoformat(),
                    },
                )
            )

        return findings

    def _build_summary(self, session: Session, findings: list[FindingSpec]) -> dict[str, Any]:
        critical_findings = sum(1 for finding in findings if finding.severity == "critical")
        warning_findings = sum(1 for finding in findings if finding.severity == "warning")
        cutover_confidence = max(0, 100 - critical_findings * 35 - warning_findings * 10)
        accounts_checked = int(
            session.scalar(
                select(func.count())
                .select_from(BrokerAccount)
                .where(BrokerAccount.is_active.is_(True))
            )
            or 0
        )
        return {
            "checked_at": utcnow().isoformat(),
            "accounts_checked": accounts_checked,
            "total_findings": len(findings),
            "critical_findings": critical_findings,
            "warning_findings": warning_findings,
            "cutover_confidence": cutover_confidence,
        }

    def _sync_incidents(self, session: Session, findings: list[FindingSpec]) -> None:
        now = utcnow()
        active_fingerprints = {finding.fingerprint for finding in findings}
        open_incidents = session.scalars(
            select(SystemIncident).where(
                SystemIncident.service_name == SERVICE_NAME,
                SystemIncident.status.in_(sorted(ACTIVE_INCIDENT_STATUSES)),
            )
        ).all()
        incidents_by_fingerprint = {
            incident.payload.get("fingerprint"): incident
            for incident in open_incidents
            if incident.payload.get("fingerprint")
        }

        for finding in findings:
            incident = incidents_by_fingerprint.get(finding.fingerprint)
            payload = {
                "fingerprint": finding.fingerprint,
                "finding_type": finding.finding_type,
                **finding.payload,
            }
            if incident is None:
                session.add(
                    SystemIncident(
                        service_name=SERVICE_NAME,
                        severity=finding.severity,
                        title=finding.title,
                        status="open",
                        payload=payload,
                        opened_at=now,
                    )
                )
                continue

            incident.severity = finding.severity
            incident.title = finding.title
            incident.status = "open"
            incident.closed_at = None
            incident.payload = payload

        for fingerprint, incident in incidents_by_fingerprint.items():
            if fingerprint in active_fingerprints:
                continue
            incident.status = "closed"
            incident.closed_at = now
