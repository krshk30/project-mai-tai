"""Passive EOD diagnostics only. No DB, broker, clock, cache or state writer."""
from decimal import Decimal
from uuid import UUID


def _data(value):
    if type(value) is dict:
        return value
    if value is None:
        return {}
    raw = object.__getattribute__(value, "__dict__")
    return raw if type(raw) is dict else {}


def _text(value):
    if value is None:
        return "NONE"
    if type(value) in (str, int, bool, Decimal, UUID):
        return str(value).replace("\n", " ").replace("\r", " ")[:160]
    return "UNKNOWN"


def _result(value):
    if type(value) is str:
        return _text(value), "0", "NONE"
    raw = _data(value)
    outcome = _text(raw.get("outcome", "NOT_OBSERVED"))
    reports = raw.get("reports", ())
    if type(reports) not in (tuple, list):
        return outcome, "UNKNOWN", "UNKNOWN"
    parts = []
    for report in reports[:2]:
        fields = _data(report)
        metadata = fields.get("metadata")
        metadata = metadata if type(metadata) is dict else {}
        parts.append("/".join(_text(fields.get(k)) for k in
            ("client_order_id", "event_type", "origin", "filled_quantity"))
            + "/" + _text(metadata.get("cancel_outcome")))
    return outcome, str(len(reports)), ";".join(parts) or "NONE"


def eod_oco_step(service, step, account, symbol, expected_row_id, scope):
    """Observe already-computed locals; a diagnostic failure cannot change gates.

    Raw instance dictionaries avoid ORM lazy loads and result/property calls.
    The supplied locals mapping is neither changed nor retained. Exception
    handling covers logging/formatting errors; original awaited cancellation
    and business exceptions retain their existing handling in the caller.
    """
    try:
        logger = _data(service).get("logger")
        if logger is None:
            return
        release, release_count, release_reports = _result(scope.get("release", scope.get("result")))
        confirm, confirm_count, confirm_reports = _result(scope.get("confirmed"))
        snapshot = _data(scope.get("snapshot"))
        row = _data(scope.get("row"))
        entry = _data(scope.get("entry"))
        logger.info(
            "[OMS-V2-EOD-OCO-STEP] account=%s symbol=%s row=%s step=%s base=%s "
            "entry=%s current_row=%s release=%s confirm=%s release_count=%s confirm_count=%s "
            "recorded=%s release_reports=%s confirm_reports=%s",
            _text(account), _text(symbol), _text(expected_row_id or snapshot.get("managed_row_id", "UNKNOWN")),
            _text(step), _text(scope.get("base", "NOT_OBSERVED")),
            _text(scope.get("entry_identity", entry.get("id", "NOT_OBSERVED"))),
            _text(scope.get("current_row_id", row.get("id", "NOT_OBSERVED"))),
            release, confirm, release_count, confirm_count, _text(scope.get("recorded", "NOT_OBSERVED")),
            release_reports, confirm_reports,
        )
    except Exception:
        pass  # Diagnostics supply no ownership, native fact, return or retry authority.
