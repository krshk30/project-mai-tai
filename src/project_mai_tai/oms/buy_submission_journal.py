"""Durable pre-wire BUY attempts and transactional never-sent admission fences.

No broker inventory is inferred here. Legacy opportunities, uncertain answers,
and incomplete writer coverage cannot acquire a never-sent certificate.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from dataclasses import dataclass
import logging
from time import time_ns
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, JSON, String, or_, select, text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from project_mai_tai.broker_adapters.cancel_terminal import broker_binding
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.cancel_terminal_proof import NeverSentScope, NeverSentWitness, TerminalSubmissionWitness
from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, TradeIntent
from project_mai_tai.fanout_segment_store import SNAPSHOT_TYPE, current_session_anchor

PROTOCOL = "durable-buy-v1"


def now_ms() -> int:
    return time_ns() // 1_000_000


class BuyCoverageEpoch(Base):
    __tablename__ = "oms_buy_coverage_epochs"
    process_id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_name: Mapped[str] = mapped_column(String(64))
    protocol: Mapped[str] = mapped_column(String(32))
    started_at_ms: Mapped[int] = mapped_column(BigInteger)


class BuySubmissionToken(Base):
    __tablename__ = "oms_buy_submission_tokens"
    id: Mapped[UUID] = mapped_column(Uuid(), primary_key=True, default=uuid4)
    process_id: Mapped[UUID] = mapped_column(Uuid(), index=True)
    account_id: Mapped[str] = mapped_column(String(128), index=True)
    account_name: Mapped[str] = mapped_column(String(64))
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    client_order_id: Mapped[str] = mapped_column(String(128))
    generation: Mapped[str] = mapped_column(String(128))
    opportunity_started_at_ms: Mapped[int] = mapped_column(BigInteger)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, index=True)
    state: Mapped[str] = mapped_column(String(32), index=True)
    wire_kind: Mapped[str] = mapped_column(String(32))
    answers: Mapped[list] = mapped_column(JSON, default=list)


class BuyAdmissionClosure(Base):
    __tablename__ = "oms_buy_admission_closures"
    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    generation: Mapped[str] = mapped_column(String(128), primary_key=True)
    opportunity_started_at_ms: Mapped[int] = mapped_column(BigInteger)
    request_id: Mapped[str] = mapped_column(String(128))
    request_token: Mapped[str] = mapped_column(String(128))
    closed_at_ms: Mapped[int] = mapped_column(BigInteger)


def lock_buy_scope(session, account_id: str, symbol: str) -> None:
    """F must hold this lock through owned-row/request CAS and final commit."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                        {"scope": f"{PROTOCOL}:{account_id}:{symbol}"})


def opportunity_start_ms(session, symbol: str, generation: str, observed_at_ms: int) -> int:
    """Original durable bind observation, never segment-ID-as-clock or latest rewrite."""
    anchor = current_session_anchor(datetime.fromtimestamp(observed_at_ms / 1000, UTC)).isoformat()
    rows = list(session.scalars(select(DashboardSnapshot).where(
        DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
        DashboardSnapshot.payload["symbol"].as_string() == symbol,
        DashboardSnapshot.payload["fanout_segment_id"].as_string() == generation,
    ).order_by(DashboardSnapshot.created_at, DashboardSnapshot.id)))
    if not rows:
        return 0
    first = rows[0]
    payload = first.payload
    if (payload.get("strategy_code") != "schwab_1m_v2" or payload.get("active") is not True
            or payload.get("reason") not in {"segment_bind", "flip_owned_opportunity_v2_bind"}
            or payload.get("schema_version") != 1
            or payload.get("session_anchor") != anchor or not isinstance(first.created_at, datetime)):
        return 0
    at = first.created_at.replace(tzinfo=UTC) if first.created_at.tzinfo is None else first.created_at
    epoch = int(at.timestamp() * 1000)
    return epoch if 0 < epoch <= observed_at_ms else 0


@dataclass(frozen=True)
class NonworkingBuySubmissionWitness:
    token_id: UUID
    order_id: UUID
    scope: NeverSentScope
    client_order_id: str
    broker_order_id: str
    observed_at_ms: int


def _day_nonworking_token(session, token, order, scope, observed_at_ms):
    """Exact current request's typed absence, not a zero-fill/order-status receipt."""
    from project_mai_tai.cancel_terminal_proof import evaluate_cancel_terminal
    from project_mai_tai.oms.cancel_terminal import load_cancel_terminal_evidence, receipt_from_intent
    from project_mai_tai.webull_day_cancel_proof import DayCancelAbsence

    if (not isinstance(scope, NeverSentScope) or token.process_id != scope.coverage_process_id
            or token.account_name != scope.account_name or token.account_id != scope.account_id
            or token.symbol != scope.symbol
            or token.generation != scope.generation
            or token.opportunity_started_at_ms != scope.opportunity_started_at_ms):
        return None
    account = session.get(BrokerAccount, order.broker_account_id)
    if account is None or account.provider != "webull":
        return None
    request_at = datetime.fromtimestamp(scope.requested_at_ms / 1000, UTC)
    intent = session.scalar(select(TradeIntent).where(
        TradeIntent.broker_account_id == account.id, TradeIntent.symbol == token.symbol,
        TradeIntent.intent_type == "cancel", TradeIntent.created_at >= request_at,
    ).order_by(TradeIntent.updated_at.desc(), TradeIntent.created_at.desc(), TradeIntent.id.desc()).limit(1))
    if intent is None or not isinstance(intent.payload, dict):
        return None
    md = intent.payload.get("metadata")
    if (not isinstance(md, dict) or intent.payload.get("source_service") != "schwab-1m-v2"
            or md.get("clearwait_removal_token") != scope.request_token
            or md.get("clearwait_opportunity_id") != scope.generation
            or md.get("clearwait_buy_only") != "true"
            or md.get("buy_submission_process_id") != str(token.process_id)
            or md.get("target_client_order_id") != token.client_order_id):
        return None
    receipt = receipt_from_intent(intent, account)
    evidence = load_cancel_terminal_evidence(session, [intent])
    proof = evidence.get(receipt.scope.event_id) if receipt is not None else None
    if (proof is None or not isinstance(proof.day_absence, DayCancelAbsence)
            or proof.day_absence.bound.broker_order_id != order.broker_order_id
            or proof.day_absence.bound.scope.client_order_id != token.client_order_id
            or proof.day_absence.bound.submitted_at_ms < token.created_at_ms
            or not evaluate_cancel_terminal(receipt, proof, now_ms=observed_at_ms).terminal):
        return None
    latest = session.scalar(select(BrokerOrderEvent).where(BrokerOrderEvent.order_id == order.id)
        .order_by(BrokerOrderEvent.event_at.desc(), BrokerOrderEvent.id.desc()).limit(1))
    if latest is None:
        return None
    at = latest.event_at.replace(tzinfo=UTC) if latest.event_at.tzinfo is None else latest.event_at
    if int(at.timestamp() * 1000) > receipt.observed_at_ms:
        return None
    return NonworkingBuySubmissionWitness(token.id, order.id, scope, token.client_order_id,
                                          order.broker_order_id, observed_at_ms)


def nonworking_buy_submission_witnesses(session, scope: NeverSentScope, *, observed_at_ms: int):
    """Read-only filter for F's working-row guard in its locked consumer unit.

    This cannot close admission or refund fills; F must still execute its complete
    owned-row, intent, request-CAS and all-attempt terminal admission checks.
    """
    if (not isinstance(scope, NeverSentScope) or type(observed_at_ms) is not int
            or not 0 < scope.opportunity_started_at_ms <= scope.requested_at_ms <= observed_at_ms
            or scope.session_key != current_session_anchor(datetime.fromtimestamp(
                observed_at_ms / 1000, UTC)).isoformat()
            or opportunity_start_ms(session, scope.symbol, scope.generation,
                                    observed_at_ms) != scope.opportunity_started_at_ms):
        return ()
    attempts = session.scalars(select(BuySubmissionToken).where(
        BuySubmissionToken.account_id == scope.account_id,
        BuySubmissionToken.symbol == scope.symbol)).all()
    witnesses = []
    for token in attempts:
        if not _token_terminal(session, token, observed_at_ms, scope=scope):
            continue
        order = session.scalar(select(BrokerOrder).join(BrokerAccount).where(
            BrokerAccount.name == scope.account_name,
            or_(BrokerAccount.external_account_id == scope.account_id,
                BrokerAccount.external_account_id.is_(None)),
            BrokerOrder.client_order_id == token.client_order_id,
            BrokerOrder.symbol == scope.symbol, BrokerOrder.side == "buy"))
        witness = _day_nonworking_token(session, token, order, scope, observed_at_ms) if order else None
        if witness is not None:
            witnesses.append(witness)
    return tuple(witnesses)


def _token_terminal(session, token: BuySubmissionToken, observed_at_ms: int, *, scope=None) -> bool:
    epoch = session.get(BuyCoverageEpoch, (token.process_id, token.account_id))
    if (epoch is None or epoch.protocol != PROTOCOL or epoch.account_name != token.account_name
            or not 0 < epoch.started_at_ms <= token.created_at_ms <= observed_at_ms):
        return False
    if token.state not in {"reported_ambiguous", "broker_terminal"} or not isinstance(token.answers, list):
        return False
    answers = [a for a in token.answers if isinstance(a, dict)
               and a.get("client_order_id") == token.client_order_id]
    broker_ids = {a.get("broker_order_id") for a in answers
                  if a.get("origin") == "broker" and isinstance(a.get("broker_order_id"), str)
                  and a["broker_order_id"]}
    if len(broker_ids) != 1:
        return False  # Lost acknowledgement/replacement identity cannot be guessed.
    order = session.scalar(select(BrokerOrder).join(BrokerAccount).where(
        BrokerAccount.name == token.account_name,
        # The durable writer epoch is bound to the actual configured broker ID.
        # A missing display/account-table ID cannot contradict that binding.
        or_(BrokerAccount.external_account_id == token.account_id,
            BrokerAccount.external_account_id.is_(None)),
        BrokerOrder.client_order_id == token.client_order_id,
        BrokerOrder.broker_order_id == next(iter(broker_ids)),
        BrokerOrder.symbol == token.symbol, BrokerOrder.side == "buy",
    ).with_for_update(of=BrokerOrder))
    terminal = {"cancelled", "canceled", "rejected", "expired", "filled"}
    if order is None:
        return False
    if order.status not in terminal:
        return isinstance(_day_nonworking_token(session, token, order, scope, observed_at_ms),
                          NonworkingBuySubmissionWitness)
    events = list(session.scalars(select(BrokerOrderEvent).where(
        BrokerOrderEvent.order_id == order.id,
    ).order_by(BrokerOrderEvent.event_at.desc(), BrokerOrderEvent.id.desc())))
    if not events:
        return False
    event = events[0]
    at = event.event_at.replace(tzinfo=UTC) if event.event_at.tzinfo is None else event.event_at
    return (event.event_source == "broker" and event.event_type == order.status
            and token.created_at_ms <= int(at.timestamp() * 1000) <= observed_at_ms)


def close_never_sent_admission(session, scope: NeverSentScope, *, observed_at_ms: int) -> NeverSentWitness:
    return _close_buy_admission(session, scope, observed_at_ms=observed_at_ms, terminal=False)


def close_terminal_buy_admission(
    session, scope: NeverSentScope, *, observed_at_ms: int,
) -> TerminalSubmissionWitness:
    """Consumer must separately prove every owned row closed in this transaction."""
    return _close_buy_admission(session, scope, observed_at_ms=observed_at_ms, terminal=True)


def _close_buy_admission(session, scope: NeverSentScope, *, observed_at_ms: int, terminal: bool):
    """Assess and close admission in the consumer's transaction; never commit it.

    This is usable only when that same transaction commits its exact request CAS.
    Roll back both closure and witness on failed CAS. Absence is not broker flatness:
    retain working BUY, intent, cancel, ownership and fill guards independently.
    """
    lock_buy_scope(session, scope.account_id, scope.symbol)
    epoch = session.get(BuyCoverageEpoch, (scope.coverage_process_id, scope.account_id))
    coverage = epoch.started_at_ms if epoch is not None else 0
    coids = ()
    def result(reason, accepted=False):
        if terminal:
            return TerminalSubmissionWitness(scope, observed_at_ms, coverage, accepted, coids, reason)
        return NeverSentWitness(scope, observed_at_ms, coverage, accepted, reason)
    if (not all(isinstance(v, str) and v and v.strip() == v for v in (
            scope.account_name, scope.account_id, scope.symbol, scope.generation,
            scope.request_id, scope.request_token, scope.session_key))
            or type(scope.opportunity_started_at_ms) is not int
            or type(scope.requested_at_ms) is not int or type(observed_at_ms) is not int
            or not 0 < scope.opportunity_started_at_ms <= scope.requested_at_ms <= observed_at_ms):
        return result("never_sent_scope_unknown")
    if scope.session_key != current_session_anchor(datetime.fromtimestamp(observed_at_ms / 1000, UTC)).isoformat():
        return result("never_sent_session_changed")
    if (epoch is None or epoch.protocol != PROTOCOL or epoch.account_name != scope.account_name
            or not 0 < coverage <= scope.opportunity_started_at_ms):
        return result("never_sent_legacy_or_coverage_unknown")
    if opportunity_start_ms(session, scope.symbol, scope.generation, observed_at_ms) != scope.opportunity_started_at_ms:
        return result("never_sent_opportunity_provenance_unknown")
    attempts = list(session.scalars(select(BuySubmissionToken).where(
        BuySubmissionToken.account_id == scope.account_id,
        BuySubmissionToken.symbol == scope.symbol)))
    if terminal:
        current = [t for t in attempts if t.created_at_ms >= scope.opportunity_started_at_ms]
        if (not current or any(t.account_name != scope.account_name for t in attempts)
                or any(t.generation != scope.generation
                       or t.opportunity_started_at_ms != scope.opportunity_started_at_ms for t in current)
                or any(not _token_terminal(session, t, observed_at_ms, scope=scope) for t in attempts)):
            return result("submission_terminal_unproven")
        coids = tuple(sorted({t.client_order_id for t in current}))
    elif any(t.created_at_ms >= scope.opportunity_started_at_ms or t.state != "broker_terminal"
             for t in attempts):
        return result("never_sent_attempt_or_ambiguity_present")
    key = scope.account_id, scope.symbol, scope.generation
    prior = session.get(BuyAdmissionClosure, key)
    if prior is not None and (prior.request_id, prior.request_token, prior.opportunity_started_at_ms) != (
            scope.request_id, scope.request_token, scope.opportunity_started_at_ms):
        return result("never_sent_request_fence_changed")
    if prior is None:
        session.add(BuyAdmissionClosure(account_id=scope.account_id, symbol=scope.symbol,
            generation=scope.generation, opportunity_started_at_ms=scope.opportunity_started_at_ms,
            request_id=scope.request_id, request_token=scope.request_token, closed_at_ms=observed_at_ms))
        session.flush()
    if terminal:
        for attempt in attempts:
            attempt.state = "broker_terminal"
    return result("broker_terminal_durable_admission_closed" if terminal
                  else "never_sent_durable_admission_closed", True)


class BuyAdmissionClosed(RuntimeError):
    pass


class DurableBuyAdapter:
    """Central OMS boundary for physical BUY submits and native BUY replacements.

    Ordinary parent transactions cannot supply durability. Private transactions
    commit off-loop, and cancellation waits for their outcome before allowing wire.
    No timeout, pruning or locally labelled rejection terminalizes a token.
    """
    def __init__(self, adapter, session_factory):
        self.cancel_terminal_delegate = adapter
        self.session_factory = session_factory
        self.process_id = uuid4()
        self._covered = set()
        self._start_lock = asyncio.Lock()

    def __getattr__(self, name):
        return getattr(self.cancel_terminal_delegate, name)

    async def _db(self, function, *args):
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def commit_order_reports(self, session):
        # The intent owns this session exclusively until its commit finishes.
        # Cancellation must not close it while the worker is still using it.
        await self._db(session.commit)

    def _coverage(self, name, account_id):
        with self.session_factory() as session:
            key = self.process_id, account_id
            if session.get(BuyCoverageEpoch, key) is None:
                session.add(BuyCoverageEpoch(process_id=self.process_id, account_id=account_id,
                    account_name=name, protocol=PROTOCOL, started_at_ms=now_ms()))
            session.commit()

    async def ensure_coverage(self, account_name):
        _leaf, account_id = broker_binding(self.cancel_terminal_delegate, account_name)
        async with self._start_lock:
            if account_id not in self._covered:
                await self._db(self._coverage, account_name, account_id)
                self._covered.add(account_id)
        return self.process_id

    async def start(self):
        delegate = self.cancel_terminal_delegate
        names = (delegate.provider_by_account if isinstance(delegate, RoutingBrokerAdapter)
                 else getattr(delegate, "accounts_by_name", {}))
        for name in names:
            try:
                await self.ensure_coverage(name)
            except (ValueError, AttributeError):
                continue  # Unsupported/missing routing is never a covered account.

    def _prepare(self, request, account_id, wire_kind):
        md = request.metadata
        generation = str(md.get("fanout_segment_id", md.get("clearwait_opportunity_id", ""))).strip()
        with self.session_factory() as session:
            lock_buy_scope(session, account_id, request.symbol)
            opportunity = opportunity_start_ms(session, request.symbol, generation, now_ms()) if generation else 0
            closures = list(session.scalars(select(BuyAdmissionClosure).where(
                BuyAdmissionClosure.account_id == account_id,
                BuyAdmissionClosure.symbol == request.symbol)))
            if any(c.generation == generation or opportunity <= c.opportunity_started_at_ms for c in closures):
                raise BuyAdmissionClosed("BUY generation closed or unidentifiable after closure")
            token = BuySubmissionToken(process_id=self.process_id, account_id=account_id,
                account_name=request.broker_account_name, symbol=request.symbol,
                client_order_id=request.client_order_id, generation=generation,
                opportunity_started_at_ms=opportunity, created_at_ms=now_ms(), state="submitting",
                wire_kind=wire_kind, answers=[])
            session.add(token)
            session.commit()
            return token.id

    def _reported(self, token_id, reports):
        with self.session_factory() as session:
            token = session.get(BuySubmissionToken, token_id)
            if token is None:
                raise RuntimeError("durable BUY token disappeared")
            token.state = "reported_ambiguous"
            token.answers = [{"status": r.event_type, "origin": r.origin,
                "client_order_id": r.client_order_id, "broker_order_id": r.broker_order_id,
                "filled_quantity": str(r.filled_quantity), "reason": r.reason,
                "metadata": dict(r.metadata)} for r in reports]
            session.commit()

    async def _wire(self, request, callback, kind):
        delegate = self.cancel_terminal_delegate
        leaf = (delegate._adapter_for_account(request.broker_account_name)
                if isinstance(delegate, RoutingBrokerAdapter) else delegate)
        if not isinstance(leaf, (SchwabBrokerAdapter, WebullBrokerAdapter)):
            return await callback()  # Simulated/paper adapters do not wire these brokers.
        if request.intent_type == "cancel":
            return await callback()
        contains_buy = request.side == "buy"
        if isinstance(leaf, SchwabBrokerAdapter) and hasattr(leaf, "settings") and leaf._is_bracket_request(request):
            contains_buy = not leaf._is_exit_only_oco_request(request)
        if isinstance(leaf, WebullBrokerAdapter) and hasattr(leaf, "settings") and leaf._is_bracket_request(request):
            contains_buy = not leaf._is_exit_only_pair(request)
        if not contains_buy:
            return await callback()
        _leaf, account_id = broker_binding(delegate, request.broker_account_name)
        try:
            await self.ensure_coverage(request.broker_account_name)
            token_id = await self._db(self._prepare, request, account_id, kind)
        except BuyAdmissionClosed:
            raise
        except Exception:
            logging.getLogger(__name__).error(
                "[OMS-SUBMIT-TOKEN] sym=%s acct=%s result=commit_failed buy_blocked=1",
                request.symbol, request.broker_account_name, exc_info=True)
            raise
        logging.getLogger(__name__).info(
            "[OMS-SUBMIT-TOKEN] sym=%s acct=%s result=committed buy_blocked=0",
            request.symbol, request.broker_account_name)
        # Persist before *any* wire, including a replacement. On an exception or
        # cancellation the durable submitting token remains an ambiguity blocker.
        result = await callback()
        reports = result if isinstance(result, list) else [] if result is None else [result]
        await self._db(self._reported, token_id, reports)
        return result

    async def submit_order(self, request):
        return await self._wire(request, lambda: self.cancel_terminal_delegate.submit_order(request), "submit")

    async def replace_bracket_order(self, request, broker_order_id):
        return await self._wire(request,
            lambda: self.cancel_terminal_delegate.replace_bracket_order(request, broker_order_id), "replace")
