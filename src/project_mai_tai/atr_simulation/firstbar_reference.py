"""Byte-bound reference metadata, NEVER an executable reference strategy.

This joins study rows, not canonical broker/flip identities. A study signal can
group broker legs; callers must bind those legs separately before engine replay.
Reported-value arithmetic is not an independently reproduced economic result.
"""

import csv
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json

from .types import EvidenceKind, SourceRef


SELECTOR_VERSION = "REPORTED_REAL_EXIT_AND_REFERENCE_LINE_MATCH"
CSV_SHA256 = "6987ae93121fc35dc09d38d099933fd93aa932a5e48245b6dbaab82e17381f25"
SPLIT_SHA256 = "c9bf47cd5d438214a174ee2bbf085c91a00e106a5dc46358d39e31032c890c78"
SELECTOR_SHA256 = "fabb6dc7c453aa078f4cfc69e8f8377a9612709e82e7f65cf8e297355c67ecb0"
KEY_FIELDS = ("sym", "date", "fill_et")


@dataclass(frozen=True)
class ReferenceSeedManifest:
    label: str
    expected_signals: int
    csv_source: SourceRef
    split_source: SourceRef
    selector_source: SourceRef
    selector_version: str = SELECTOR_VERSION


@dataclass(frozen=True)
class ReferenceSeedCase:
    reference_key: tuple[str, str, str]
    csv_source: SourceRef
    x3_source: SourceRef
    csv_fields: tuple[tuple[str, str], ...]
    x3_fields: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class ReferenceExclusion:
    reference_key: tuple[str, str, str]
    source: SourceRef
    reasons: tuple[str, ...]
    fields: tuple[tuple[str, object], ...]


@dataclass(frozen=True)
class ReferenceSeed:
    manifest: ReferenceSeedManifest
    cases: tuple[ReferenceSeedCase, ...]
    reference_exclusions: tuple[ReferenceExclusion, ...]
    provided_a_sum: Decimal
    provided_x3_sum: Decimal
    label: str = "PROVIDED_ROW_ARITHMETIC_NOT_ENGINE_REPLAY"


def reviewed_173_manifest(root: str) -> ReferenceSeedManifest:
    root = root.rstrip("/")
    return ReferenceSeedManifest("replay, in-sample; reviewed CSV173", 173,
        SourceRef(root + "/four_columns.csv", CSV_SHA256, "$", EvidenceKind.RETAINED),
        SourceRef(root + "/four/raw_split.json", SPLIT_SHA256, "$", EvidenceKind.RETAINED),
        SourceRef(root + "/four/agg4.py", SELECTOR_SHA256, "lines4-6", EvidenceKind.RETAINED))


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_reference_json_key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("nonfinite_reference_json_number")


def _key(row):
    values = tuple(row.get(field) for field in KEY_FIELDS)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("reference_identity_fields_required")
    return values


def _amount(value):
    try:
        if type(value) is bool:
            raise ValueError("boolean_reference_amount")
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("numeric_reference_amount_required") from exc
    if not result.is_finite():
        raise ValueError("finite_reference_amount_required")
    return result


def bind_reference_seed(csv_bytes: bytes, split_bytes: bytes, *,
                        manifest: ReferenceSeedManifest) -> ReferenceSeed:
    """Strict source-bound metadata join; no tape processing or strategy call."""
    if (type(manifest.expected_signals) is not int or manifest.expected_signals <= 0
            or manifest.selector_version != SELECTOR_VERSION):
        raise ValueError("explicit_reference_population_contract_required")
    if manifest.selector_source.sha256 != SELECTOR_SHA256:
        raise ValueError("reviewed_reference_selector_source_required")
    for raw, source in ((csv_bytes, manifest.csv_source), (split_bytes, manifest.split_source)):
        if not source.artifact or hashlib.sha256(raw).hexdigest() != source.sha256:
            raise ValueError("reference_input_sha256_mismatch")
    reader = csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"), newline=""), strict=True)
    fields = reader.fieldnames
    required = (*KEY_FIELDS, "sess", "qty", "A_usd")
    if not fields or len(fields) != len(set(fields)) or not set(required).issubset(fields):
        raise ValueError("reference_csv_schema_required")
    numbered_rows = [(reader.line_num, row) for row in reader]
    csv_rows = [row for _, row in numbered_rows]
    if len(csv_rows) != manifest.expected_signals:
        raise ValueError("reference_csv_population_count_mismatch")
    if any(set(row) != set(fields) or any(value is None for value in row.values()) for row in csv_rows):
        raise ValueError("reference_csv_column_count_mismatch")
    csv_keys = tuple(_key(row) for row in csv_rows)
    if len(set(csv_keys)) != len(csv_keys):
        raise ValueError("duplicate_reference_csv_identity")
    split = json.loads(split_bytes.decode("utf-8"), parse_float=Decimal,
                       parse_constant=_nonfinite, object_pairs_hook=_pairs)
    if not isinstance(split, dict) or not isinstance(split.get("x3"), list):
        raise ValueError("reference_x3_array_required")
    indexed, selected, excluded = {}, set(), []
    for index, row in enumerate(split["x3"]):
        if not isinstance(row, dict) or any(isinstance(value, (dict, list)) for value in row.values()):
            raise ValueError("scalar_reference_row_required")
        key = _key(row)
        if key in indexed:
            raise ValueError("duplicate_reference_x3_identity")
        if "real_usd" not in row or type(row.get("line_match")) is not bool:
            raise ValueError("reference_selection_operands_required")
        source = replace(manifest.split_source, pointer=f"$.x3[{index}]")
        indexed[key] = row, source
        reasons = []
        if row["real_usd"] is None:
            reasons.append("REFERENCE_MISSING_REPORTED_REAL_EXIT")
        else:
            _amount(row["real_usd"])
        if not row["line_match"]:
            reasons.append("REFERENCE_LINE_MATCH_FALSE")
        if reasons:
            excluded.append(ReferenceExclusion(key, source, tuple(reasons), tuple(row.items())))
        else:
            selected.add(key)
    if selected != set(csv_keys):
        raise ValueError("reference_selector_csv_membership_mismatch")
    cases, a_sum, x3_sum = [], Decimal(0), Decimal(0)
    for record_number, (key, (end_line, row)) in enumerate(zip(csv_keys, numbered_rows), start=1):
        x3, source = indexed[key]
        if row["sess"] != x3.get("sess") or _amount(row["qty"]) != _amount(x3.get("qty")):
            raise ValueError("reference_join_session_or_quantity_mismatch")
        if row["A_usd"] != "":
            a_sum += _amount(row["A_usd"])
        elif not row.get("fbh_skip"):
            raise ValueError("reference_csv_pnl_missing_without_skip")
        if x3.get("fbh_usd") is not None:
            x3_sum += _amount(x3["fbh_usd"])
        elif not x3.get("fbh_why"):
            raise ValueError("reference_x3_pnl_missing_without_skip")
        cases.append(ReferenceSeedCase(key,
            replace(manifest.csv_source, pointer=f"data-row:{record_number};ending-file-line:{end_line}"),
            source, tuple(row.items()), tuple(x3.items())))
    return ReferenceSeed(manifest, tuple(cases), tuple(excluded), a_sum, x3_sum)
