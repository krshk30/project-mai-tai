"""Compare MAIN's two outputs; no source import or native/provider access."""

import argparse
import json
from pathlib import Path


def compare(old, new):
    if old["contract"] != "PRICE1-CENSUS-RESULT-v4" or new["contract"] != "PRICE1-CENSUS-RESULT-v4":
        raise ValueError("comparison requires v4 census outputs with explicit session/clock/window/config")
    if old["input_sha256"] != new["input_sha256"] or old["cap"] != new["cap"] or new["cap"] != 16:
        raise ValueError("comparison requires identical retained input and cap 16")
    for field in ("session_date", "study_start_ns", "study_end_ns", "validation_window", "clock_basis", "initial_state"):
        if old[field] != new[field]:
            raise ValueError(f"comparison study/config mismatch: {field}")
    minutes = []
    for minute in sorted(old["minutes"].keys() | new["minutes"].keys()):
        before, after = old["minutes"].get(minute), new["minutes"].get(minute)
        minutes.append({"minute": minute, "old": before, "new": after,
            "delta_max": after["candidate_max"] - before["candidate_max"] if before and after else None,
            "delta_desired_owner_max": (after["desired_owner_max"] - before["desired_owner_max"]
                                        if before and after else None),
            "delta_cap_refusals": after["cap_refusals"] - before["cap_refusals"] if before and after else None,
            "scope": "SAMPLED_RETAINED_EVENTS" if before and after else "UNKNOWN_MISSING_MINUTE"})
    return {"session_date": new["session_date"],
        "old_head": old["declared_head"], "new_head": new["declared_head"],
        "old_source_bindings": old["source_bindings"], "new_source_bindings": new["source_bindings"],
        "source_roots_pinned": old["source_root_pinned"] is True and new["source_root_pinned"] is True,
        "input_sha256": new["input_sha256"], "old_max": old["maximum"], "new_max": new["maximum"],
        "scope": new["scope"], "clock_basis": new["clock_basis"], "initial_state": new["initial_state"],
        "old_desired_owner_max": old["desired_owner_maximum"], "new_desired_owner_max": new["desired_owner_maximum"],
        "old_cap_refusals": old["cap_refusals"], "new_cap_refusals": new["cap_refusals"],
        "study_start_ns": new["study_start_ns"], "study_end_ns": new["study_end_ns"],
        "unknown_minutes": [row["minute"] for row in minutes if row["scope"] == "UNKNOWN_MISSING_MINUTE"],
        "cap": 16, "old_detections": old["required_names"], "new_detections": new["required_names"],
        "coverage": [old["coverage"], new["coverage"]], "minutes": minutes,
        "acceptance": "UNKNOWN_UNTIL_MAIN_VERIFIES_INPUT_COMPLETENESS_AND_ORIGINAL_CASES"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(json.loads(args.old.read_text()), json.loads(args.new.read_text())), sort_keys=True))


if __name__ == "__main__":
    main()
