"""Pure source-transition check, not a migration runner or execution approval.

Main must independently establish the committed L14 head and source membership.
This check only binds that acknowledgment to the exact reviewed source bytes.
It never reads Git, writes files, imports migrations, or accepts a staged head.
Historical V8 remains a separate unchanged standalone contract.
"""

from hashlib import sha256

from tools.l12_close_asof_contract import is_head, is_sha


L12_PATH = "sql/migrations/versions/20261022_l12_dispatch_retirement.py"
L14_PATH = "sql/migrations/versions/20261023_atr_simulation_store.py"
SCHEMA_PATH = "src/project_mai_tai/atr_simulation/store_schema_20261023.py"
HISTORICAL_L12_SHA256 = "2670f9b3c997713ca61983e5be40aae80e5159e5f8796336cb75b1ba4ba864bf"
STAGED_REVIEW_PINS = {
    L14_PATH: "185e4ee38f868b84fe031fc77232061e081b02dacf429bd4a0048d26aaaeea0b",
    SCHEMA_PATH: "c6e98ec1865b36f48d810d813dc36c6a335d579860d77549319e418d462bb2bc",
}
OLD_PARENT = b'down_revision = "20261015_bot_control"'
NEW_PARENT = b'down_revision = "20261023_atr_simulation"'


def review_transition(*, original_l12, proposed_l12, l14_sources, commit_ack,
                      review_record_sha256):
    """Validate a proposed one-line successor after an independent commit ACK.

    A hash-looking head is not evidence of Git membership by itself. The caller
    must retain Main's actual commit/source observations with the review record.
    New L14 source bytes require a newly reviewed contract, not refreshed pins.
    """
    if (not isinstance(original_l12, bytes)
            or sha256(original_l12).hexdigest() != HISTORICAL_L12_SHA256
            or original_l12.count(OLD_PARENT) != 1):
        raise ValueError("exact_historical_unapplied_l12_source_required")
    if (not isinstance(l14_sources, dict) or set(l14_sources) != set(STAGED_REVIEW_PINS)
            or any(not isinstance(raw, bytes) or sha256(raw).hexdigest() != STAGED_REVIEW_PINS[path]
                   for path, raw in l14_sources.items())):
        raise ValueError("exact_reviewed_l14_migration_and_frozen_schema_required")
    if (not isinstance(commit_ack, dict)
            or set(commit_ack) != {"state", "head", "source_pins", "ack_record_sha256"}
            or commit_ack["state"] != "COMMITTED_SOURCE_ACK"
            or not is_head(commit_ack["head"])
            or commit_ack["source_pins"] != STAGED_REVIEW_PINS
            or not is_sha(commit_ack["ack_record_sha256"])
            or not is_sha(review_record_sha256)):
        raise ValueError("independent_committed_l14_source_ack_and_review_required")
    if (not isinstance(proposed_l12, bytes)
            or proposed_l12 != original_l12.replace(OLD_PARENT, NEW_PARENT)):
        raise ValueError("only_same_l12_revision_single_parent_line_change_allowed")
    return {
        "status": "REVIEWED_SOURCE_CHAIN_ONLY_NOT_APPLIED_OR_BOOT_PROOF",
        "l14_committed_head": commit_ack["head"],
        "l14_commit_ack_sha256": commit_ack["ack_record_sha256"],
        "review_record_sha256": review_record_sha256,
        "reviewed_l14_sources": dict(STAGED_REVIEW_PINS),
        "l12_original_sha256": HISTORICAL_L12_SHA256,
        "l12_proposed_sha256": sha256(proposed_l12).hexdigest(),
        "chain": ["20261015_bot_control", "20261023_atr_simulation", "20261022_l12_retirement"],
        "historical_v8": "UNCHANGED_NOT_A_COMBINED_PROFILE",
        "actual_migration_applied": "UNRUN", "actual_close_boot": "UNKNOWN",
        "permission_granted": False,
    }
