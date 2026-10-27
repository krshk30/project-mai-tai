"""Hash-bound retained SIM policy; no provider fetch or historical-as-of claim."""

import hashlib
import json
from pathlib import Path

from .execution_eligibility import policy_from_payload
from .types import EvidenceKind, SourceRef


POLICY_LABEL = "ex-post SIM policy, snapshot retrieved ~10-05, retrieval time UNKNOWN"
MAX_POLICY_BYTES = 2 * 1024 * 1024


def load_execution_policy(reference, evidence_root):
    if reference is None:
        return None
    if (not isinstance(reference, dict) or reference.get("label") != POLICY_LABEL
            or reference.get("retrieved_at_ns") is not None):
        raise ValueError("explicit_ex_post_policy_label_required")
    value = reference["source"]
    if (value.get("pointer") != "$" or value.get("asof_ns") is not None
            or value.get("kind") != EvidenceKind.RETAINED.value):
        raise ValueError("whole_retained_policy_without_invented_asof_required")
    root = Path(evidence_root).resolve(strict=True)
    artifact = Path(value["artifact"])
    path = (artifact if artifact.is_absolute() else root / artifact).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("policy_path_outside_declared_root")
    with path.open("rb") as stream:
        raw = stream.read(MAX_POLICY_BYTES + 1)
    if len(raw) > MAX_POLICY_BYTES:
        raise ValueError("policy_file_budget_exceeded")
    if hashlib.sha256(raw).hexdigest() != value["sha256"]:
        raise ValueError("actual_policy_file_hash_mismatch")
    source = SourceRef(value["artifact"], value["sha256"], "$", EvidenceKind.RETAINED)
    return policy_from_payload(json.loads(raw), source)
