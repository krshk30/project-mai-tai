"""Validate explicit request refreshes in a private, off-loop database session."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from project_mai_tai.broker_adapters.cancel_terminal import broker_binding
from project_mai_tai.cancel_terminal_proof import FRESHNESS_MS, TERMINAL
from project_mai_tai.db.models import BrokerAccount, DashboardSnapshot, TradeIntent
from project_mai_tai.oms.unbound_cancel_book import REASONS
from project_mai_tai.oms.cancel_feedback import feedback_published


def _utc(value):
    if not isinstance(value, datetime):
        raise ValueError("missing receipt time")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def read_assessment_receipts(session_factory, adapter, payload, now_ms):
    """A refresh authorizes a read only, never a cancel, order or owner release."""
    try:
        UUID(payload["assessment_id"])
        at = payload["assessment_at_ms"]
        request = payload["request"]
        receipts = payload["receipts"]
        names = request["account_names"]
        purpose = request.get("purpose", "scanner_removal")
        if (payload.get("event_type") != "v2_cancel_terminal_assessment"
                or payload.get("source_service") != "schwab-1m-v2"
                or payload.get("schema_version") != 1
                or payload.get("trigger") not in {
                    "boot", "session_reset", "fresh_sell", "request_raised", "after_feedback"}
                or type(at) is not int or not 0 <= now_ms - at <= FRESHNESS_MS
                or request.get("active") is not True or request.get("schema_version") != 1
                or request.get("strategy_code") != "schwab_1m_v2"
                or not isinstance(names, list) or len(names) != 2 or len(set(names)) != 2
                or any(not isinstance(n, str) or not n for n in names)
                or not isinstance(receipts, list) or len(receipts) != len(names)
                or purpose not in REASONS
                or not 0 < int(request["requested_at_ms"]) <= at
                or not isinstance(request["token"], str) or not request["token"]
                or not isinstance(request["symbol"], str) or not request["symbol"]
                or len(request["symbol"]) > 32 or int(request["opportunity_id"]) <= 0):
            return ()
        with session_factory() as session:
            current = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == "v2_removed_wait",
                DashboardSnapshot.payload["symbol"].as_string() == request["symbol"],
            ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
            if current is None or not isinstance(current.payload, dict):
                return ()
            if not all(current.payload.get(k) == v for k, v in request.items()):
                return ()
            found = []
            providers = set()
            for receipt in receipts:
                account = session.scalar(select(BrokerAccount).where(
                    BrokerAccount.name == receipt["account_name"]))
                if account is None or account.name not in names or account.provider != receipt["provider"]:
                    return ()
                _leaf, actual_id = broker_binding(adapter, account.name)
                if actual_id != receipt["account_id"] or account.external_account_id not in {None, actual_id}:
                    return ()
                row = session.scalar(select(TradeIntent).where(
                    TradeIntent.broker_account_id == account.id,
                    TradeIntent.symbol == request["symbol"], TradeIntent.intent_type == "cancel",
                    TradeIntent.payload["metadata"]["clearwait_removal_token"].as_string() == request["token"],
                ).order_by(TradeIntent.updated_at.desc(), TradeIntent.created_at.desc(),
                           TradeIntent.id.desc()).limit(1))
                if (row is None or row.id != UUID(receipt["intent_id"]) or row.side != "buy"
                        or not feedback_published(row)):
                    return ()
                md = (row.payload or {}).get("metadata", {})
                if (row.status not in TERMINAL or row.status != receipt["status"]
                        or row.payload.get("source_service") != "schwab-1m-v2"
                        or row.payload.get("event_id") != receipt["event_id"]
                        or md.get("clearwait_opportunity_id") != request["opportunity_id"]
                        or md.get("fanout_segment_id") != request["opportunity_id"]
                        or md.get("clearwait_purpose") != purpose
                        or md.get("clearwait_buy_only") != "true" or md.get("reason") != REASONS[purpose]
                        or md.get("buy_submission_process_id") != receipt["coverage_process_id"]
                        or _utc(row.created_at).isoformat() != receipt["created_at"]
                        or _utc(row.updated_at).isoformat() != receipt["updated_at"]
                        or not int(request["requested_at_ms"]) <=
                            int(_utc(row.updated_at).timestamp() * 1000) <= at):
                    return ()
                found.append((account.name, row.id, at))
                providers.add(account.provider)
            if providers != {"schwab", "webull"} or {n for n, _, _ in found} != set(names):
                return ()
            return tuple(found)
    except (KeyError, ValueError, TypeError, AttributeError):
        return ()
