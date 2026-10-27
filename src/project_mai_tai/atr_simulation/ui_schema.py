"""Structural display validation only; never reconstructs fills or economics."""

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
from uuid import UUID


STATUSES = {"AVAILABLE", "UNAVAILABLE", "UNKNOWN_SOURCE", "NO_ROWS_UNKNOWN"}
MODELS = {"LIVE": {"ACTUAL"}, "TODAY": {"SUPPLIED"},
          "VOLBODY": {"QUEUE_AWARE", "OPTIMISTIC"},
          "FIRSTBAR": {"MODELED_STOP_AT_OBSERVED_PRINT_VOLUME"},
          "FIRSTBAR_X3": {"MODELED_STOP_AT_OBSERVED_PRINT_VOLUME"}}


def _require(condition):
    if not condition:
        raise ValueError("simulation_stored_shape_invalid")


def _sequence(value):
    _require(isinstance(value, (list, tuple)))
    return value


def _number(value, *, positive=False):
    _require(not isinstance(value, bool) and isinstance(value, (str, int, float, Decimal)))
    number = Decimal(str(value))
    _require(number.is_finite() and (not positive or number > 0))


def _clock(value):
    _require(isinstance(value, Mapping))
    _require(type(value.get("event_ns")) is int and value["event_ns"] > 0)


def _fact(fact, *, exit_fact):
    _require(isinstance(fact, Mapping))
    _require(type(fact.get("quantity")) is int and fact["quantity"] > 0)
    _require("price" in fact)
    if fact["price"] is not None:
        _number(fact["price"], positive=True)
    else:
        _require(exit_fact)
    clock = fact.get("print_key", fact.get("at"))
    if clock is not None:
        _clock(clock)
    else:
        _require(exit_fact and "at" in fact)
    for reason in ("reason", "raw_reason", "mapped_reason"):
        _require(fact.get(reason) is None or isinstance(fact[reason], str))


def _json_default(value):
    if isinstance(value, Decimal):
        _number(value)
        return str(value)
    if isinstance(value, (datetime, date, UUID)):
        return str(value)
    raise TypeError("unsupported_simulation_storage_value")


@dataclass(frozen=True, init=False)
class _DetachedSimulationSnapshot:
    snapshot: dict

    def __init__(self, stored):
        object.__setattr__(self, "snapshot", _detach_validated_snapshot(stored))


def detached_simulation_snapshot(stored):
    """Own one normalized row tree; keep consumer datetimes for age checks."""
    return _DetachedSimulationSnapshot(stored)


def _detach_validated_snapshot(stored):
    if type(stored) is not dict:
        stored = deepcopy(stored)
    # Structural types must be checked before conversion can turn them to strings.
    validate_stored_view(stored)

    def row_default(value):
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        return _json_default(value)

    rows = stored["rows"]
    pending = [(rows, 0)]
    normalize_keys = False
    while pending:
        value, depth = pending.pop()
        if isinstance(value, dict) and any(type(key) is not str for key in value):
            normalize_keys = True
        if depth >= 64 or type(value) not in (dict, list, tuple):
            # Retain the old copy-depth refusal for unusual deeply nested input;
            # the fast codec must not silently admit trees the old copy rejected.
            rows = deepcopy(rows)
            normalize_keys = True
            break
        children = value.values() if isinstance(value, dict) else value
        pending.extend((item, depth + 1) for item in children
            if isinstance(item, (dict, list, tuple)))
    if normalize_keys:
        # The JSON codec coerces None/True keys differently from the public API.
        from .repository import json_value

        rows = json_value(deepcopy(rows))
    snapshot = deepcopy({key: value for key, value in stored.items() if key != "rows"})
    snapshot["rows"] = json.loads(json.dumps(rows, default=row_default, allow_nan=False))
    validate_stored_view(snapshot)
    return snapshot


def validate_stored_view(stored):
    """Validate the read-port tree before aggregation, including renderer operands."""
    _require(isinstance(stored, Mapping))
    page, consumers = _sequence(stored["rows"]), _sequence(stored["consumers"])
    _require(type(stored["truncated"]) is bool)
    _require(type(stored.get("workers_truncated", False)) is bool)
    run = stored["run"]
    _require(run is None or isinstance(run, Mapping))
    if run is not None:
        _require(isinstance(run.get("coverage"), Mapping))
        complete = run["coverage"].get("complete")
        _require(complete is None or type(complete) is bool)
    status = stored.get("read_status", "AVAILABLE" if run is not None else "UNKNOWN_SOURCE")
    _require(isinstance(status, str) and status in STATUSES)
    _require(all(isinstance(issue, str) for issue in _sequence(stored.get("read_issues", []))))
    for consumer in consumers:
        _require(isinstance(consumer, Mapping))
        _require(isinstance(consumer.get("updated_at"), datetime))
        _require(isinstance(consumer.get("consumer_id"), str))
        _require(isinstance(consumer.get("status"), str))
        _require(all(isinstance(issue, str) for issue in _sequence(consumer["issues"])))
    for row in page:
        _require(isinstance(row, Mapping) and isinstance(row.get("identity"), Mapping))
        _require(all(isinstance(row["identity"].get(key), str) and row["identity"][key]
                     for key in ("trading_day", "symbol", "broker", "account_id",
                                 "strategy", "flip_id", "segment_id")))
        mode, model = row.get("mode"), row.get("model")
        _require(isinstance(mode, str) and mode in MODELS)
        _require(isinstance(model, str) and model in MODELS[mode])
        _require(isinstance(row.get("decision"), str))
        _require("gross_pnl" in row)
        for field in ("gross_pnl", "pnl_difference"):
            if row.get(field) is not None:
                _number(row[field])
        for field in ("entries", "fills", "exits"):
            for fact in _sequence(row.get(field, [])):
                _fact(fact, exit_fact=field == "exits")
                if field == "exits" and (fact["price"] is None or fact.get("at") is None):
                    _require(row["gross_pnl"] is None)
        if row["gross_pnl"] is not None:
            _require(bool(row.get("fills", row.get("entries", []))) and bool(row.get("exits", [])))
        if "reason_pair" in row:
            _require(isinstance(row["reason_pair"], Mapping))
            for side in ("live", "sim"):
                _require(all(reason is None or isinstance(reason, str)
                             for reason in _sequence(row["reason_pair"][side])))
        if mode != "LIVE":
            _require(all(field in row for field in (
                "first_fill_ts", "vwap", "filled_qty", "remainder_cancel_reason")))
            quantity = row["filled_qty"]
            _require(quantity is None or type(quantity) is int and quantity >= 0)
            first = row["first_fill_ts"]
            _require(first is None or type(first) is int and first > 0)
            if row["vwap"] is not None:
                _number(row["vwap"], positive=True)
            _require(row["remainder_cancel_reason"] is None
                     or isinstance(row["remainder_cancel_reason"], str))
            if quantity:
                _require(first is not None and row["vwap"] is not None)
    # Unsupported/nonfinite nested metadata must not escape JSONResponse later.
    json.dumps(stored, default=_json_default, allow_nan=False)
