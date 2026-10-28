"""Bounded, no-I/O decoding of already materialized original122 snapshot facts.

This is not a historical store loader or a permission/complete-state binder.
The real store schemas and typed records are used without their unbounded SQL.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import math

from tools.l12_original122_retained_materialization import classify_retained_row, EXPORT_SHA
from tools.l12_original122_branch_adapter import STATE_READBACK_FIELDS, STRATEGY_READBACK_FIELDS


KINDS = (
    "v2_fanout_segment_identity", "v2_flip_entry_ownership", "v2_retry_one_budget",
    "v2_falseflip_budget", "v2_falseflip_entry_bar", "v2_removed_wait",
    "v2_wait_dispatch", "v2_restart_rest",
)
OWNER_FIELDS = (
    "opportunity_id", "phase", "flip_bar_ts", "provisional_started_ms",
    "fill_accounts", "position_ids", "position_entry_ms", "retry_segment_id",
    "retry_closes_at_place",
)


@dataclass
class DecodedCase:
    case_id: str
    typed: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    missing: dict = field(default_factory=dict)
    excluded: list = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    real_objects_constructed: bool = False
    permission_granted: bool = False
    native_permission: str = "UNKNOWN_NOT_STORED"


def _time(value):
    if type(value) is not str:
        raise ValueError("recorded_offset_clock_required")
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("recorded_offset_clock_required")
    return stamp.astimezone(UTC)


def _integer(value, *, minimum=0):
    if type(value) is bool or type(value) not in (int, str):
        raise ValueError("explicit_integer_required_no_bool_or_float")
    if type(value) is str and (not value or not value.isascii() or not value.isdecimal()):
        raise ValueError("canonical_nonnegative_integer_required")
    number = int(value)
    if number < minimum:
        raise ValueError("integer_outside_schema_range")
    return number


def _required(payload, names):
    absent = set(names) - payload.keys()
    if absent:
        raise ValueError("missing_explicit_fields:" + ",".join(sorted(absent)))


def _strings(value):
    if (type(value) is not list or any(type(v) is not str or not v for v in value)
            or len(value) != len(set(value))):
        raise ValueError("explicit_unique_string_array_required")
    return tuple(sorted(value))


def _mapping(value, *, integer=False):
    if type(value) is not dict or any(type(k) is not str or not k for k in value):
        raise ValueError("explicit_account_mapping_required")
    if integer:
        return {k: _integer(v) for k, v in value.items()}
    if any(type(v) is not str or not v for v in value.values()):
        raise ValueError("explicit_nonempty_mapping_values_required")
    return dict(value)


def _provenance(item):
    row = item["row"]
    return dict(reference=deepcopy(item["reference"]), row_id=row["id"],
        recorded_at=row["created_at"], snapshot_type=row["snapshot_type"],
        scope="RECORDED_SNAPSHOT_FACT_NOT_COMPLETE_HISTORICAL_RAM")


def _put(result, name, value, item, pointer):
    result.fields[name] = dict(value=deepcopy(value), payload_pointer=pointer,
        **_provenance(item))


def _payload(item, symbol, *, strategy=True):
    payload = item["row"]["payload"]
    if type(payload) is not dict:
        raise ValueError("snapshot_payload_object_required")
    if payload.get("symbol") != symbol:
        raise ValueError("exact_case_symbol_required")
    if strategy and payload.get("strategy_code") != "schwab_1m_v2":
        raise ValueError("exact_strategy_required")
    return payload


def _active(payload):
    if type(payload.get("active")) is not bool:
        raise ValueError("explicit_active_boolean_required")
    return payload["active"]


def _owner(payload):
    from project_mai_tai.v2_flip_entry_ownership import ACTIVE_PHASES, FlipEntryOwnershipRecord

    _required(payload, OWNER_FIELDS)
    if payload["schema_version"] != 1 or type(payload["schema_version"]) is not int:
        raise ValueError("owner_schema_required")
    if payload["phase"] not in ACTIVE_PHASES:
        raise ValueError("owner_phase_invalid")
    return FlipEntryOwnershipRecord(symbol=payload["symbol"],
        opportunity_id=_integer(payload["opportunity_id"], minimum=1), phase=payload["phase"],
        flip_bar_ts=_integer(payload["flip_bar_ts"]),
        provisional_started_ms=_integer(payload["provisional_started_ms"]),
        fill_accounts=_strings(payload["fill_accounts"]),
        position_ids=_mapping(payload["position_ids"]),
        position_entry_ms=_mapping(payload["position_entry_ms"], integer=True),
        retry_segment_id=_integer(payload["retry_segment_id"]),
        retry_closes_at_place=_integer(payload["retry_closes_at_place"]))


def _decode_kind(result, kind, items, case):
    symbol = case["symbol"]
    latest = items[-1]
    p = latest["row"]["payload"]
    if kind == "v2_restart_rest":
        from project_mai_tai.v2_restart_rest_store import REST_FIELDS

        if (type(p) is not dict or type(p.get("schema_version")) is not int
                or p["schema_version"] != 1 or p.get("strategy_code") != "schwab_1m_v2"
                or _integer(p.get("session_anchor_ms")) != int(_time(case["session_anchor"]).timestamp() * 1000)):
            raise ValueError("restart_schema_or_session_invalid")
        if (type(p.get("cursor")) is not str or type(p.get("symbols")) is not dict
                or symbol not in p["symbols"]):
            raise ValueError("restart_case_symbol_missing")
        saved = p["symbols"][symbol]
        if type(saved) is not dict or type(saved.get("state")) is not dict:
            raise ValueError("restart_symbol_state_required")
        if (type(saved.get("watch_ms")) is not int
                or not int(_time(case["session_anchor"]).timestamp() * 1000) <= saved["watch_ms"]
                    <= int(_time(latest["row"]["created_at"]).timestamp() * 1000)):
            raise ValueError("restart_watch_membership_clock_invalid")
        state = saved["state"]
        _required(state, ("sell_ms", "opportunity_id", "cancel_pending", "rest"))
        for name in ("sell_ms", "opportunity_id"):
            if type(state[name]) is not int or state[name] < 0:
                raise ValueError("restart_exact_integer_required")
        if type(state["cancel_pending"]) is not bool:
            raise ValueError("restart_cancel_pending_explicit_bool_required")
        rest = state["rest"]
        if type(rest) is not dict or set(rest) != set(REST_FIELDS):
            raise ValueError("restart_complete_real_rest_fields_required")
        for name, value in rest.items():
            if name in {"resting_active", "resting_is_broker_order", "webull_resting_active", "resting_buy_frozen"}:
                if type(value) is not bool:
                    raise ValueError("restart_rest_bool_required")
            elif "generation" in name or name in {"resting_slot", "last_resting_placed_slot"}:
                if type(value) is not str:
                    raise ValueError("restart_rest_identity_required")
            elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("restart_rest_number_invalid")
        if rest["resting_slot"] not in {"first", "reclaim"}:
            raise ValueError("restart_slot_invalid")
        if state.get("country_route") is not None:
            # This increment does not establish a country/segment restore proof.
            result.missing["country_route"] = "COUNTRY_ROUTE_CHECKPOINT_REQUIRES_OWN_REAL_DECODER_AND_CASE_LINK"
        result.typed[kind] = saved  # Borrow this case's immutable payload; never accumulate a batch.
        for name in ("cw_resting_taken", "cw_reclaim_taken"):
            value = saved["state"].get(name)
            if type(value) is bool:
                _put(result, name, value, latest, f"/symbols/{symbol}/state/{name}")
            else:
                result.missing[name] = "EXPLICIT_CHECKPOINT_BOOLEAN_MISSING"
        rest = saved["state"].get("rest")
        for name in ("resting_active", "webull_resting_active"):
            if type(rest) is dict and type(rest.get(name)) is bool:
                _put(result, name, rest[name], latest, f"/symbols/{symbol}/state/rest/{name}")
            else:
                result.missing[name] = "EXPLICIT_CHECKPOINT_BOOLEAN_MISSING"
        return
    if kind in {"v2_wait_dispatch", "v2_falseflip_entry_bar"}:
        result.evidence[kind] = items
        result.missing[kind] = "RAW_EVIDENCE_REQUIRES_CAUSAL_DISPATCH_OR_FULL_POSITION_BOOK"
        return
    payloads = [_payload(item, symbol, strategy=kind != "v2_falseflip_budget") for item in items]
    if kind == "v2_fanout_segment_identity":
        for payload in payloads:
            if payload.get("session_anchor") != _time(case["session_anchor"]).isoformat():
                raise ValueError("fanout_session_anchor_mismatch")
            _active(payload)
            if payload["active"]:
                _integer(payload.get("fanout_segment_id"), minimum=1)
        if not _active(p):
            result.missing[kind] = "LATEST_INACTIVE_IS_NOT_IDLE_OR_NO_SEND"
            return
        result.typed[kind] = _integer(p.get("fanout_segment_id"), minimum=1)
        _put(result, "fanout_segment_id", result.typed[kind], latest, "/fanout_segment_id")
    elif kind == "v2_flip_entry_ownership":
        for payload in payloads:
            if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
                raise ValueError("owner_schema_required")
            _active(payload)
            if payload["active"]:
                _owner(payload)
        if not _active(p):
            result.missing[kind] = "LATEST_INACTIVE_IS_NOT_IDLE_OR_NO_SEND"
            return
        value = _owner(p)
        result.typed[kind] = value
        for name in OWNER_FIELDS:
            _put(result, "owner." + name, getattr(value, name), latest, "/" + name)
        for source, target in (("opportunity_id", "flip_owner_opportunity_id"),
                ("phase", "flip_owner_phase"), ("fill_accounts", "flip_owner_fill_accounts"),
                ("position_ids", "flip_owner_position_ids")):
            _put(result, target, getattr(value, source), latest, "/" + source)
    elif kind == "v2_retry_one_budget":
        chosen = None
        for item, payload in zip(items, payloads):
            version = payload.get("schema_version")
            if type(version) is not int or version not in (1, 2):
                raise ValueError("retry_schema_required")
            _required(payload, ("closes_today",) if version == 1 else ("segment_id", "closes_in_segment"))
            segment = 0 if version == 1 else _integer(payload["segment_id"], minimum=1)
            count = _integer(payload["closes_today"] if version == 1 else payload["closes_in_segment"])
            if chosen is None or (segment, count) >= chosen[:2]:
                chosen = (segment, count, item)
        if chosen[0] == 0:
            result.missing[kind] = "V1_DAILY_COUNT_HAS_NO_SELL_SEGMENT_IDENTITY"
            result.evidence[kind] = items
            return
        result.typed[kind] = chosen[:2]
        _put(result, "retry_one_segment_id", chosen[0], chosen[2], "/segment_id")
        _put(result, "retry_one_closes_in_segment", chosen[1], chosen[2], "/closes_in_segment")
    elif kind == "v2_falseflip_budget":
        from project_mai_tai.falseflip1_runtime import FalseBudget

        budgets, refs = {}, {}
        for item, payload in zip(items, payloads):
            _required(payload, ("schema_version", "symbol", "segment", "episodes", "refunded_counts",
                "skipped", "revision", "cancelled", "drafted", "filled_closed"))
            if type(payload["skipped"]) is not bool or type(payload["schema_version"]) is not int:
                raise ValueError("falseflip_explicit_types_required")
            for key in ("segment", "revision"):
                _integer(payload[key], minimum=1 if key == "segment" else 0)
            for key in ("episodes", "refunded_counts", "cancelled", "drafted", "filled_closed"):
                if type(payload[key]) is not list:
                    raise ValueError("falseflip_explicit_array_required")
                for value in payload[key]:
                    _integer(value, minimum=1)
            value = FalseBudget.read(payload)
            key = (value.symbol, value.segment)
            before = budgets.get(key)
            if before is not None and value.revision == before.revision and value != before:
                raise ValueError("falseflip_equal_revision_conflict")
            if before is None or value.revision > before.revision:
                budgets[key], refs[key] = value, _provenance(item)
        result.typed[kind] = budgets
        result.evidence[kind] = refs
    elif kind == "v2_removed_wait":
        from project_mai_tai.v2_removed_wait import RemovedWait

        _required(p, ("schema_version", "account_names", "opportunity_id", "token", "requested_at_ms", "purpose"))
        if type(p["schema_version"]) is not int or p["schema_version"] != 1:
            raise ValueError("removed_wait_schema_required")
        active = _active(p)
        accounts = _strings(p["account_names"])
        if not accounts or type(p["token"]) is not str or not p["token"]:
            raise ValueError("removed_wait_identity_required")
        if p["purpose"] not in {"scanner_removal", "retry_exhausted", "false_flip_restore"}:
            raise ValueError("removed_wait_purpose_invalid")
        value = RemovedWait(symbol, _integer(p["opportunity_id"]), p["token"],
            _integer(p["requested_at_ms"], minimum=1), accounts, p["purpose"])
        if active:
            result.typed[kind] = value
            result.evidence[kind] = _provenance(latest)
        else:
            result.missing[kind] = "INACTIVE_REQUEST_IS_NOT_ECONOMIC_FLAT_OR_NO_SEND"


def decode_materialized_case(record):
    """Return partial typed facts, per-field provenance and explicit missing inputs.

    Main must first validate the materialization execution/source/readback receipt.
    This function repeats temporal and row-identity checks; it cannot mint that receipt.
    """
    case = record["case"]
    result = DecodedCase(case_id=case["case_id"])
    if case["case_id"].split("/")[1] != case["symbol"]:
        raise ValueError("original_case_symbol_mismatch")
    cutoff, anchor, end = (_time(case[k]) for k in ("selection_clock", "session_anchor", "session_end"))
    if not anchor <= cutoff < end:
        raise ValueError("case_clock_outside_original_session")
    items = record["rows"]["dashboard_snapshots"]
    if type(items) is not list or len(items) > 4096:
        raise ValueError("bounded_snapshot_list_required")
    grouped, identities = {kind: [] for kind in KINDS}, set()
    for item in items:
        row = item["row"]
        if row["id"] in identities:
            raise ValueError("duplicate_snapshot_PK")
        identities.add(row["id"])
        if (type(item.get("reference")) is not dict
                or item["reference"].get("primary_key") != [row["id"]]):
            raise ValueError("exact_materialized_PK_reference_required")
        chronology = classify_retained_row("dashboard_snapshots", row, case)
        if chronology != item["chronology"]:
            raise ValueError("materialized_chronology_changed")
        if chronology != "RECORDED_PAYLOAD_AT_OR_BEFORE_CUTOFF":
            result.excluded.append(dict(reason=chronology, **_provenance(item)))
            continue
        kind = row["snapshot_type"]
        if kind not in grouped:
            raise ValueError("unreviewed_snapshot_type")
        if kind not in {"v2_removed_wait", "v2_wait_dispatch"} and _time(row["created_at"]) < anchor:
            result.excluded.append(dict(reason="PRIOR_SESSION_NOT_TRANSFERABLE", **_provenance(item)))
            continue
        grouped[kind].append(item)
    for kind, rows in grouped.items():
        rows.sort(key=lambda item: (_time(item["row"]["created_at"]), item["row"]["id"]))
        if not rows:
            result.missing[kind] = "NO_PRE_CUTOFF_SNAPSHOT_NOT_PROOF_OF_EMPTY_STATE"
            continue
        # Malformed evidence makes this component UNKNOWN without omitting the case.
        component = DecodedCase(case_id=result.case_id)
        try:
            _decode_kind(component, kind, rows, case)
        except (ValueError, TypeError, KeyError, OverflowError):
            result.missing[kind] = "INVALID_OR_INCOMPLETE_EXPLICIT_SCHEMA"
            result.evidence[kind] = rows
            continue
        for name in ("typed", "fields", "missing", "evidence"):
            getattr(result, name).update(getattr(component, name))
    owner = result.typed.get("v2_flip_entry_ownership")
    segment = result.typed.get("v2_fanout_segment_identity")
    if owner is not None and (segment is None or owner.opportunity_id != segment):
        result.missing["owner_active_segment_link"] = "MISSING_OR_MISMATCHED_ACTIVE_SEGMENT_NO_OWNER_RESTORE"
    elif owner is not None:
        result.evidence["owner_active_segment_link"] = dict(
            opportunity_id=owner.opportunity_id, fanout_segment_id=segment,
            owner=result.fields["owner.opportunity_id"], fanout=result.fields["fanout_segment_id"],
            same_case_callback_link="STILL_REQUIRES_ORIGINAL_LOG_AND_LOCAL_LINEAGE_REVIEW")
    result.missing.update({
        "causal_line": "REAL_BAR_INDICATOR_WATCH_SEED_REPLAY_AND_COVERAGE_NOT_YET_BOUND",
        "position_book": "HISTORICAL_LEGS_CLASSIFICATIONS_AND_ARRIVAL_EVIDENCE_NOT_YET_BOUND",
        "dispatch_pending": "HISTORICAL_WIRE_AND_PENDING_CLAIM_STATE_NOT_YET_BOUND",
        "restore_readability": "ROW_PRESENCE_DOES_NOT_PROVE_HISTORICAL_RESTORE_SUCCESS",
        "native_permission": "UNKNOWN_NOT_STORED",
    })
    if case["clock_precision"] == "RECORDED_FLIP_MINUTE_PREFIX_NOT_EXACT_CALLBACK":
        result.missing["callback_clock"] = "EXACT_CALLBACK_AND_CASE_LINK_NOT_BOUND"
    for name in STATE_READBACK_FIELDS:
        if name not in result.fields:
            result.missing["state." + name] = "NO_DIRECT_RECORDED_FIELD_OR_CAUSAL_REPLAY_BINDING"
    for name in STRATEGY_READBACK_FIELDS:
        result.missing["strategy." + name] = "REQUIRES_SETTINGS_OR_EXPLICIT_RESTORE_AND_PENDING_STATE_PROOF"
    return result


def original_log_prefix(extracted_case, case):
    """Preserve exact original pointers/facts; prefix selection is not a state decoder."""
    if extracted_case["case_id"] != case["case_id"]:
        raise ValueError("foreign_original_case_log_members")
    cutoff = _time(case["selection_clock"])
    prefix, context = [], []
    for member in extracted_case["members"]["bot_log_lines"]:
        fact = member["fact"]
        item = deepcopy(member)
        if (not fact.get("source_symbol_matches") or fact.get("duplicate_field_names")
                or not fact.get("within_declared_log_window") or not fact.get("recorded_utc")):
            item["reconstruction_status"] = "CONTEXT_UNBOUND_IDENTITY_OR_CLOCK"
            context.append(item)
        elif _time(fact["recorded_utc"]) > cutoff:
            item["reconstruction_status"] = "LATER_LOG_CONTEXT_NOT_HISTORICAL_STATE"
            context.append(item)
        else:
            item["reconstruction_status"] = "PRE_CUTOFF_EXPLICIT_FACT_NOT_SAME_OPPORTUNITY_PROOF"
            prefix.append(item)
    return dict(case_id=case["case_id"], prefix=prefix, context=context,
        original_order_preserved=True, fields_applied=False, permission_granted=False)


def decode_materialized_original122(materialization):
    """All122-or-refusal decoding after Main's independent execution acceptance."""
    records = materialization["cases"]
    if (materialization.get("source_sha256") != EXPORT_SHA
            or materialization.get("original_cases") != 122
            or materialization.get("omitted_cases") != 0
            or type(records) is not list or len(records) != 122
            or len({record["case"]["case_id"] for record in records}) != 122
            or materialization.get("permission_granted") is not False
            or materialization.get("real_objects_constructed") is not False):
        raise ValueError("complete_original122_materialization_required")
    for record in records:
        if (record["case"].get("source_sha256") != EXPORT_SHA
                or record.get("permission_granted") is not False
                or record.get("real_objects_constructed") is not False):
            raise ValueError("per_case_original_source_and_nonpermission_status_required")
    return [decode_materialized_case(record) for record in records]
