"""Transport via stdin with FLAT_SOURCE/FLAT_SHA injected; no remote file writes.

Uses unchanged staged helpers for their pure reads. Official snapshot output
is redirected to an in-memory sink; the exact collector and parser still run.
No deploy lock creation, git fetch, staging, token refresh or service actions.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import ModuleType

REPO = Path("/home/trader/project-mai-tai")
FLAT_SOURCE = globals()["FLAT_SOURCE"]
FLAT_SHA = globals()["FLAT_SHA"]
OLD = Path("/home/trader/after-hours/2026-10-05/owned-entry-rpg-all-on-1910-native-tz-job")
APP = "7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf"
BOX = "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89"
sys.path[:0] = [str(OLD), str(REPO / "src")]


def stamp():
    return datetime.now(timezone.utc).isoformat()


def command(name, args):
    print("REHEARSAL_CALL", stamp(), name, json.dumps(args), flush=True)
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    print(result.stdout, end="", flush=True)
    print(result.stderr, end="", file=sys.stderr, flush=True)
    print("REHEARSAL_RESULT", name, "rc=" + str(result.returncode), flush=True)
    if result.returncode:
        raise RuntimeError(name + " refused rc=" + str(result.returncode))
    return result.stdout.strip()


def flat_turn(name, service=None):
    print("REHEARSAL_CALL", stamp(), name, service, flush=True)
    for attempt in range(1, 4):
        try:
            rc = asyncio.run(flat.collect(service))
        except Exception as exc:
            print("UNKNOWN", type(exc).__name__, str(exc), flush=True)
            rc = 2
        print("REHEARSAL_RESULT", name, "attempt=" + str(attempt), "rc=" + str(rc), flush=True)
        if rc == 0:
            return
        if rc != 2 or attempt == 3:
            raise RuntimeError(name + " refused rc=" + str(rc))
        time.sleep(60)


print("READ_ONLY_REHEARSAL_BEGIN", stamp(), "native_TZ=" + str(os.environ.get("TZ")), flush=True)
assert hashlib.sha256(FLAT_SOURCE.encode()).hexdigest() == FLAT_SHA  # Injected exact local helper.
print("IN_MEMORY_HELPER_SHA256", FLAT_SHA, flush=True)
flat = ModuleType("rehearsal_flat")
exec(compile(FLAT_SOURCE, "strict_flat_readonly.py", "exec"), flat.__dict__)
actions = importlib.import_module("actions")
census = importlib.import_module("census_readonly")
proof = importlib.import_module("proof")
redis_checkpoint = importlib.import_module("redis_checkpoint")

command("native-clock", ["date", "-u", "--iso-8601=seconds"])
assert command("head", ["git", "-C", str(REPO), "rev-parse", "HEAD"]) == BOX
assert not command("clean", ["git", "-C", str(REPO), "status", "--porcelain"])
flat_turn("general-oms", "oms")
flat_turn("general-strategy", "strategy")
sys.argv = ["census_readonly.py", "--require-reviewed"]
for attempt in range(1, 4):
    print("REHEARSAL_CALL", stamp(), "census", "attempt=" + str(attempt), flush=True)
    rc = asyncio.run(census.main())
    print("REHEARSAL_RESULT census", "rc=" + str(rc), flush=True)
    if rc == 0:
        break
    if rc != 2 or attempt == 3:
        raise RuntimeError("census refused rc=" + str(rc))
    time.sleep(60)
baseline = redis_checkpoint.collect()
print("REHEARSAL_REDIS_BEFORE", json.dumps(baseline, sort_keys=True), flush=True)
command("oms-restart-fence", ["nice", "-n", "19", str(REPO / "ops/preflight/preflight_oms_restart.sh"),
                              "--require-all-account-positions-flat"])
flat_turn("fresh-flat-before-v2")
command("v2-restart-fence", ["nice", "-n", "19", str(REPO / "ops/preflight/preflight_v2_restart.sh")])
before = proof.capture(str(REPO))
print("REHEARSAL_CAPTURE", json.dumps(before, default=str, sort_keys=True), flush=True)
command("box-ancestor", ["git", "-C", str(REPO), "merge-base", "--is-ancestor", BOX, APP])
main = command("remote-main-read-only", ["runuser", "-u", "trader", "--", "git", "-C", str(REPO),
                                        "ls-remote", "origin", "refs/heads/main"]).split()[0]
command("target-ancestor", ["git", "-C", str(REPO), "merge-base", "--is-ancestor", APP, main])
paths = command("main-ahead-paths", ["git", "-C", str(REPO), "diff", "--name-only", APP, main]).splitlines()
assert all(path.startswith("docs/") for path in paths)
actions.source_proof(OLD)  # Application source/preopen hashes unchanged; no write.
spec = importlib.util.spec_from_file_location("official_evidence", REPO / "ops/health/v2_restart_evidence.py")
official = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = official
spec.loader.exec_module(official)


class MemorySnapshot:
    parent = None

    def __init__(self):
        self.parent = self
        self.payload = None

    def mkdir(self, **kwargs):
        pass

    def write_text(self, value, **kwargs):
        self.payload = json.loads(value)

    def __str__(self):
        return "in-memory-official-snapshot-no-write"


sink = MemorySnapshot()
assert official.snapshot(sink)
print("REHEARSAL_OFFICIAL_SNAPSHOT", json.dumps(sink.payload, sort_keys=True), flush=True)
command("import-path", [str(REPO / ".venv/bin/python"), "-c",
                        'import project_mai_tai; print(project_mai_tai.__file__); assert project_mai_tai.__file__.startswith("/home/trader/project-mai-tai/src/")'])
flat_turn("final-pre-stop-flat")
after = redis_checkpoint.collect()
assert after["evicted_keys"] == baseline["evicted_keys"]
assert after["stream_types"] == baseline["stream_types"]
assert after["sets"] == baseline["sets"]
print("REHEARSAL_REDIS_AFTER", json.dumps(after, sort_keys=True), flush=True)
for name, state in before["services"].items():
    assert proof.service(name) == state, name
print("READ_ONLY_REHEARSAL_COMPLETE", stamp(), "all_pre_write_read_gates=PASS",
      "approval_and_post_install_gates=execution_only", "no_remote_files_or_services_changed", flush=True)
