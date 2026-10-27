"""Pure original122 embedded log/order identity extraction, NOT replay.

No files, app imports, provider, DB, clocks or inferred original flip/op join.
Every embedded record is retained. Day-wide order/fill arrays are context;
their presence in a case is not evidence of entry in that case's opportunity.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
from uuid import UUID

from tools.l12_aa87_original122_ledger import EXPORT_SHA, GROUPS, index_rows


VERSION = "ORIGINAL122_EMBEDDED_IDENTITY_V2"
FIELDS = ("bot_log_lines", "orders_symbol_day", "fills_symbol_day")
MAX_MEMBERS, MAX_MEMBER_BYTES = 250000, 1024 * 1024
LOG = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3}) (?:INFO|WARNING|ERROR|DEBUG|CRITICAL) [^|]+ \| (.*)$")
TAG = re.compile(r"^\[([A-Z0-9_-]+)\] (.*)$")
PAIR = re.compile(r"(?<!\S)([a-z][a-z0-9_]*)=([^\s]+)")
IDENTITY_TAGS = {"V2-FLIP-OWNER-OPPORTUNITY": "opportunity_id",
                 "V2-FANOUT-SEGMENT-BOUND": "segment_id",
                 "V2-FANOUT-IDENTITY-PERSISTED": "segment_id"}
SEGMENT_EXPLANATION = (
    " \u2014 polarity: attributed=1 means this leg can be grouped; "
    "unattributed filled legs make the duplicate grade UNEXERCISED"
)


def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("embedded_explicit_utc_offset_required")
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc).isoformat()
    except ValueError:
        raise ValueError("embedded_explicit_utc_offset_required") from None


def _quantity(value):
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise ValueError("embedded_finite_nonnegative_decimal_text_required") from None
    return value  # Source scale/precision retained, never a float.


def _order(text):
    parts = text.split("|")
    if len(parts) != 9 or any(not part for part in parts[:6]) or not parts[-1]:
        raise ValueError("embedded_exact_nine_order_columns_required")
    pk, coid, side, order_type, status, qty, submitted, updated, account = parts
    try:
        if str(UUID(pk)) != pk:
            raise ValueError()
    except ValueError:
        raise ValueError("embedded_canonical_order_pk_required") from None
    if side not in {"buy", "sell"}:
        raise ValueError("embedded_recorded_order_side_required")
    return dict(order_pk=pk, client_order_id=coid, side=side, order_type=order_type,
        recorded_status=status, quantity_text=_quantity(qty),
        submitted_utc=None if submitted in {"", "None", "null"} else _stamp(submitted),
        updated_utc=None if updated in {"", "None", "null"} else _stamp(updated), db_account_name=account,
        native_account_identity="UNKNOWN_NOT_PROVEN_BY_DB_NAME",
        opportunity_binding="UNKNOWN_DAY_WIDE_CONTEXT", status_is_terminal_proof=False)


def _fill(text):
    parts = text.split("|")
    if len(parts) != 5 or any(not part for part in parts):
        raise ValueError("embedded_exact_five_fill_columns_required")
    stamp, side, qty, price, account = parts
    if side not in {"buy", "sell"}:
        raise ValueError("embedded_recorded_fill_side_required")
    return dict(recorded_utc=_stamp(stamp), side=side, quantity_text=_quantity(qty),
        price_text=_quantity(price), db_account_name=account,
        fill_pk="NOT_EXPORTED", order_pk="NOT_EXPORTED", inferred_order_link=False,
        opportunity_binding="UNKNOWN_DAY_WIDE_CONTEXT")


def _log(text, symbol):
    match = LOG.fullmatch(text)
    if match is None:
        return dict(state="UNPARSED_RETAINED_NOT_DISCARDED", opportunity_binding="UNKNOWN")
    try:
        # UTC is the export's log_window_utc declaration, NOT machine local time.
        stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        raise ValueError("embedded_invalid_recorded_log_clock") from None
    tagged = TAG.fullmatch(match[2])
    if tagged is None:
        return dict(state="RECORDED_UNTAGGED", recorded_utc=stamp, opportunity_binding="UNKNOWN")
    tag, body = tagged.groups()
    annotation = None
    if tag == "V2-FANOUT-SEGMENT-BOUND" and body.endswith(SEGMENT_EXPLANATION):
        # The producer repeats attributed=1 in this literal prose suffix.
        # Only this exact source-backed suffix is non-structured commentary.
        body, annotation = body[:-len(SEGMENT_EXPLANATION)], SEGMENT_EXPLANATION
    pairs, duplicates = {}, []
    for key, value in PAIR.findall(body):
        if key in pairs:
            duplicates.append(key)
        pairs[key] = value
    prefix = body.split(" ", 1)[0]
    observed_symbols = [value for value in (pairs.get("symbol"), pairs.get("sym"),
                        prefix if "=" not in prefix else None) if value is not None]
    source_symbol_matches = bool(observed_symbols) and all(value == symbol for value in observed_symbols)
    identity = None
    if tag in IDENTITY_TAGS:
        field = IDENTITY_TAGS[tag]
        value = pairs.get(field)
        if (duplicates or not source_symbol_matches or not isinstance(value, str)
                or re.fullmatch(r"[1-9][0-9]{0,24}", value) is None):
            raise ValueError("embedded_explicit_identity_tag_ambiguous_or_foreign")
        identity = dict(kind=field, value=value, state="EXPLICIT_RECORDED_LOG_ID_ONLY")
    return dict(state="RECORDED_TAG_FIELDS_NOT_EXECUTED", recorded_utc=stamp,
        tag=tag, fields=pairs, duplicate_field_names=duplicates,
        annotation_text=annotation, annotation_is_structured_evidence=False,
        source_symbol_matches=source_symbol_matches, explicit_identity=identity,
        same_entry_opportunity="UNKNOWN_NO_ORIGINAL_FLIP_LINK", opportunity_binding="UNKNOWN")


def _extract(indexed):
    """UNIT seam; public wrapper pins the actual original122 bytes first."""
    output, total = [], 0
    for original in indexed:
        row, members = original["original_record"], {}
        window = row.get("log_window_utc")
        if not isinstance(window, list) or len(window) != 2:
            raise ValueError("embedded_declared_log_window_required")
        starts, ends = _stamp(window[0]), _stamp(window[1])
        if datetime.fromisoformat(starts) > datetime.fromisoformat(ends):
            raise ValueError("embedded_log_window_reversed")
        for field in FIELDS:
            values = row.get(field)
            if not isinstance(values, list) or len(values) > MAX_MEMBERS:
                raise ValueError("embedded_full_array_required_no_truncation")
            total += len(values)
            if total > MAX_MEMBERS:
                raise ValueError("embedded_total_bound_exceeded_no_truncation")
            found, seen_orders = [], set()
            for ordinal, text in enumerate(values):
                if not isinstance(text, str) or not 0 < len(text.encode()) <= MAX_MEMBER_BYTES:
                    raise ValueError("embedded_bounded_raw_record_text_required")
                fact = _log(text, row["sym"]) if field == "bot_log_lines" else (
                    _order(text) if field == "orders_symbol_day" else _fill(text))
                if field == "orders_symbol_day":
                    if fact["order_pk"] in seen_orders:
                        raise ValueError("embedded_duplicate_order_pk_in_one_case")
                    seen_orders.add(fact["order_pk"])
                if field == "bot_log_lines" and "recorded_utc" in fact:
                    fact["within_declared_log_window"] = (
                        datetime.fromisoformat(starts) <= datetime.fromisoformat(fact["recorded_utc"])
                        <= datetime.fromisoformat(ends))
                found.append(dict(source_pointer=f'{original["source_json_pointer"]}/{field}/{ordinal}',
                    member_ordinal_1based=ordinal + 1, raw_text=text,
                    decoded_text_sha256=sha256(text.encode()).hexdigest(),
                    hash_domain="DECODED_UTF8_STRING_NOT_ORIGINAL_JSON_TOKEN_BYTES",
                    fact=fact))
            members[field] = found
        output.append(dict(case_id=original["case_id"], original_pointer=original["source_json_pointer"],
            original_record_sha256=original["canonical_record_sha256"],
            original_source_sha256=original["source_sha256"], log_window_utc=[starts, ends],
            members=members, decision="UNKNOWN", permission_granted=False,
            local_old_new_execution="UNRUN", same_entry_opportunity="UNKNOWN"))
    return dict(version=VERSION, status="EMBEDDED_LOCAL_IDENTITIES_EXTRACTED_NOT_REPLAY",
        original_cases=len(indexed), omitted_original_cases=0, embedded_members=total, cases=output,
        historical_native_permission="UNKNOWN_NOT_STORED", source_payloads_not_rewritten=True,
        extension_owner_trigger_cases="SEPARATE_NOT_JOINED", actual_close_boot="UNRUN",
        permission_granted=False)


def extract_actual_original122_identity(raw):
    indexed = index_rows(raw, expected_sha=EXPORT_SHA, expected_groups=GROUPS)
    return _extract(indexed)
