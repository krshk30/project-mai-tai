"""Parent supplies exact evidence; no moving-ref or default candidate fallback."""
import re
from pathlib import PurePosixPath
from release_policy import BOX, CUMULATIVE, Stop, need

HELPERS = ("expected_flags_check.py", "expected_flags.json", "expected_numeric.json",
           "v2_restart_evidence.py", "preopen_restart_evidence.sh")
INPUTS = ("snapshot", "fleet_before", "journal", "continuation_journal", "human_review")
EXISTING_MERGES = ((1102, "52659779bcf4b6e28a2896e6eef3002488a6d2c9"),
                   (1106, "f80a9c4af5e79beaa214e99f448bfe408c596df1"))
BATCH_GOVERNING_PRS = [1104, 1099, 1105, 1101]


def sha(value, size):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value) is not None


def validate_binding(value):
    need(isinstance(value, dict), "parent binding absent")
    need(sha(value.get("approved_sha"), 40) and sha(value.get("tree"), 40)
         and value["approved_sha"] != BOX and value.get("box_sha") == BOX,
         "APP/TREE unbound or BOX differs; do not execute")
    rows = value.get("merged_prs", [])
    need(isinstance(rows, list) and len(rows) == 3
         and all(isinstance(row, dict) and type(row.get("pr")) is int and row["pr"] > 0
                 and sha(row.get("merge_sha"), 40) for row in rows),
         "two completed merges plus one actual batch merge required")
    need(len({row["pr"] for row in rows}) == len({row["merge_sha"] for row in rows}) == 3
         and all((row["pr"], row["merge_sha"]) == expected
                 for row, expected in zip(rows[:2], EXISTING_MERGES))
         and all(not row.get("governing_prs") for row in rows[:2])
         and rows[2]["pr"] not in BATCH_GOVERNING_PRS
         and rows[2].get("governing_prs") == BATCH_GOVERNING_PRS,
         "merge identity/order or reviewed batch coverage differs from parent ruling")
    paths = value.get("reviewed_changed_paths", [])
    need(paths and len(set(paths)) == len(paths), "exact reviewed source scope absent/duplicate")
    for path in paths:
        pure = PurePosixPath(path)
        need(str(pure) == path and not pure.is_absolute() and ".." not in pure.parts
             and path.startswith(("src/project_mai_tai/", "tests/", "ops/health/", "docs/"))
             and not any(word in path for word in ("migration", "alembic", "systemd")),
             "source scope outside authorized assembly: " + path)
    need(sha(value.get("source_combination_proof_sha256"), 64), "parent combination proof unbound")
    need(isinstance(value.get("t43_catalog_name"), str) and value["t43_catalog_name"],
         "T43 exact existing ON catalog identity unbound")
    inputs = value.get("install1", {})
    need(set(inputs) == set(INPUTS), "original Install1 chain incomplete")
    for item in inputs.values():
        need(isinstance(item, dict) and isinstance(item.get("path"), str)
             and item["path"].startswith("/home/trader/") and sha(item.get("sha256"), 64),
             "Install1 reviewed evidence path/hash unbound")
        path = PurePosixPath(item["path"])
        need(str(path) == item["path"] and ".." not in path.parts, "Install1 evidence path escape")
    need(set(value.get("old_helper_hashes", {})) == set(HELPERS)
         and all(sha(item, 64) for item in value["old_helper_hashes"].values()),
         "actual old isolated catalogs/helper pins unbound")
    need(value.get("control_restart") is True, "control scope withdrawal requires revised tested runner")
    return value
