"""Local derivation from actual read-only files and quoted human authority, not COMPLETE."""
import argparse
import json
from pathlib import Path
from cumulative import bounded, load_install1
from daily import exclusive
from release_policy import BOX, CUMULATIVE, canonical, digest, need


def derive(inputs, current_path, provenance, output):
    need(set(inputs) == {"snapshot", "fleet_before", "journal", "continuation_journal"},
         "four real Install1 inputs required")
    raws = {}
    for name, item in inputs.items():
        raws[name] = bounded(Path(item["path"]))
        need(digest(raws[name]) == item["sha256"], "derivation input hash differs")
    current_raw = bounded(current_path)
    current = json.loads(current_raw)
    need(isinstance(provenance.get("human_quote"), str) and "VERIFIED" in provenance["human_quote"]
         and provenance.get("source"), "quoted human review provenance required")
    review = dict(kind="DERIVED_HUMAN_VERIFIED_INSTALL1_INCOMPLETE", application=BOX,
                  original_complete=False, evidence_hashes={name: digest(raw) for name, raw in raws.items()},
                  fresh_current_fleet_sha256=digest(current_raw), provenance=provenance,
                  verified_pins={name: {key: current[name][key] for key in
                                       ("MainPID", "ExecMainStartTimestamp")} for name in CUMULATIVE})
    destination = output.resolve()
    need(not any(str(destination).startswith(prefix) for prefix in
                 ("/home/trader", "/etc", "/private/etc", "/run", "/var")),
         "derivation is local only; parent owns publication")
    destination.mkdir(mode=0o700)
    path = destination / "install1-derived-human-review.json"
    raw = canonical(review)
    exclusive(path, raw)
    refs = {**inputs, "human_review": dict(path=str(path), sha256=digest(raw))}
    evidence = load_install1(dict(install1=refs), current)
    snapshot = json.loads(raws["snapshot"])
    record = dict(schema_version=1, classification="DERIVED_HUMAN_VERIFIED_INSTALL1_INCOMPLETE",
                  original_complete=False, snapshot_captured_at_utc=snapshot["captured_at_utc"],
                  source_journal=inputs["journal"]["path"],
                  continuation_journal=inputs["continuation_journal"]["path"],
                  service_actions={name: "restarted" if name in {"schwab-1m-v2", "oms", "orb-schwab", "control"}
                                   else "deliberately_untouched" for name in snapshot["services"]},
                  derivation=evidence["proof"])
    exclusive(destination / "install1-derived-record.json", canonical(record))
    return review, record


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--current-fleet", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    review, record = derive(json.loads(args.inputs.read_bytes()), args.current_fleet,
                            json.loads(args.provenance.read_bytes()), args.output)
    print("DERIVED Install1 original_complete=false review_sha256=" + digest(canonical(review))
          + " record_sha256=" + digest(canonical(record)))


if __name__ == "__main__":
    main()
