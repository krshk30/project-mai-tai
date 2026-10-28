"""CONTROLLED metadata parser controls; never reference-script execution."""

from dataclasses import replace
import hashlib
import json

import pytest

from project_mai_tai.atr_simulation.firstbar_reference import (
    ReferenceSeedManifest, SELECTOR_SHA256, bind_reference_seed, reviewed_173_manifest,
    CSV_SHA256, SPLIT_SHA256,
)
from project_mai_tai.atr_simulation.types import EvidenceKind, SourceRef


CSV = ("sym,date,fill_et,sess,qty,A_usd,fbh_skip\n"
       "X,2026-10-23,07:01:00,PM,100,1.10,\n"
       "Y,2026-10-23,10:01:00,RTH,200,,flip closed short\n").encode()


def rows():
    return [
        {"sym": "X", "date": "2026-10-23", "fill_et": "07:01:00", "sess": "PM",
         "qty": 100, "real_usd": 3, "line_match": True, "fbh_usd": 2.20},
        {"sym": "Y", "date": "2026-10-23", "fill_et": "10:01:00", "sess": "RTH",
         "qty": 200, "real_usd": -3, "line_match": True, "fbh_why": "flip closed short"},
        {"sym": "Z", "date": "2026-10-23", "fill_et": "11:01:00", "sess": "RTH",
         "qty": 300, "real_usd": 1, "line_match": False, "fbh_usd": -8},
    ]


def operands(values=None, csv_bytes=CSV, raw=None):
    split_bytes = raw if raw is not None else json.dumps({"x3": rows() if values is None else values}).encode()
    def source(name, content):
        return SourceRef("CONTROLLED:" + name, hashlib.sha256(content).hexdigest(),
                         "$", EvidenceKind.CONTROLLED)
    manifest = ReferenceSeedManifest("CONTROLLED; NOT173", 2,
        source("csv", csv_bytes), source("json", split_bytes),
        SourceRef("four/agg4.py", SELECTOR_SHA256, "lines4-6", EvidenceKind.RETAINED))
    return csv_bytes, split_bytes, manifest


def bind(values=None, csv_bytes=CSV, raw=None):
    csv_bytes, split_bytes, manifest = operands(values, csv_bytes, raw)
    return bind_reference_seed(csv_bytes, split_bytes, manifest=manifest)


def test_join_by_identity_not_array_position_and_retain_exclusions():
    seed = bind(list(reversed(rows())))
    assert [case.reference_key[0] for case in seed.cases] == ["X", "Y"]
    assert seed.cases[0].x3_source.pointer == "$.x3[2]"
    assert seed.cases[0].csv_source.pointer == "data-row:1;ending-file-line:2"
    assert seed.reference_exclusions[0].reference_key[0] == "Z"
    assert seed.reference_exclusions[0].reasons == ("REFERENCE_LINE_MATCH_FALSE",)
    assert str(seed.provided_a_sum) == "1.10"
    assert str(seed.provided_x3_sum) == "2.2"
    assert seed.label == "PROVIDED_ROW_ARITHMETIC_NOT_ENGINE_REPLAY"
    assert seed.manifest.csv_source.kind is EvidenceKind.CONTROLLED


def test_reviewed_factory_pins173_and_all_three_actual_source_hashes():
    manifest = reviewed_173_manifest("/actual-reference-root")
    assert manifest.expected_signals == 173
    assert manifest.csv_source.sha256 == CSV_SHA256
    assert manifest.split_source.sha256 == SPLIT_SHA256
    assert manifest.selector_source.sha256 == SELECTOR_SHA256
    assert manifest.selector_source.pointer == "lines4-6"


def test_wrong_input_hash_fails_before_parser():
    csv_bytes, split_bytes, manifest = operands()
    with pytest.raises(ValueError, match="reference_input_sha256_mismatch"):
        bind_reference_seed(csv_bytes + b"\n", split_bytes, manifest=manifest)


def test_wrong_selector_source_not_blindly_refreshed():
    csv_bytes, split_bytes, manifest = operands()
    manifest = replace(manifest, selector_source=replace(manifest.selector_source, sha256="0" * 64))
    with pytest.raises(ValueError, match="reviewed_reference_selector_source_required"):
        bind_reference_seed(csv_bytes, split_bytes, manifest=manifest)


def test_duplicate_json_object_keys_are_not_last_value_wins():
    with pytest.raises(ValueError, match="duplicate_reference_json_key"):
        bind(raw=b'{"x3":[],"x3":[]}')


def test_duplicate_x3_identity_is_not_silently_deduplicated():
    values = rows()
    values.append(values[0].copy())
    with pytest.raises(ValueError, match="duplicate_reference_x3_identity"):
        bind(values)


def test_selector_cannot_silently_remove_a_csv_seed():
    values = rows()
    values[0]["line_match"] = False
    with pytest.raises(ValueError, match="reference_selector_csv_membership_mismatch"):
        bind(values)


def test_reference_exclusion_missing_exit_is_not_firstbar_rule_skip():
    values = rows()
    values[2]["line_match"] = True
    values[2]["real_usd"] = None
    result = bind(values)
    assert result.reference_exclusions[0].reasons == ("REFERENCE_MISSING_REPORTED_REAL_EXIT",)
    assert result.reference_exclusions[0].source.pointer == "$.x3[2]"


def test_quantity_join_must_be_exact_not_grouped_by_symbol_only():
    values = rows()
    values[0]["qty"] = 101
    with pytest.raises(ValueError, match="reference_join_session_or_quantity_mismatch"):
        bind(values)


def test_absent_pnl_without_explicit_skip_is_unknown_not_zero():
    values = rows()
    values[1].pop("fbh_why")
    with pytest.raises(ValueError, match="reference_x3_pnl_missing_without_skip"):
        bind(values)
