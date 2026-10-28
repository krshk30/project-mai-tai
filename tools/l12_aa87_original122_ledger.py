"""Offline original122 UNKNOWN ledger, NOT BUY acceptance or a replay engine.

No application imports, callback execution, engine, provider or clock projection.
The CLI pins actual export bytes. Missing native/close operands stay UNKNOWN.
"""

import argparse
from collections import Counter
from datetime import date, time
import hashlib
import json
import os
from pathlib import Path
import re

APP_HEAD = "aa87a13c0ea36adc8c982f77783026121f38cc65"
SRC_TREE = "31d566e8cb75022581d02dfe7a94dff2d1701059"
EXPORT_SHA = "31147bd20bb1f0c3e9a997bc45f9dbf41ef5fb71a6a58bec4b9c0a9e30c6c5f6"
GROUPS = {"A": 27, "B": 17, "C": 12, "D": 9, "TAKEN": 57}
APP_PINS = {
    "brokerdecides1.py": "0f3132bbd9d84154107024a05e1327678777695cfdc5fcca7093c412172eaf98",
    "brokerdecides1_entry.py": "3e244ccab4d5ba44a6adc029fad7fa80f31c77704c74d5f67b0c34dc188a33c3",
    "brokerdecides1_native.py": "389202ffe0cf7e27339e09f2720aa25afea3ac89ced83a77ac5069b82ec9a6c1",
    "brokerdecides1_collect.py": "74895e5c6d747b021232c0e11a43a3f60b0d259d108458ab8cda967eda73ac9b",
    "brokerdecides1_install_chunks.py": "de1fad979b35af74bfae77616a31334bc7d00107685bda61e91765464746c815",
    "brokerdecides1_retirement.py": "fbdc1030142f255a736cd7a95316657c6cbb8bf4b0d7e02e67687285e6865d0f",
    "brokerdecides1_sync.py": "1b8e40842b032cf735b82024ad6e3c7d4a42ce2274b6b02fbd1a830a5e07cd50",
    "brokerdecides1_accounts.py": "49b38edab2705d348baf05baa674022dc1294106603909a3400851cb02ad28dc",
    "cancelack1.py": "1e4887dbc9b6fbdf54bafc92f3ab48a8f01ecc01ba145f1c8042d5d17817c381",
    "db/models.py": "eb0593e46163bdeeedcd72bcf469cdbacdb50499b90e8e39db3393dfddfb9cd4",
    "brokerdecides1_set_proof.py": "c47773dbd1f56a80221b598df1c6ae762019119481945971f432997c05c411e4",
    "oms/service.py": "98184f5c40e1441ecb527d2e2efe95356eb709e4080b0d084c9968997b3c95e6",
    "broker_adapters/webull.py": "da3be1841a5358271285bff7f5c956e870e0997d02732da5951b415371a59c56",
    "strategy_core/schwab_1m_v2.py": "b47d77a0f16407e63d1fb6c20c1f8a41b931e1c72462ec83842a5977dff8100d",
    "services/schwab_1m_v2_bot.py": "242c9ec431ecfbe78853e2cbdf8a82d5494d42cecb069afc825e023587c2d9d1",
    "settings.py": "2a4b92f8618b8984941863cf7236ba5f7a489a05416e051b6827c46bc62f08b7",
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_export_key")
        result[key] = value
    return result


def index_rows(raw, *, expected_sha, expected_groups):
    """Pure contract helper; synthetic operands allowed in UNIT controls only."""
    if digest(raw) != expected_sha:
        raise ValueError("original_export_hash_mismatch")
    rows = json.loads(raw, object_pairs_hook=unique_keys)
    if not isinstance(rows, list) or len(rows) != sum(expected_groups.values()):
        raise ValueError("complete_original_denominator_required")
    seen, output, groups = set(), [], Counter()
    for index, row in enumerate(rows):
        day, symbol, flip = row["day"], row["sym"], row["flip"]
        date.fromisoformat(day)
        time.fromisoformat(flip)
        if (not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9.-]+", symbol)
                or not re.fullmatch(r"\d{2}:\d{2}", flip)
                or type(row.get("taken")) is not bool or type(row.get("reached")) is not bool):
            raise ValueError("original_case_identity_or_fact_UNKNOWN")
        identity = (day, symbol, flip)
        if identity in seen:
            raise ValueError("duplicate_original_case")
        seen.add(identity)
        groups[row["group"]] += 1
        output.append({"case_id": "/".join(identity), "record_ordinal_1based": index + 1,
            "source_json_pointer": "/" + str(index), "source_sha256": expected_sha,
            "canonical_record_sha256": digest(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()),
            "original_record": row, "decision": "UNKNOWN",
            "reason": "asof_native_book_positions_and_local_lineage_not_recorded",
            "actual_buy_replay": "NOT_RUN_MISSING_OPERANDS",
            "historical_taken_is_not_asof_broker_flat": True})
    if dict(groups) != expected_groups:
        raise ValueError("original_group_membership_changed")
    return output


def original_ledger(raw, *, tool_head):
    if not isinstance(tool_head, str) or not re.fullmatch(r"[0-9a-f]{40}", tool_head):
        raise ValueError("exact_tool_head_required")
    rows = index_rows(raw, expected_sha=EXPORT_SHA, expected_groups=GROUPS)
    return {"status": "ORIGINAL122_ACCOUNTED_UNKNOWN_NOT_BUY_ACCEPTANCE", "application_head": APP_HEAD,
        "application_src_tree": SRC_TREE, "tool_head_declared": tool_head, "source_sha256": EXPORT_SHA,
        "counts": {"original": 122, "CLEAR": 0, "HOLD": 0, "UNKNOWN": 122},
        "groups": GROUPS, "rows": rows, "actual_tonight_close_boot": "UNKNOWN_OPERAND_NOT_DELIVERED",
        "controlled1062_copy": "SEPARATE_CLAIM_NOT_REAL_CLOSE_STATE", "native_flatness": "UNKNOWN"}


LOCAL_INPUT_TOPICS = (
    "orders_and_intents", "fills_and_events", "owner_retry_notes",
    "line_and_coverage", "account_aliases", "dispatch_reprice_lineage",
)


def _local_case_input_index(indexed_rows, inputs):
    """Reference intake only; declarations are NOT data/temporal completeness.

    UNIT controls may use synthetic indexed rows. The public original122
    entrypoint below always pins the real export and original population.
    No native-history wait, PG access or old/new application callback runs here.
    """
    known = {row["case_id"] for row in indexed_rows}
    if (not isinstance(inputs, dict) or not set(inputs) <= known
            or len(known) != len(indexed_rows)):
        raise ValueError("original_case_membership_required_no_extra_or_duplicate_ids")
    output = []
    for row in indexed_rows:
        supplied = inputs.get(row["case_id"], {})
        if not isinstance(supplied, dict) or not set(supplied) <= set(LOCAL_INPUT_TOPICS):
            raise ValueError("fixed_local_case_input_topics_required")
        topics = {}
        for topic in LOCAL_INPUT_TOPICS:
            refs = supplied.get(topic, [])
            if not isinstance(refs, list) or len(refs) > 256:
                raise ValueError("bounded_explicit_reference_list_required")
            seen, bound = set(), []
            for ref in refs:
                if (not isinstance(ref, dict) or set(ref) != {"artifact_sha256", "reference", "record_pointer", "temporal_scope"}
                        or not isinstance(ref["artifact_sha256"], str)
                        or not re.fullmatch(r"[0-9a-f]{64}", ref["artifact_sha256"])
                        or not isinstance(ref["reference"], str) or not ref["reference"]
                        or len(ref["reference"]) > 1024
                        or not isinstance(ref["record_pointer"], str) or not ref["record_pointer"].startswith("/")
                        or len(ref["record_pointer"]) > 1024
                        or ref["temporal_scope"] not in {"RETAINED_EVENT_CHRONOLOGY", "CURRENT_RETAINED_NOT_ASOF"}):
                    raise ValueError("source_hash_location_and_explicit_temporal_scope_required")
                key = (ref["artifact_sha256"], ref["reference"], ref["record_pointer"])
                if key in seen:
                    raise ValueError("duplicate_local_case_reference")
                seen.add(key)
                bound.append(dict(ref))
            topics[topic] = {"references": bound,
                "state": "REFERENCES_DECLARED_BYTES_AND_MEMBERSHIP_NOT_YET_VALIDATED" if bound else "UNKNOWN_NOT_SUPPLIED"}
        output.append({"case_id": row["case_id"], "record_ordinal_1based": row["record_ordinal_1based"],
            "source_json_pointer": row["source_json_pointer"], "source_sha256": row["source_sha256"],
            "canonical_record_sha256": row["canonical_record_sha256"], "local_inputs": topics,
            "original_result": row["original_record"],
            "native_asof": "UNKNOWN_NOT_STORED_NOT_REQUIRED_TO_INDEX_LOCAL_INPUTS",
            "unresolved_parent_references": "MUST_REMAIN_UNRESOLVED_UNTIL_EXACT_LINK_PROOF",
            "local_branch_execution": "UNRUN_REQUIRES_REAL_OLD_AND_NEW_SOURCE_CALLBACKS",
            "decision": "UNKNOWN", "permission_granted": False})
    return {"status": "LOCAL_CASE_REFERENCE_INTAKE_NOT_REPLAY_OR_COMPLETENESS_PROOF",
        "original_cases": len(indexed_rows), "case_rows": output,
        "omitted_cases": 0, "source_absence_is_flat": False, "permission_granted": False}


def original122_local_inputs(raw, inputs):
    """Exact real122 population; unknown/missing topics never remove a case."""
    indexed = index_rows(raw, expected_sha=EXPORT_SHA, expected_groups=GROUPS)
    result = _local_case_input_index(indexed, inputs)
    result.update(population="ORIGINAL122_HASH_BOUND", original_export_sha256=EXPORT_SHA,
                  historical_native_permission="UNKNOWN", actual_close_boot="UNRUN")
    return result


def verify_application(root):
    actual = {name: digest((root / "src/project_mai_tai" / name).read_bytes()) for name in APP_PINS}
    if actual != APP_PINS:
        raise ValueError("exact_aa87_application_source_mismatch")
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-export", required=True, type=Path)
    parser.add_argument("--tool-head", required=True)
    parser.add_argument("--application-head", required=True, choices=[APP_HEAD])
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        raise ValueError("fresh_output_required")
    pins = verify_application(Path(__file__).resolve().parents[1])
    report = original_ledger(args.original_export.read_bytes(), tool_head=args.tool_head)
    report["application_source_sha256"] = pins
    with args.out.open("x") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(report, handle, sort_keys=True, indent=2)
        handle.write("\n")
    print(report["status"])
    return 2  # Accounting complete; never present UNKNOWN122 as a qualification PASS.


if __name__ == "__main__":
    raise SystemExit(main())
