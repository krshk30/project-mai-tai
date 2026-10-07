"""Derive a labelled two-install record; original snapshot bytes never change."""
import json
from pathlib import Path
from daily import exclusive, system_time
from release_policy import BOX, CHANGED, CUMULATIVE, PHASES, SERVICES, canonical, digest, need, states

INSTALL1_ACTIONS = {("stop", "schwab-1m-v2"), ("stop", "orb-schwab"),
                    ("restart", "oms"), ("restart", "strategy"),
                    ("restart", "control"), ("start", "orb-schwab")}


def bounded(path):
    need(path.is_file() and not path.is_symlink() and path.stat().st_size <= 8_000_000,
         "evidence missing/symlink/overflow: " + str(path))
    return path.read_bytes()


def journal_actions(path, raw):
    actions, receipts = [], {}
    for line in raw.splitlines():
        row = json.loads(line)
        if "receipt" in row:
            name = row["receipt"]
            need(Path(name).name == name, "journal receipt path escape")
            if not name.endswith("command.json"):
                continue
            file = path.parent / name
            value = bounded(file)
            need(digest(value) == row["sha256"] and len(value) == row["bytes"],
                 "journal referenced receipt drift")
            receipts[str(file)] = digest(value)
            row = json.loads(value)
        argv = row.get("argv", [])
        if len(argv) == 3 and argv[0] == "systemctl" and argv[1] in {"stop", "start", "restart"}:
            unit = argv[2]
            need(unit.startswith("project-mai-tai-") and unit.endswith(".service"), "foreign Install1 action")
            action = (argv[1], unit.removeprefix("project-mai-tai-").removesuffix(".service"))
            need(action in INSTALL1_ACTIONS and row.get("rc") == 0, "unreviewed/failed Install1 action")
            actions.append(action)
    return actions, receipts


def load_install1(binding, current):
    raw, paths = {}, {}
    for name, item in binding["install1"].items():
        paths[name] = Path(item["path"])
        raw[name] = bounded(paths[name])
        need(digest(raw[name]) == item["sha256"], "Install1 evidence hash drift: " + name)
    snapshot, before, review = (json.loads(raw[name]) for name in ("snapshot", "fleet_before", "human_review"))
    # The original JSONL includes the continuation's command receipts. Its text
    # log also contains the disclosed receipt-collision traceback, not JSON rows.
    actions, references = journal_actions(paths["journal"], raw["journal"])
    need(set(actions) == INSTALL1_ACTIONS, "actual Install1 service-action receipts incomplete")
    need(review.get("kind") == "DERIVED_HUMAN_VERIFIED_INSTALL1_INCOMPLETE"
         and review.get("application") == BOX and review.get("original_complete") is False
         and review.get("evidence_hashes") == {name: digest(raw[name]) for name in
              ("snapshot", "fleet_before", "journal", "continuation_journal")},
         "human review derivation not bound; no original COMPLETE claim")
    provenance = review.get("provenance", {})
    need(isinstance(provenance.get("human_quote"), str) and "VERIFIED" in provenance["human_quote"]
         and isinstance(provenance.get("source"), str) and provenance["source"].strip(),
         "actual human VERIFIED authorization quote/provenance missing")
    need(set(before) == set(current) == set(SERVICES), "actual Install1/Install2 fleet census incomplete")
    need(set(review.get("verified_pins", {})) == set(CUMULATIVE), "five reviewer identity pins incomplete")
    for name, pin in review["verified_pins"].items():
        need(pin == {key: current[name][key] for key in ("MainPID", "ExecMainStartTimestamp")},
             "fresh fleet differs from human-reviewed identity: " + name)
    need(current["orb-schwab"]["MainPID"] == 612486 and current["market-data"]["MainPID"] == 2907,
         "retained ORB-Schwab/gateway identity differs from human ruling")
    restarted = set(CUMULATIVE)
    for name in SERVICES:
        old, now = before[name], current[name]
        if name in restarted:
            need(old["MainPID"] != now["MainPID"] and now["MainPID"] > 0
                 and now["NRestarts"] == 0 and now["Result"] == "success"
                 and now["ActiveState"] == "active" and now["SubState"] == "running"
                 and system_time(now["ExecMainStartTimestamp"]) > system_time(old["ExecMainStartTimestamp"]),
                 "Install1 restart not measured: " + name)
        else:
            need(old == now, "Install1 unreviewed non-scoped identity/state drift: " + name)
    need(set(CUMULATIVE) <= set(snapshot["services"]), "original snapshot misses cumulative owners")
    for name, old in snapshot["services"].items():
        if name == "tv-alerts":
            # The official collector includes this inactive unit; attended fleet never did.
            need(old["pid"] == 0 and old["active_state"] == "inactive" and old["sub_state"] == "dead"
                 and old["n_restarts"] == 0, "original inactive tv-alerts identity unproven")
            continue
        need(name in before and old["pid"] == before[name]["MainPID"]
             and old["active_state"] == before[name]["ActiveState"]
             and old["sub_state"] == before[name]["SubState"]
             and old["n_restarts"] == before[name]["NRestarts"],
             "original snapshot does not match actual Install1 before fleet: " + name)
    v2_start = dict(service="schwab-1m-v2", classification="IDENTITY_AND_HUMAN_REVIEW_PROVEN_START",
                    command_receipt="UNAVAILABLE", old_pid=before["schwab-1m-v2"]["MainPID"],
                    new_pid=current["schwab-1m-v2"]["MainPID"],
                    new_start=current["schwab-1m-v2"]["ExecMainStartTimestamp"], provenance=provenance)
    return dict(raw=raw, paths=paths, snapshot=snapshot, references=references,
                proof=dict(classification="DERIVED_HUMAN_VERIFIED_INSTALL1_INCOMPLETE",
                           original_complete=False, actual_actions=[list(item) for item in actions],
                           identity_derived_start=v2_start,
                           input_hashes={name: digest(value) for name, value in raw.items()},
                           referenced_receipt_hashes=references, actual_install2_before=current,
                           provenance=provenance))


def publish(effects):
    states(effects.before, effects.last, len(PHASES))
    previous = load_install1(effects.release["binding"], effects.before)
    need(previous["proof"] == effects.install1["proof"], "Install1 chain moved during Install2")
    snapshot = effects.attempt / "install1-original-before-restart.json"
    exclusive(snapshot, previous["raw"]["snapshot"])
    record = effects.attempt / "cumulative-install-record.json"
    source = effects.attempt / "cumulative-source-journals.json"
    second_snapshot = effects.attempt / "before-restart.json"
    journal = effects.attempt / "runner-journal.jsonl"
    need(second_snapshot.is_file() and journal.is_file(), "Install2 actual evidence incomplete")
    observed = json.loads(second_snapshot.read_bytes())["services"]
    for name, row in observed.items():
        if name in effects.before:
            expected = effects.before[name]
            need(row["pid"] == expected["MainPID"] and row["active_state"] == expected["ActiveState"]
                 and row["sub_state"] == expected["SubState"] and row["n_restarts"] == expected["NRestarts"],
                 "actual Install2 snapshot differs from recorded initial fleet: " + name)
    for name, old in previous["snapshot"]["services"].items():
        need(name in observed, "actual Install2 official snapshot missing service: " + name)
        if name not in CUMULATIVE:
            need(all(observed[name][key] == old[key] for key in ("pid", "active_state", "sub_state", "n_restarts")),
                 "original/current untouched official snapshot identity drift: " + name)
    chain = dict(classification="DERIVED_CUMULATIVE_TWO_INSTALLS", install1=previous["proof"],
                 install2=dict(before_fleet_sha256=digest(canonical(effects.before)),
                               snapshot=str(second_snapshot), snapshot_sha256=digest(second_snapshot.read_bytes()),
                               source_journal=str(journal), source_journal_checkpoint_sha256=digest(journal.read_bytes()),
                               actions=[list(item) for item in PHASES], actual_after=effects.last))
    exclusive(source, canonical(chain))
    actions = {name: "restarted" if name in CUMULATIVE else "deliberately_untouched"
               for name in previous["snapshot"]["services"]}
    exclusive(record, canonical(dict(schema_version=1, classification="DERIVED_CUMULATIVE_TWO_INSTALLS",
              install1_original_complete=False,
              snapshot_captured_at_utc=previous["snapshot"]["captured_at_utc"], source_journal=str(source),
              service_actions=actions, cumulative_installs=2, install2_restarted=list(CHANGED),
              original_snapshot_sha256=digest(previous["raw"]["snapshot"]))))
    effects.cumulative_inputs = [source, second_snapshot, effects.attempt / "fleet-before.json",
                                *previous["paths"].values(), *map(Path, previous["references"])]
    return snapshot, record
