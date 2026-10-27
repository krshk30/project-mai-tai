"""Finite Main-only original122 retained-row materialization, never a BUY proof.

No engine/role/lease creation, writes, timeless restore loaders or object defaults.
Main supplies its already held private factory and accepted full-source reader.
The returned operands are for reconstruction; they are not historical RAM images.
"""

from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
from zoneinfo import ZoneInfo

from sqlalchemy import Float, JSON, Numeric, String, and_, cast, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB

from tools.l12_aa87_original122_ledger import EXPORT_SHA, GROUPS, index_rows
from tools.l12_original122_embedded_identity import _extract
from tools.l12_original122_branch_adapter import _references
from tools.l12_scoped_chunks_v2 import (
    JSON_TEXTS, NULL_FLAG, SQL_NULLS, TABLES, TEXT_FLAG,
    canonical, encode_source_row, read_part,
)
from tools.l12_close_export_restore import typed_value, normalized

VERSION = "ORIGINAL122_RETAINED_ROWS_AND_READBACK_V1"
SNAPSHOTS = (
    "v2_fanout_segment_identity", "v2_flip_entry_ownership", "v2_retry_one_budget",
    "v2_falseflip_budget", "v2_falseflip_entry_bar", "v2_removed_wait",
    "v2_wait_dispatch", "v2_restart_rest",
)
TABLE_NAMES = ("dashboard_snapshots", "strategy_bar_history", "broker_orders",
               "trade_intents", "fills", "broker_order_events", "broker_accounts", "strategies")
NY = ZoneInfo("America/New_York")


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("explicit_offset_clock_required")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("explicit_offset_clock_required")
    return result.astimezone(UTC)


def _case_plan(indexed, extracted, clock_reference):
    row = indexed["original_record"]
    minute = datetime.fromisoformat(row["day"] + "T" + row["flip"]).replace(tzinfo=NY)
    anchor = minute.replace(hour=4, minute=0, second=0, microsecond=0)
    if not anchor <= minute < anchor + timedelta(hours=16):
        raise ValueError("original_flip_outside_recorded_session")
    start, end = map(_stamp, extracted["log_window_utc"])
    selected, clock_members = minute.astimezone(UTC), []
    precision = "RECORDED_FLIP_MINUTE_PREFIX_NOT_EXACT_CALLBACK"
    if clock_reference is not None:
        # An exact member clock is not itself a SAME-opportunity proof.
        clock_members = _references(extracted, [clock_reference])
        fact = clock_members[0]["fact"]
        if (not fact.get("source_symbol_matches") or fact.get("duplicate_field_names")
                or fact.get("tag") != "V2-FLIP-OWNER-ADMISSION"
                or fact.get("fields", {}).get("slot") != "first"
                or not fact.get("within_declared_log_window")):
            raise ValueError("exact_original_first_admission_clock_reference_required")
        selected = _stamp(fact["recorded_utc"])
        if not start <= selected <= end:
            raise ValueError("referenced_clock_outside_original_case_window")
        precision = "EXPLICIT_RECORDED_MEMBER_CLOCK_CASE_LINK_STILL_REQUIRES_REVIEW"
    if not anchor.astimezone(UTC) <= selected < (anchor + timedelta(hours=16)).astimezone(UTC):
        raise ValueError("selected_clock_crosses_original_session")
    return dict(case_id=indexed["case_id"], symbol=row["sym"],
        original_record_sha256=indexed["canonical_record_sha256"],
        original_pointer=indexed["source_json_pointer"],
        source_sha256=indexed["source_sha256"],
        recorded_flip_minute=minute.isoformat(), selection_clock=selected.isoformat(),
        clock_precision=precision, clock_references=[dict(source_pointer=m["source_pointer"],
            decoded_text_sha256=m["decoded_text_sha256"]) for m in clock_members],
        session_anchor=anchor.astimezone(UTC).isoformat(),
        session_end=(anchor + timedelta(hours=16)).astimezone(UTC).isoformat(),
        same_entry_opportunity="UNKNOWN_UNTIL_EXACT_LOCAL_LINEAGE_BINDING")


def original122_retained_selection_plan(original_raw, *, clock_references):
    indexed = index_rows(original_raw, expected_sha=EXPORT_SHA, expected_groups=GROUPS)
    extracted = _extract(indexed)
    if (type(clock_references) is not dict
            or set(clock_references) - {row["case_id"] for row in indexed}):
        raise ValueError("no_foreign_or_extension_case_clock")
    plans = [_case_plan(row, case, clock_references.get(row["case_id"]))
        for row, case in zip(indexed, extracted["cases"])]
    return dict(version=VERSION, original_cases=122, omitted_cases=0,
        source_sha256=EXPORT_SHA, cases=plans, permission_granted=False)


def selection_rules(case):
    """Closed eight-query predicate vocabulary shared by SQL and file sizing."""
    symbol = case["symbol"]
    anchor, end = _stamp(case["session_anchor"]), _stamp(case["session_end"])
    return (
        ("and", ("in", "snapshot_type", SNAPSHOTS),
            ("or", ("json_symbol", symbol), ("and", ("eq", "snapshot_type", "v2_restart_rest"), ("json_restart_symbol", symbol))),
            ("lt", "created_at", end), ("or", ("in", "snapshot_type", ("v2_removed_wait", "v2_wait_dispatch")), ("ge", "created_at", anchor))),
        ("and", ("eq", "strategy_code", "schwab_1m_v2"), ("eq", "symbol", symbol),
            ("eq", "interval_secs", 60), ("ge", "bar_time", anchor), ("lt", "bar_time", end)),
        ("and", ("eq", "symbol", symbol), ("or", ("lt", "submitted_at", end), ("null", "submitted_at"))),
        ("and", ("eq", "symbol", symbol), ("lt", "created_at", end)),
        ("and", ("eq", "symbol", symbol), ("lt", "filled_at", end)),
        ("and", ("order_parent_symbol", symbol), ("lt", "event_at", end)),
        ("true",), ("true",),
    )


def _sql_selection_rule(table, rule):
    op, *args = rule
    if op in {"and", "or"}:
        return (and_ if op == "and" else or_)(*(_sql_selection_rule(table, r) for r in args))
    if op == "true":
        return text("true")
    if op == "json_symbol":
        return table.c.payload["symbol"].as_string() == args[0]
    if op == "json_restart_symbol":
        return cast(table.c.payload, JSONB)["symbols"].has_key(args[0])
    if op == "order_parent_symbol":
        orders = TABLES["broker_orders"]
        return table.c.order_id.in_(select(orders.c.id).where(orders.c.symbol == args[0]))
    column = table.c[args[0]]
    if op == "null":
        return column.is_(None)
    if op == "eq":
        return column == args[1]
    if op == "in":
        return column.in_(args[1])
    if op == "lt":
        return column < args[1]
    if op == "ge":
        return column >= args[1]
    raise ValueError("unreviewed_original122_selection_rule")


def source_selection_matches(rule, row, *, parent_symbols):
    """Evaluate only this closed WHERE vocabulary on validated source rows.

    SQL NULL comparisons are not True and therefore do not select a row. JSON
    symbol matching cannot turn a non-string JSON value into a case symbol.
    """
    op, *args = rule
    if op in {"and", "or"}:
        values = (source_selection_matches(r, row, parent_symbols=parent_symbols) for r in args)
        return all(values) if op == "and" else any(values)
    if op == "true":
        return True
    if op in {"json_symbol", "json_restart_symbol"}:
        payload = row["payload"]
        if type(payload) is not dict:
            return False
        if op == "json_symbol":
            if payload.get("symbol") is not None and type(payload["symbol"]) is not str:
                raise ValueError("unreviewed_nonstring_JSON_symbol_text_STOP")
            return type(payload.get("symbol")) is str and payload["symbol"] == args[0]
        symbols = payload.get("symbols")
        return ((type(symbols) is dict and args[0] in symbols)
            or (type(symbols) is list and args[0] in symbols)
            or (type(symbols) is str and args[0] == symbols))
    if op == "order_parent_symbol":
        return parent_symbols.get(row["order_id"]) == args[0]
    value = row[args[0]]
    if op == "null":
        return value is None
    if value is None:
        return False
    if op == "eq":
        return value == args[1]
    if op == "in":
        return value in args[1]
    if op in {"lt", "ge"}:
        return _stamp(value) < args[1] if op == "lt" else _stamp(value) >= args[1]
    raise ValueError("unreviewed_original122_selection_rule")


def selection_queries(case, *, maximum_rows):
    """Eight bounded SELECTs, all accounts; future/mutated rows remain context."""
    if type(maximum_rows) is not int or not 1 <= maximum_rows <= 4096:
        raise ValueError("explicit_at_most_4096_rows_per_query_required")
    queries = []
    for name, rule in zip(TABLE_NAMES, selection_rules(case)):
        table = TABLES[name]
        columns = list(table.columns)
        columns += [col.is_(None).label(NULL_FLAG + col.name)
                    for col in table.columns if isinstance(col.type, JSON)]
        columns += [cast(col, String).label(TEXT_FLAG + col.name)
                    for col in table.columns if isinstance(col.type, JSON)]
        statement = select(*columns).where(_sql_selection_rule(table, rule)).order_by(*table.primary_key.columns).limit(maximum_rows + 1)
        queries.append((name, statement))
    return queries


def _read_encoded(session, name, query, *, maximum_rows, byte_budget, held):
    result, primary, rows, size = None, None, [], 0
    try:
        result = session.execute(query.execution_options(stream_results=True, yield_per=1))
        for value in result.mappings():
            held()
            if len(rows) >= maximum_rows:
                raise ValueError("case_row_bound_exceeded_no_truncation")
            raw = canonical(encode_source_row(name, dict(value)))
            size += len(raw)
            if size > byte_budget:
                raise ValueError("finite_materialization_bytes_exceeded_no_partial_success")
            rows.append(json.loads(raw))
        return rows
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if result is not None:
            try:
                result.close()
            except BaseException as cleanup:
                if primary is None:
                    raise
                primary.add_note("retained_cursor_cleanup_type=" + type(cleanup).__name__)


def classify_retained_row(table, row, case):
    """Classify source chronology, never infer historical absence from a query."""
    at = _stamp(case["selection_clock"])
    if table in {"broker_accounts", "strategies"}:
        return "RETAINED_IDENTITY_MAPPING_NOT_HISTORICAL_NATIVE_PERMISSION"
    if table == "dashboard_snapshots":
        return "RECORDED_PAYLOAD_AT_OR_BEFORE_CUTOFF" if _stamp(row["created_at"]) <= at else "LATER_ONLY_CONTEXT"
    if table in {"trade_intents", "broker_orders", "strategy_bar_history"}:
        born = row.get("submitted_at") if table == "broker_orders" else row["created_at"]
        if born is None:
            return "CREATION_CLOCK_UNKNOWN"
        if _stamp(born) > at:
            return "LATER_ONLY_CONTEXT"
        if _stamp(row["updated_at"]) > at:
            return "MUTATED_AFTER_CUTOFF_NOT_HISTORICAL_PAYLOAD"
        if table == "strategy_bar_history" and _stamp(row["bar_time"]) + timedelta(minutes=1) > at:
            return "BAR_NOT_CLOSED_AT_CUTOFF"
        return "RETAINED_PAYLOAD_AT_OR_BEFORE_CUTOFF_NOT_RAM_PROOF"
    stamp = row["filled_at"] if table == "fills" else row["event_at"]
    return ("EVENT_TIME_AT_OR_BEFORE_CUTOFF_ARRIVAL_UNKNOWN" if _stamp(stamp) <= at
            else "LATER_ONLY_CONTEXT")


class RetainedRowMismatch(ValueError):
    """Same STOP, with a bounded value-free witness for Main's diagnostic claim."""

    def __init__(self, witness):
        super().__init__("restored_row_differs_from_immutable_source_row")
        self.witness = witness


def retained_numeric_comparison(table, source, restored):
    """C12119: exact declared Numeric values only; all other bytes remain strict.

    Compare Decimal values before normalized(): Decimal.normalize can be affected
    by context precision, which must never hide a genuine value difference.
    Neither input, the source row hash nor the stored raw JSON text is modified.
    """
    if set(source) != set(restored):
        return False, []
    before, after, equivalent = dict(source), dict(restored), []
    for column in TABLES[table].columns:
        if (not isinstance(column.type, Numeric) or isinstance(column.type, Float)
                or column.name not in source):
            continue
        left, right = source[column.name], restored[column.name]
        if (left is None) != (right is None):
            return False, []
        try:
            typed_left, typed_right = typed_value(column, left), typed_value(column, right)
        except (ValueError, TypeError, ArithmeticError):
            return False, []
        if typed_left != typed_right:
            return False, []
        before[column.name] = normalized(typed_left)
        after[column.name] = normalized(typed_right)
        if canonical(left) != canonical(right):
            equivalent.append(column.name)
    accepted = canonical(before) == canonical(after)
    return accepted, sorted(equivalent) if accepted else []


def retained_row_mismatch_witness(table, primary_key, source, restored, descriptor, ordinal):
    """Only declared field names, types and canonical digests; never raw values."""
    declared = {col.name for col in TABLES[table].columns} | {SQL_NULLS, JSON_TEXTS}
    differences = []
    for name in sorted(declared):
        present_before, present_after = name in source, name in restored
        before = canonical(source[name]) if present_before else None
        after = canonical(restored[name]) if present_after else None
        if before != after:
            differences.append(dict(field=name, source_present=present_before, restored_present=present_after,
                source_type=type(source[name]).__name__ if present_before else None,
                restored_type=type(restored[name]).__name__ if present_after else None,
                source_sha256=sha256(before).hexdigest() if before is not None else None,
                restored_sha256=sha256(after).hexdigest() if after is not None else None))
    return dict(version="RETAINED_ROW_MISMATCH_VALUE_FREE_V1", table=table,
        primary_key=list(primary_key), part_sha256=descriptor["sha256"], row_ordinal_1based=ordinal,
        source_row_sha256=sha256(canonical(source)).hexdigest(),
        restored_row_sha256=sha256(canonical(restored)).hexdigest(), fields=differences,
        undeclared_field_names_differ=(set(source) - declared != set(restored) - declared),
        acceptance="STOP_NO_DIFFERENCES_ACCEPTED", raw_values_included=False)


def _source_reference(table, row, manifest, reader, cache):
    key = tuple(row[col.name] for col in TABLES[table].primary_key.columns)
    # These eight source tables all have an id PK. Bounds are from the already
    # validated full-PK manifest, not a new observed-hash allowlist or SQL join.
    if len(key) != 1 or type(key[0]) is not str:
        raise ValueError("literal_eight_table_id_PK_required")
    indices = cache.setdefault("indices", {})
    if table not in indices:
        descriptors = manifest["tables"][table]["chunks"]
        indices[table] = (descriptors, [tuple(d["last_key"]) for d in descriptors])
    descriptors, last_keys = indices[table]
    position = bisect_left(last_keys, key)
    if position >= len(descriptors) or not tuple(descriptors[position]["first_key"]) <= key <= last_keys[position]:
        raise ValueError("restored_PK_not_in_accepted_source_ranges")
    descriptor = descriptors[position]
    if cache.get("reference") != descriptor["reference"]:
        cache.pop("part", None)  # At most one32MiB source part plus bound indices.
        cache.update(reference=descriptor["reference"],
            part=read_part(reader, descriptor, limit=manifest["limits"]["chunk_bytes"]))
    part = cache["part"]
    ordinals = [i + 1 for i, candidate in enumerate(part["rows"])
                if tuple(candidate[col.name] for col in TABLES[table].primary_key.columns) == key]
    if part.get("table") != table or len(ordinals) != 1:
        raise ValueError("source_PK_or_table_mismatch")
    ordinal = ordinals[0]
    source = part["rows"][ordinal - 1]
    equivalent_columns = []
    if canonical(source) != canonical(row):
        equivalent, equivalent_columns = retained_numeric_comparison(table, source, row)
    else:
        equivalent = True
    if not equivalent:
        try:
            witness = retained_row_mismatch_witness(table, key, source, row, descriptor, ordinal)
        except Exception as diagnostic_failure:
            witness = dict(acceptance="STOP_NO_DIFFERENCES_ACCEPTED",
                witness_failure_type=type(diagnostic_failure).__name__, raw_values_included=False)
        raise RetainedRowMismatch(witness)
    return dict(table=table, primary_key=list(key), part_reference=descriptor["reference"],
        part_sha256=descriptor["sha256"], row_ordinal_1based=ordinal,
        source_row_sha256=sha256(canonical(source)).hexdigest(),
        restored_row_sha256=sha256(canonical(row)).hexdigest(),
        comparison_profile="C12119_DECLARED_PG_NUMERIC_EXACT_DECIMAL_OTHER_FIELDS_STRICT",
        numeric_equivalent_columns=equivalent_columns,
        payload_text_sha256={k: sha256(v.encode()).hexdigest() if v is not None else None
            for k, v in row[JSON_TEXTS].items()}, sql_null_columns=row[SQL_NULLS])


def materialize_retained_original122(original_raw, *, factory, target_capability,
        source_manifest_raw, source_plan, source_reader,
        clock_references, read_guard, held_shared_read_lock,
        maximum_rows_per_query, maximum_total_bytes):
    """Main executes ONE RR/RO readback; no commits or real objects inside here.

    read_guard must independently bind the actual copy/source/held claim and
    immutable manifest. A UNIT lambda is not runtime acceptance.
    An overflow/readback/source mismatch refuses the batch; no truncation/retry.
    """
    from sqlalchemy.orm import sessionmaker
    from tools.l12_private_full23_lifecycle import require_private_factory, verify_private_session
    if (type(maximum_total_bytes) is not int or not 1 <= maximum_total_bytes <= 64 * 1024 * 1024
            or not callable(read_guard) or not callable(source_reader)
            or not callable(held_shared_read_lock)):
        raise ValueError("finite_existing_private_read_and_source_operands_required")
    if type(factory) is not sessionmaker:
        raise ValueError("explicit_native_capability_readonly_sessionmaker_required")
    if sha256(source_manifest_raw).hexdigest() != source_plan["manifest_sha256"]:
        raise ValueError("actual_validated_manifest_bytes_required")
    manifest = json.loads(source_manifest_raw)
    plan = original122_retained_selection_plan(original_raw, clock_references=clock_references)
    for case in plan["cases"]:
        selection_queries(case, maximum_rows=maximum_rows_per_query)
    require_private_factory(factory, target_capability=target_capability)
    session, primary, output, size, cache = None, None, [], 0, {}

    def guard():
        if (held_shared_read_lock() is not True
                or read_guard(factory, target_capability, source_plan) is not True):
            raise ValueError("actual_copy_source_or_held_read_claim_UNKNOWN")

    def held():
        if held_shared_read_lock() is not True:
            raise ValueError("original_close_shared_read_exclusion_lost")

    try:
        guard()
        session = factory()
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        verify_private_session(session, target_capability=target_capability)
        for case in plan["cases"]:
            guard()
            streams, readback, case_bytes = {}, {}, 0
            for name, query in selection_queries(case, maximum_rows=maximum_rows_per_query):
                guard()
                rows = _read_encoded(session, name, query, maximum_rows=maximum_rows_per_query,
                    byte_budget=maximum_total_bytes - size - case_bytes,
                    held=held)
                readback[name] = sha256(canonical(rows)).hexdigest()
                streams[name] = []
                for row in rows:
                    held()
                    reference = _source_reference(name, row, manifest, source_reader, cache)
                    item = dict(row=row, reference=reference,
                        chronology=classify_retained_row(name, row, case))
                    case_bytes += len(canonical(item))
                    if size + case_bytes > maximum_total_bytes:
                        raise ValueError("finite_materialization_bytes_exceeded_no_partial_success")
                    streams[name].append(item)
            # Reselect every query in the same RR snapshot; reference hashes
            # remain attached to the first exact payload, never latest state.
            for name, query in selection_queries(case, maximum_rows=maximum_rows_per_query):
                guard()
                rows = _read_encoded(session, name, query, maximum_rows=maximum_rows_per_query,
                    byte_budget=maximum_total_bytes - size,
                    held=held)
                if sha256(canonical(rows)).hexdigest() != readback[name]:
                    raise ValueError("case_materialization_readback_changed")
            operands = materialized_case_operands(case, streams)
            record = dict(case=case, rows=streams, readback_sha256=sha256(canonical(readback)).hexdigest(),
                operands=operands, real_objects_constructed=False, permission_granted=False)
            size += len(canonical(record))
            if size > maximum_total_bytes:
                raise ValueError("finite_materialization_bytes_exceeded_no_partial_success")
            output.append(record)
        guard()
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup_failure = None
        if session is not None:
            for cleanup in (session.rollback, session.close):
                try:
                    cleanup()
                except BaseException as exc:
                    if primary is not None:
                        primary.add_note("retained_read_cleanup_type=" + type(exc).__name__)
                    elif cleanup_failure is None:
                        cleanup_failure = exc
        if primary is None and cleanup_failure is not None:
            raise cleanup_failure
    return dict(version=VERSION, original_cases=122, omitted_cases=0, cases=output,
        source_sha256=EXPORT_SHA, source_manifest_sha256=source_plan["manifest_sha256"],
        readback="ALL_EIGHT_STREAMS_RESELECTED_IN_SAME_RR_RO_THEN_ROLLBACK",
        retained_bytes=size, real_objects_constructed=False,
        historical_native_permission="UNKNOWN_NOT_STORED", permission_granted=False)


def materialized_case_operands(case, streams):
    """Expose positive recorded fields with their refs; no absent-record zeroes."""
    known, missing, snapshots = {}, [], {}
    decoder_inputs = {kind: [] for kind in SNAPSHOTS}
    for item in streams["dashboard_snapshots"]:
        row = item["row"]
        if item["chronology"] != "RECORDED_PAYLOAD_AT_OR_BEFORE_CUTOFF":
            continue
        kind = row["snapshot_type"]
        if kind not in decoder_inputs:
            raise ValueError("unreviewed_snapshot_type_in_materialization")
        decoder_inputs[kind].append(item)
        previous = snapshots.get(kind)
        if previous is None or (_stamp(row["created_at"]), row["id"]) > (
                _stamp(previous["row"]["created_at"]), previous["row"]["id"]):
            snapshots[kind] = item
    # Exact raw payloads remain available, including inactive records. A latest
    # inactive owner is NOT synthesized into idle/flat/no-send state.
    for kind in SNAPSHOTS:
        decoder_inputs[kind].sort(key=lambda item: (_stamp(item["row"]["created_at"]), item["row"]["id"]))
        if kind in snapshots:
            item = snapshots[kind]
            known[kind] = dict(payload=item["row"]["payload"], reference=item["reference"])
        else:
            missing.append(kind + ":NO_PRE_CUTOFF_RECORDED_PAYLOAD")
    if case["clock_precision"] == "RECORDED_FLIP_MINUTE_PREFIX_NOT_EXACT_CALLBACK":
        missing.append("EXACT_CALLBACK_CLOCK_AND_CASE_LINK_NOT_BOUND")
    missing.extend(("CAUSAL_LINE_AND_COVERAGE_NOT_YET_REPLAYED",
        "OWNER_RETRY_FALSEFLIP_REMOVED_WAIT_REAL_DECODERS_NOT_YET_APPLIED",
        "POSITION_AND_DISPATCH_HISTORICAL_NEGATIVE_EVIDENCE_NOT_ESTABLISHED"))
    fields = {}
    restart = known.get("v2_restart_rest")
    if restart is not None:
        payload = restart["payload"]
        if (type(payload) is dict and payload.get("strategy_code") == "schwab_1m_v2"
                and type(payload.get("schema_version")) is int and payload["schema_version"] == 1):
            symbols = payload.get("symbols")
            saved = symbols.get(case["symbol"], {}) if type(symbols) is dict else {}
            state = saved.get("state", {}) if type(saved) is dict else {}
            if type(state) is dict:
                for name in ("cw_resting_taken", "cw_reclaim_taken"):
                    if name in state and type(state[name]) is bool:
                        fields[name] = dict(value=state[name], reference=restart["reference"],
                            payload_pointer="/symbols/" + case["symbol"] + "/state/" + name,
                            provenance="RECORDED_CHECKPOINT_VALUE_NOT_COMPLETE_HISTORICAL_RAM")
                rest = state.get("rest", {})
                if type(rest) is dict:
                    for name in ("resting_active", "webull_resting_active"):
                        if name in rest and type(rest[name]) is bool:
                            fields[name] = dict(value=rest[name], reference=restart["reference"],
                                payload_pointer="/symbols/" + case["symbol"] + "/state/rest/" + name,
                                provenance="RECORDED_CHECKPOINT_VALUE_NOT_COMPLETE_HISTORICAL_RAM")
    return dict(latest_recorded_context_only=known,
        decoder_inputs_by_snapshot_type=decoder_inputs,
        recorded_state_field_candidates=fields, missing_operands=missing,
        latest_context_is_not_retry_or_owner_decoder_output=True,
        real_object_construction="REQUIRES_MAIN_RECONSTRUCTION_AND_FIELD_READBACK",
        local_predicate="UNRUN", native_permission="UNKNOWN", absence_is_flat=False)
