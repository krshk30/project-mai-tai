"""Durable publication fence for exact cancellation receipt assessments."""

from datetime import UTC
from uuid import UUID

from sqlalchemy import select, update

from project_mai_tai.cancel_terminal_proof import TERMINAL
from project_mai_tai.db.models import TradeIntent

KEY = "cancel_feedback_published"


def _utc(at):
    return at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)


def _binding(intent):
    at = _utc(intent.updated_at)
    p = intent.payload
    return {"event_id": p["event_id"], "source_service": p["source_service"],
        "metadata": p["metadata"], "status": intent.status, "updated_at": at.isoformat(),
        "refusal_origin": p.get("refusal_origin", ""), "refusal_code": p.get("refusal_code", "")}


def feedback_published(intent):
    try:
        raw = intent.payload[KEY]
        events = raw["feedback_event_ids"]
        return (raw["receipt"] == _binding(intent) and isinstance(events, list) and bool(events)
                and len(set(events)) == len(events) and all(str(UUID(e)) == e for e in events))
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def mark_feedback_published(session_factory, intent_id, revision, payload, feedback):
    """Called only after every normal feedback publish succeeds, off the loop."""
    if not feedback or len(feedback) > 32:
        return False
    with session_factory() as session:
        row = session.scalar(select(TradeIntent).where(TradeIntent.id == intent_id).with_for_update())
        if (row is None or row.payload != payload or _utc(row.updated_at) != _utc(revision)
                or row.status not in TERMINAL or row.intent_type != "cancel"
                or row.payload.get("source_service") != "schwab-1m-v2"):
            return False
        if any(e.payload.intent_db_id != intent_id
               or str(e.payload.intent_event_id) != payload.get("event_id")
               or e.payload.intent_type != "cancel" or e.payload.symbol != row.symbol for e in feedback):
            return False
        marker = {"receipt": _binding(row), "feedback_event_ids": [str(e.event_id) for e in feedback]}
        session.execute(update(TradeIntent).where(TradeIntent.id == intent_id).values(
            payload={**payload, KEY: marker}, updated_at=revision))
        session.commit()
        return True
