"""Per-request cancel evidence journal; database work stays in worker threads."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from project_mai_tai.broker_adapters.cancel_terminal import (
    acquire_broker_cancel_evidence, broker_binding,
)
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.cancel_terminal_proof import (
    BookOrder, CancelReceipt, CancelScope, CancelTerminalEvidence, CompleteWorkingBook,
    evaluate_cancel_terminal,
)
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, TradeIntent
from project_mai_tai.oms.unbound_cancel_book import acquire_unbound_request_working_book

JOURNAL_KEY = "cancel_terminal_evidence"
BINDING_KEY = "cancel_target_binding"


def bind_cancel_target(intent, event, adapter, target_order=None) -> None:
    """Record the actual selected target before dispatch, never infer a historical coid."""
    payload = dict(intent.payload or {})
    md = dict(payload.get("metadata", {}))
    coid = target_order.client_order_id if target_order is not None else md.get("target_client_order_id")
    coid = coid or md.get("client_order_id", "")
    candidates = [md[k] for k in ("target_client_order_id", "client_order_id") if md.get(k)]
    if not isinstance(coid, str) or not coid or any(candidate != coid for candidate in candidates):
        return
    try:
        _leaf, account_id = broker_binding(adapter, event.payload.broker_account_name)
    except (ValueError, AttributeError):
        return
    if target_order is not None and target_order.symbol != event.payload.symbol:
        return
    md["target_client_order_id"] = coid
    if target_order is not None and target_order.broker_order_id:
        md["broker_order_id"] = target_order.broker_order_id
    scope = CancelScope(event.payload.broker_account_name, account_id,
                        event.payload.symbol, coid, str(event.event_id))
    payload["metadata"] = md
    payload[BINDING_KEY] = {
        "scope": asdict(scope),
        "broker_order_id": md.get("broker_order_id", ""),
        "purpose": md.get("clearwait_purpose", md.get("reason", event.payload.reason)),
        "token": md.get("clearwait_removal_token", ""),
        "generation": md.get("clearwait_opportunity_id", md.get("fanout_segment_id", "")),
    }
    payload.pop(JOURNAL_KEY, None)
    intent.payload = payload


def receipt_from_intent(intent, account: BrokerAccount) -> CancelReceipt | None:
    payload = intent.payload or {}
    binding = payload.get(BINDING_KEY)
    if intent.intent_type != "cancel" or not isinstance(binding, dict):
        return None
    try:
        scope = CancelScope(**binding["scope"])
        md = payload["metadata"]
        at = intent.updated_at
        if (scope.account_name != account.name or scope.symbol != intent.symbol
                or (account.external_account_id and scope.account_id != account.external_account_id)
                or scope.event_id != payload.get("event_id")
                or scope.client_order_id != md.get("target_client_order_id")
                or (md.get("client_order_id") and scope.client_order_id != md["client_order_id"])
                or binding.get("token") != md.get("clearwait_removal_token", "")
                or binding.get("generation") != md.get(
                    "clearwait_opportunity_id", md.get("fanout_segment_id", ""))
                or binding.get("purpose") != md.get("clearwait_purpose", md.get("reason", intent.reason))
                or not isinstance(at, datetime)):
            return None
        epoch_ms = int(at.replace(tzinfo=UTC).timestamp() * 1000) if at.tzinfo is None else int(at.timestamp() * 1000)
        return CancelReceipt(scope, epoch_ms, intent.status,
                             payload.get("refusal_origin", ""), payload.get("refusal_code", ""))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def evidence_from_payload(payload: dict) -> CancelTerminalEvidence | None:
    try:
        raw = payload[JOURNAL_KEY]
        if raw["binding"] != payload[BINDING_KEY]:
            return None
        data = dict(raw["evidence"])
        receipt = dict(data.pop("receipt"))
        receipt["scope"] = CancelScope(**receipt["scope"])
        book = data.pop("book")
        if book is not None:
            book = dict(book)
            book["orders"] = tuple(BookOrder(**row) for row in book["orders"])
            book = CompleteWorkingBook(**book)
        return CancelTerminalEvidence(CancelReceipt(**receipt), book, **data)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def load_cancel_terminal_evidence(session, intents) -> dict[str, CancelTerminalEvidence]:
    """Read committed evidence inside the consumer's existing off-loop DB unit."""
    result = {}
    for intent in intents:
        account = session.get(BrokerAccount, intent.broker_account_id)
        if account is None:
            continue
        receipt = receipt_from_intent(intent, account)
        evidence = evidence_from_payload(intent.payload or {})
        if receipt is not None and evidence is not None and evidence.receipt == receipt:
            result[receipt.scope.event_id] = evidence
    return result


@dataclass(frozen=True)
class _Request:
    intent_id: UUID
    receipt: CancelReceipt
    payload: dict
    updated_at: datetime


def _read_request(session_factory, intent_id, adapter) -> _Request | None:
    with session_factory() as session:
        intent = session.get(TradeIntent, intent_id)
        if intent is None:
            return None
        account = session.get(BrokerAccount, intent.broker_account_id)
        receipt = receipt_from_intent(intent, account) if account else None
        if receipt is None:
            return None
        try:
            leaf, account_id = broker_binding(adapter, account.name)
        except (ValueError, AttributeError):
            return None
        provider = "schwab" if isinstance(leaf, SchwabBrokerAdapter) else "webull"
        if account.provider != provider or receipt.scope.account_id != account_id:
            return None
        broker_id = intent.payload[BINDING_KEY].get("broker_order_id", "")
        if account.provider == "schwab" and broker_id:
            # Schwab detail has no client id. Its broker id must already be bound
            # to this exact client/account/symbol in the committed order journal.
            target = session.scalar(select(BrokerOrder).where(
                BrokerOrder.broker_account_id == account.id,
                BrokerOrder.client_order_id == receipt.scope.client_order_id,
                BrokerOrder.broker_order_id == broker_id,
                BrokerOrder.symbol == receipt.scope.symbol,
            ))
            if target is None:
                return None
        return _Request(intent.id, receipt, dict(intent.payload), intent.updated_at)


def _write_evidence(session_factory, request: _Request, evidence: CancelTerminalEvidence, adapter) -> bool:
    with session_factory() as session:
        current = session.scalar(select(TradeIntent).where(
            TradeIntent.id == request.intent_id,
        ).with_for_update())
        if (current is None or current.status != request.receipt.status
                or current.updated_at != request.updated_at or current.payload != request.payload):
            return False
        account = session.get(BrokerAccount, current.broker_account_id)
        if account is None or receipt_from_intent(current, account) != request.receipt:
            return False
        try:
            leaf, account_id = broker_binding(adapter, account.name)
        except (ValueError, AttributeError):
            return False
        provider = "schwab" if isinstance(leaf, SchwabBrokerAdapter) else "webull"
        if account.provider != provider or account_id != request.receipt.scope.account_id:
            return False
        payload = {**current.payload, JOURNAL_KEY: {
            "binding": request.payload[BINDING_KEY], "evidence": asdict(evidence),
        }}
        # Evidence acquisition must not invent a new cancel receipt revision.
        session.execute(update(TradeIntent).where(TradeIntent.id == request.intent_id).values(
            payload=payload, updated_at=request.updated_at,
        ))
        session.commit()
        return True


async def acquire_cancel_terminal_evidence(
    session_factory, adapter, intent_ids, *, minimum_started_at_ms=None,
) -> dict[str, CancelTerminalEvidence]:
    """Acquire once for this request; no timer, order submit, retry, or expiry.

    Request consumers may call this again when a new explicit assessment needs a
    fresh book. Every read/write uses a private session entirely off the event loop.
    """
    result = {}
    for intent_id in intent_ids:
        request = await asyncio.to_thread(_read_request, session_factory, intent_id, adapter)
        if request is None:
            await acquire_unbound_request_working_book(
                session_factory, adapter, intent_id,
                minimum_started_at_ms=(minimum_started_at_ms or {}).get(intent_id, 0),
            )
            continue
        try:
            prior = evidence_from_payload(request.payload)
            if (prior is not None and prior.target_status and evaluate_cancel_terminal(
                    request.receipt, prior, now_ms=int(datetime.now(UTC).timestamp() * 1000)).terminal):
                evidence = prior
            else:
                evidence = await acquire_broker_cancel_evidence(
                    adapter, request.receipt,
                    broker_order_id=request.payload[BINDING_KEY].get("broker_order_id", ""),
                    minimum_started_at_ms=(minimum_started_at_ms or {}).get(intent_id, 0),
                )
        except Exception:
            evidence = CancelTerminalEvidence(request.receipt, None)
        if await asyncio.to_thread(_write_evidence, session_factory, request, evidence, adapter):
            result[request.receipt.scope.event_id] = evidence
    return result
