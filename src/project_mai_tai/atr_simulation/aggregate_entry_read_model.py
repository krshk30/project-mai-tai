"""Pure aggregate-entry read projection; no store, engine or application IO.

Main prepares an external receipt binding once, then passes detached run/row
payloads from its read port. Stored source metadata is never its own authority.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from zoneinfo import ZoneInfo

from .aggregate_entry_contract import LABEL, MODE, MODEL, VERSION
from .aggregate_entry_display import CONTRACT, MAX_ROWS
from .aggregate_entry_publication import (
    CONFIRMATION_ADAPTER_VERSION, CONFIRMATION_POLICY_VERSION, INDEPENDENT_COHORT_POLICY, amount, checksum,
    digest, document, encode, prepare_aggregate_publication, require,
)


READ_VERSION = "ATRSIM_AGGREGATE_BOUND_READ_V3_CONFIRMATION_EXIT"
FIRST_DAY = date(2026, 9, 22)
LAST_DAY = date(2026, 10, 23)
ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class AggregateReadBinding:
    """Prepared by trusted Main, never constructed from a DB run's own pins.

    Bytes prevent an otherwise frozen object's nested expected rows from being
    modified by the caller of the display projection.
    """

    source_id: str
    receipt_sha256: str
    publication_raw: bytes
    publication_sha256: str


def prepare_aggregate_read_binding(receipt_raw, *, receipt_source, producer,
                                   expected_receipt_sha256, source_id):
    """Validate actual receipt bytes and external head/exact17 before any read.

    Does not inspect the producer filesystem or attest that its files currently
    match. Main supplies independently reviewed pins and the artifact descriptor.
    Reuses metadata validation only, not the producer/model or writer execution.
    """
    checksum(expected_receipt_sha256)
    require(type(receipt_raw) is bytes
            and hashlib.sha256(receipt_raw).hexdigest() == expected_receipt_sha256,
            "aggregate_read_external_receipt_sha_mismatch")
    require(source_id == "aggregate-entry:" + expected_receipt_sha256,
            "aggregate_read_external_source_id_required")
    prepared = prepare_aggregate_publication(receipt_raw, receipt_source=receipt_source,
                                             producer=producer)
    payload = document(prepared.publication_raw)
    study = payload["coverage"]["aggregate_entry_study"]
    require(study.get("source_adapter_version") == CONFIRMATION_ADAPTER_VERSION
            and study.get("baseline_policy_version") == CONFIRMATION_POLICY_VERSION
            and study.get("cohort_policy") == INDEPENDENT_COHORT_POLICY
            and study.get("same_account_overlap_informational") is True,
            "aggregate_read_confirmation_v3_receipt_required")
    require(prepared.receipt_sha256 == expected_receipt_sha256
            and payload["source_id"] == source_id,
            "aggregate_read_external_publication_mismatch")
    return AggregateReadBinding(source_id, expected_receipt_sha256,
                                prepared.publication_raw, prepared.publication_sha256)


def _window(start, end):
    require(type(start) is str and type(end) is str, "aggregate_read_iso_dates_required")
    left, right = date.fromisoformat(start), date.fromisoformat(end)
    require(left.isoformat() == start and right.isoformat() == end
            and FIRST_DAY <= left <= right <= LAST_DAY
            and (right - left).days < 32, "aggregate_read_bounded32_day_window_required")
    return left, right


def _time(ns):
    if ns is None:
        return "UNKNOWN"
    require(type(ns) is int and 0 < ns <= 2**63 - 1, "aggregate_read_integer_event_clock_required")
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        seconds=ns // 1_000_000_000, microseconds=(ns % 1_000_000_000) // 1000)
    return instant.astimezone(ET).isoformat(timespec="seconds")


def _money(value):
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):+,.2f}"


def _row(row):
    known = row["decision"] == "MODELED_BASELINE_CLOSED"
    pnl = amount(row["gross_pnl"]) if known else None
    quantity = row["filled_qty"]
    vwap = amount(row["vwap"]) if row["vwap"] is not None else None
    exits = row["exits"]
    closed = known and len(exits) == 1 and exits[0]["at"] is not None
    exit = exits[0] if closed else None
    flags = row["flags"]
    summary = " / ".join([*(item["reason"] for item in exits), *row["issues"], *flags])
    return dict(ticker=row["identity"]["symbol"],
        path=row["identity"]["broker"] + ":" + row["identity"]["account_id"],
        quantity=str(quantity), entry_time=_time(row["first_fill_ts"]),
        entry_price="$" + str(vwap) if vwap is not None else "UNKNOWN",
        exit_time=_time(exit["at"]["event_ns"]) if exit else "UNKNOWN",
        exit_price="$" + str(exit["price"]) if exit else "UNKNOWN",
        pnl=str(pnl) if known else None, pnl_label=_money(pnl) if known else "UNKNOWN",
        pnl_pct_label=f"{pnl / (vwap * quantity) * 100:+.1f}%" if known else "UNKNOWN",
        pnl_label_known=known, summary=(summary or "UNKNOWN") + " / " + LABEL,
        identity=row["identity"], outcome_id=row["outcome_id"], trade_id=row["trade_id"],
        flags=flags, same_account_overlap_order_ids=row["same_account_overlap_order_ids"],
        issues=row["issues"], source_adapter_version=row["source_adapter_version"],
        baseline_policy_version=row["baseline_policy_version"],
        history_label=LABEL, clock_label=row["clock_label"],
        native_execution_qualified=False, partial_fill_timing="UNKNOWN",
        raw_baseline_result=row["raw_baseline_result"])


def aggregate_entry_display_view(stored, *, binding, start, end):
    """Validate/project only the new mode; no legacy schema, query or DB call.

    `stored` is {run, rows, truncated, read_status}; run uses existing repository
    publish fields plus result_sha256 (created_at may also be present). Rows are
    payloads, not wrappers. A complete page must contain the exact requested
    outcome set. A truncated page is a validated subset, never a global total.
    Invalid/foreign evidence raises ValueError; explicit absent storage returns
    an empty UNKNOWN/UNAVAILABLE view, never a zero-valued success.
    """
    left, right = _window(start, end)
    require(isinstance(binding, AggregateReadBinding)
            and type(binding.publication_raw) is bytes
            and len(binding.publication_raw) <= 64 * 1024 * 1024
            and hashlib.sha256(binding.publication_raw).hexdigest() == binding.publication_sha256,
            "aggregate_read_trusted_binding_required")
    expected = document(binding.publication_raw)
    require(expected["source_id"] == binding.source_id
            and expected["input_sha256"] == binding.receipt_sha256,
            "aggregate_read_binding_identity_mismatch")
    require(isinstance(stored, dict) and type(stored.get("truncated")) is bool
            and stored.get("read_status") in {"AVAILABLE", "UNKNOWN", "UNAVAILABLE"}
            and isinstance(stored.get("rows"), list) and len(stored["rows"]) <= MAX_ROWS,
            "aggregate_read_bounded_detached_port_required")
    view = dict(contract=CONTRACT, read_version=READ_VERSION,
        mode=MODE, model=MODEL, version=VERSION, history_label=LABEL,
        source_id=binding.source_id, receipt_sha256=binding.receipt_sha256,
        read_status=stored["read_status"], truncated=stored["truncated"],
        requested_start=start, requested_end=end, rows=[], unknown_count=0,
        known_pnl_label="UNKNOWN", known_page_gross_pnl=None,
        global_pnl_qualified=False, actual_realized_pnl_included=False,
        native_execution_qualified=False, underlying_capture_completeness="UNKNOWN",
        requested_window_completeness_scope="BOUND_CANONICAL_OUTCOME_MEMBERSHIP_ONLY",
        complete_requested_window=False, full_study_totals=None)
    if stored["read_status"] != "AVAILABLE":
        require(stored.get("run") is None and not stored["rows"] and not stored["truncated"],
                "aggregate_unavailable_port_cannot_present_rows_or_run")
        return view
    run = stored.get("run")
    require(isinstance(run, dict), "aggregate_read_available_run_required")
    for field in ("source_id", "input_sha256", "policy_version", "source_refs", "coverage"):
        require(field in run and encode(run[field]) == encode(expected[field]),
                "aggregate_read_stored_run_mismatch:" + field)
    require(run.get("result_sha256") == digest(expected["result_rows"]),
            "aggregate_read_stored_result_sha_mismatch")
    study = expected["coverage"]["aggregate_entry_study"]
    require(study.get("source_adapter_version") == CONFIRMATION_ADAPTER_VERSION
            and study.get("baseline_policy_version") == CONFIRMATION_POLICY_VERSION
            and len(study["selected_cases"]) == 173 and study["canonical_rows"] == 253
            and study.get("same_account_overlap_informational") is True,
            "aggregate_read_full173_253_independent_index_required")
    index = {row["outcome_id"]: row for row in expected["result_rows"]
             if left <= date.fromisoformat(row["identity"]["trading_day"]) <= right}
    seen, projected, subtotal, known = set(), [], Decimal(0), 0
    for row in stored["rows"]:
        require(isinstance(row, dict) and isinstance(row.get("outcome_id"), str),
                "aggregate_read_canonical_payload_required")
        key = row["outcome_id"]
        require(key in index and key not in seen, "aggregate_read_duplicate_foreign_or_outside_window")
        require(encode(row) == encode(index[key]), "aggregate_read_stored_payload_not_bound_receipt")
        seen.add(key)
        projected.append(_row(row))
        if projected[-1]["pnl_label_known"]:
            subtotal += amount(row["gross_pnl"])
            known += 1
    require(stored["truncated"] or seen == set(index),
            "aggregate_read_incomplete_nontruncated_page")
    projected.sort(key=lambda row: (row["identity"]["trading_day"], row["entry_time"], row["outcome_id"]))
    view.update(rows=projected, unknown_count=len(projected)-known,
        known_page_gross_pnl=str(subtotal) if known else None,
        known_pnl_label=_money(subtotal) if known else "UNKNOWN",
        complete_requested_window=not stored["truncated"],
        expected_requested_rows=len(index), returned_rows=len(projected),
        expected_study_cases=173, expected_study_rows=253,
        bound_study_index=study["selected_cases"],
        full_study_totals=dict(scope="FULL_BOUND_STUDY_NOT_PAGE_OR_REALIZED_PNL",
            independent_leg_totals=study["independent_leg_totals"],
            unknown_account_legs=study["unknown_account_legs"],
            global_pnl_qualified=False, native_execution_qualified=False),
        producer_head=study["producer_reviewed_head"], source_pins=study["source_pins"])
    view.update(baseline_policy_version=study["baseline_policy_version"],
                exit_reason_counts=study["exit_reason_counts"])
    return view
