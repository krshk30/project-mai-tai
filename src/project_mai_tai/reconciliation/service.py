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
from redis.exceptions import (
    BusyLoadingError,
    ConnectionError as RedisConnectionError,
    RedisError,
    TimeoutError as RedisTimeoutError,
)
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.models import (
    AccountPosition,
    BrokerAccount,
    BrokerOrder,
    OmsManagedPosition,
    ReconciliationFinding,
    ReconciliationRun,
    Strategy,
    SystemIncident,
    TradeIntent,
    VirtualPosition,
)
from project_mai_tai.db.session import build_timed_session_factory
from project_mai_tai.events import HeartbeatEvent, HeartbeatPayload, stream_name
from project_mai_tai.services.runtime import _install_signal_handlers
from project_mai_tai.settings import Settings, get_settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "reconciler"
ACTIVE_INCIDENT_STATUSES = {"open", "acknowledged"}
ACTIVE_ORDER_STATUSES = {"pending", "submitted", "accepted", "partially_filled"}
ACTIVE_INTENT_STATUSES = {"pending", "submitted", "accepted"}
# The 2026-08-27 live Redis restart loaded its RDB in 2.375s. These bounded
# delays provide 7.5s of reload margin (five total attempts) without turning a
# persistent Redis failure into an indefinitely healthy-looking heartbeat loop.
HEARTBEAT_RELOAD_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _is_transient_redis_reload_error(exc: RedisError) -> bool:
    """Return true only for errors Redis emits while restarting/reloading.

    ``AuthenticationError`` subclasses redis-py's ``ConnectionError``, so an
    ``isinstance(exc, RedisConnectionError)`` check would wrongly retry bad
    credentials. Exact ``ConnectionError`` covers a dropped/refused socket;
    ``BusyLoadingError`` and ``TimeoutError`` cover the bounded reload window.
    """

    return (
        isinstance(exc, (BusyLoadingError, RedisTimeoutError))
        or type(exc) is RedisConnectionError
    )


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
        self.session_factory = session_factory or build_timed_session_factory(self.settings, service="reconciler", profile="slow")
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
        max_attempts = len(HEARTBEAT_RELOAD_RETRY_DELAYS_SECONDS) + 1
        transient_failures = 0
        for attempt in range(1, max_attempts + 1):
            try:
                await self.redis.xadd(
                    stream_name(self.settings.redis_stream_prefix, "heartbeats"),
                    {"data": event.model_dump_json()},
                    maxlen=self.settings.redis_heartbeat_stream_maxlen,
                    approximate=True,
                )
            except RedisError as exc:
                if not _is_transient_redis_reload_error(exc):
                    self.logger.error(
                        "[RECONCILER-HEARTBEAT-FAILED] attempt=%s/%s "
                        "outcome=non_transient_propagated error=%s",
                        attempt,
                        max_attempts,
                        type(exc).__name__,
                    )
                    raise

                transient_failures += 1
                if attempt == max_attempts:
                    self.logger.error(
                        "[RECONCILER-HEARTBEAT-FAILED] attempt=%s/%s "
                        "transient_failures=%s outcome=transient_exhausted error=%s",
                        attempt,
                        max_attempts,
                        transient_failures,
                        type(exc).__name__,
                    )
                    raise

                delay_seconds = HEARTBEAT_RELOAD_RETRY_DELAYS_SECONDS[attempt - 1]
                self.logger.warning(
                    "[RECONCILER-HEARTBEAT-RETRY] attempt=%s/%s "
                    "transient_failures=%s outcome=retrying delay_seconds=%.1f error=%s",
                    attempt,
                    max_attempts,
                    transient_failures,
                    delay_seconds,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay_seconds)
                continue

            if transient_failures:
                self.logger.info(
                    "[RECONCILER-HEARTBEAT-RECOVERED] attempt=%s/%s "
                    "transient_failures=%s outcome=published",
                    attempt,
                    max_attempts,
                    transient_failures,
                )
            return

    def _collect_findings(self, session: Session) -> list[FindingSpec]:
        findings: list[FindingSpec] = []
        findings.extend(self._build_position_findings(session))
        findings.extend(self._build_stuck_order_findings(session))
        findings.extend(self._build_stuck_intent_findings(session))
        return findings

    def _build_position_findings(self, session: Session) -> list[FindingSpec]:
        tolerance = Decimal(str(self.settings.reconciliation_position_quantity_tolerance))
        avg_price_tolerance = Decimal(str(self.settings.reconciliation_average_price_tolerance))
        ignored_pairs = self.settings.reconciliation_ignored_position_mismatch_pairs
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

        # ⛔⭐⭐ `virtual_positions` FALSELY READS ZERO ON A POSITION WE REALLY HOLD, and comparing
        # ONLY against it manufactures a CRITICAL drift for a position that is tracked correctly.
        # Live instance 2026-08-14 (WETO, live:orb): the OMS filled 1 @ 8.005 at 16:19:14Z, and
        # `[VIRTUAL-CLEAR]` zeroed the virtual row 0.7s later — inside the ~15s Webull settle window,
        # while `[SETTLE-PENDING] shape=FLAT_INFERRED` still said "our fill is not visible yet". The
        # broker became visible at 16:19:30Z and NOTHING restored the row. Meanwhile
        # `oms_managed_positions` read `status=open, current_quantity=1` throughout, updating every
        # ~8s. Broker 1 vs virtual 0 ⇒ CRITICAL page, while the OMS's own book was correct all along.
        #
        # ⇒ OUR BOOKS = the MAX of the two sources. `oms_managed_positions` is single-writer
        # (OMS-only) and is the ladder's own state, so it is the authority on "do we think we hold
        # this". Taking the max can only REMOVE a drift the OMS can account for; it can never hide
        # one, because a stale-open managed row still disagrees with a flat broker and still fires.
        # ⛔ It keys on broker_account_NAME (TEXT natural key, no FK), so it needs the name->id map.
        # ⛔ Inert when `oms_v2_exit_management_enabled` is OFF (no rows) — then this is a no-op and
        # behaviour is byte-identical to before.
        account_id_by_name = {account.name: account.id for account in account_lookup.values()}
        managed_quantities: dict[tuple[Any, str], Decimal] = {}
        managed_strategies: dict[tuple[Any, str], set[str]] = defaultdict(set)
        for managed in session.scalars(
            select(OmsManagedPosition).where(OmsManagedPosition.status == "open")
        ).all():
            account_id = account_id_by_name.get(managed.broker_account_name)
            if account_id is None:
                continue
            key = (account_id, managed.symbol)
            managed_quantities[key] = managed_quantities.get(key, Decimal("0")) + Decimal(
                str(managed.current_quantity)
            )
            managed_strategies[key].add(managed.strategy_code)

        findings: list[FindingSpec] = []
        keys = sorted(
            set(aggregates) | set(account_positions) | set(managed_quantities),
            key=lambda item: (str(item[0]), item[1]),
        )
        for account_id, symbol in keys:
            account = account_lookup.get(account_id)
            account_name = account.name if account is not None else str(account_id)
            if (account_name, symbol.upper()) in ignored_pairs:
                continue
            aggregate = aggregates.get((account_id, symbol))
            account_position = account_positions.get((account_id, symbol))

            virtual_quantity = aggregate["quantity"] if aggregate else Decimal("0")
            managed_quantity = managed_quantities.get((account_id, symbol), Decimal("0"))
            # OUR BOOKS = the OMS ladder's own state OR the virtual row, whichever knows more. See
            # the WETO note above: the virtual row false-zeroes inside the broker settle window while
            # oms_managed_positions stays correct, and comparing only the former manufactures a page.
            our_quantity = max(virtual_quantity, managed_quantity)
            account_quantity = account_position.quantity if account_position is not None else Decimal("0")
            quantity_delta = abs(account_quantity - our_quantity)
            if quantity_delta > tolerance:
                severity = "critical" if account_quantity == 0 or our_quantity == 0 else "warning"
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
                            # ⛔ BOTH sources are reported, never collapsed. When these disagree the
                            # reader must be able to see WHICH book was wrong — on 2026-08-14 the
                            # virtual row said 0 and the managed row said 1, and only one was right.
                            "virtual_quantity": str(virtual_quantity),
                            "managed_quantity": str(managed_quantity),
                            "our_quantity": str(our_quantity),
                            "quantity_delta": str(quantity_delta),
                            "strategy_codes": sorted(
                                set(aggregate["strategy_codes"] if aggregate else [])
                                | managed_strategies.get((account_id, symbol), set())
                            ),
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
