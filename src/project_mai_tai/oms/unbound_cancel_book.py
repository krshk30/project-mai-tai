"""Exact cancel-receipt Webull inventory journal, never a fabricated target ID."""

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from project_mai_tai.broker_adapters.cancel_terminal import acquire_complete_working_book, broker_binding
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.cancel_terminal_proof import BookOrder, CompleteWorkingBook, FRESHNESS_MS, TERMINAL
from project_mai_tai.db.models import BrokerAccount, TradeIntent

JOURNAL_KEY = "cancel_unbound_working_book"
REASONS = {"scanner_removal": "watchlist-removed", "retry_exhausted": "retry_budget_exhausted",
           "false_flip_restore": "false_flip_restore"}


def _epoch(value):
    if not isinstance(value, datetime):
        raise ValueError("receipt time missing")
    return int((value.replace(tzinfo=UTC) if value.tzinfo is None else value).timestamp() * 1000)


def _binding(intent, account, actual_id):
    """Retain actual event/intent identity even when no order was ever dispatched."""
    payload = intent.payload
    md = payload["metadata"]
    purpose = md["clearwait_purpose"]
    generation = md["clearwait_opportunity_id"]
    token = md["clearwait_removal_token"]
    UUID(payload["event_id"])
    if (account.provider != "webull" or not actual_id
            or (account.external_account_id and account.external_account_id != actual_id)
            or intent.intent_type != "cancel" or intent.side != "buy" or intent.status not in TERMINAL
            or payload["source_service"] != "schwab-1m-v2"
            or not isinstance(token, str) or not token or token.strip() != token
            or not isinstance(generation, str) or not generation.isdigit() or int(generation) <= 0
            or str(md.get("fanout_segment_id")) != generation or md.get("clearwait_buy_only") != "true"
            or purpose not in REASONS or intent.reason != REASONS[purpose]):
        raise ValueError("unbound receipt binding unknown")
    return {"intent_id": str(intent.id), "event_id": payload["event_id"],
            "account_name": account.name, "account_id": actual_id, "provider": account.provider,
            "symbol": intent.symbol, "token": token, "generation": generation, "purpose": purpose,
            "created_at_ms": _epoch(intent.created_at), "observed_at_ms": _epoch(intent.updated_at),
            "status": intent.status, "refusal_origin": payload.get("refusal_origin", ""),
            "refusal_code": payload.get("refusal_code", "")}


def _read(session_factory, adapter, intent_id):
    with session_factory() as session:
        intent = session.get(TradeIntent, intent_id)
        if intent is None:
            return None
        account = session.get(BrokerAccount, intent.broker_account_id)
        if account is None:
            return None
        try:
            leaf, actual_id = broker_binding(adapter, account.name)
            if not isinstance(leaf, WebullBrokerAdapter):
                return None
            binding = _binding(intent, account, actual_id)
            return binding, dict(intent.payload), intent.updated_at
        except (ValueError, KeyError, TypeError, AttributeError):
            return None


def _write(session_factory, adapter, snapshot, book):
    binding, payload, revision = snapshot
    with session_factory() as session:
        intent = session.scalar(select(TradeIntent).where(
            TradeIntent.id == UUID(binding["intent_id"])).with_for_update())
        if intent is None or intent.updated_at != revision or intent.payload != payload:
            return False
        account = session.get(BrokerAccount, intent.broker_account_id)
        try:
            _leaf, actual_id = broker_binding(adapter, account.name)
            if _binding(intent, account, actual_id) != binding:
                return False
        except (ValueError, KeyError, TypeError, AttributeError):
            return False
        session.execute(update(TradeIntent).where(TradeIntent.id == intent.id).values(
            payload={**payload, JOURNAL_KEY: {"binding": binding, "book": asdict(book)}},
            updated_at=revision))
        session.commit()
        return True


async def acquire_unbound_request_working_book(session_factory, adapter, intent_id):
    """Called only by the existing bounded, post-receipt OMS proof worker."""
    snapshot = await asyncio.to_thread(_read, session_factory, adapter, intent_id)
    if snapshot is None:
        return False
    binding = snapshot[0]
    book = await acquire_complete_working_book(adapter, binding["account_name"],
        after_ms=binding["observed_at_ms"])
    if book is None:
        return False
    return await asyncio.to_thread(_write, session_factory, adapter, snapshot, book)


def load_unbound_request_working_books(session, intents, expected):
    """Read inside F's existing locked transaction; never read HTTP or commit.

    Match the exact request, then require the latest relevant receipt's journal.
    Missing/malformed/newer-unanswered evidence omits that account (UNKNOWN).
    F separately retains dispatch, ownership and exact request/CAS fences.
    """
    groups = {}
    unreadable = set()
    if expected.request_id != expected.token:
        return {}
    for intent in intents:
        account = session.get(BrokerAccount, intent.broker_account_id)
        if (account is None or account.name not in expected.account_ids
                or expected.account_providers.get(account.name) != "webull"
                or intent.symbol != expected.symbol or intent.intent_type != "cancel"):
            continue
        payload = intent.payload
        md = payload.get("metadata") if isinstance(payload, dict) else None
        if not isinstance(md, dict):
            unreadable.add(account.name)
        elif md.get("clearwait_removal_token") == expected.token:
            groups.setdefault(account.name, []).append((intent, account))
    result = {}
    now = int(datetime.now(UTC).timestamp() * 1000)
    for name, rows in groups.items():
        if name in unreadable:
            continue
        try:
            bindings = [(_binding(intent, account, expected.account_ids[name]), intent) for intent, account in rows]
            if any(b["generation"] != expected.generation or b["purpose"] != expected.purpose
                   or b["created_at_ms"] < expected.requested_at_ms
                   or b["observed_at_ms"] < expected.requested_at_ms for b, _ in bindings):
                continue
            binding, intent = max(bindings, key=lambda pair: pair[0]["observed_at_ms"])
            raw = intent.payload[JOURNAL_KEY]
            if raw["binding"] != binding:
                continue
            data = dict(raw["book"])
            data["orders"] = tuple(BookOrder(**row) for row in data["orders"])
            book = CompleteWorkingBook(**data)
            if (book.complete is not True or book.source != "broker" or book.coverage != "all_working"
                    or (book.account_name, book.account_id) != (name, expected.account_ids[name])
                    or type(book.started_at_ms) is not int or type(book.finished_at_ms) is not int
                    or not max(expected.requested_at_ms, binding["observed_at_ms"]) <=
                        book.started_at_ms <= book.finished_at_ms <= now
                    or now - book.started_at_ms > FRESHNESS_MS):
                continue
            result[name] = book
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
    return result
