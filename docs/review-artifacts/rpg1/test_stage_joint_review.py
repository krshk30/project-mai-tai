"""Offline staging-contract regressions. No SSH, broker, or systemd execution."""
from __future__ import annotations

import copy
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from unittest.mock import AsyncMock, patch


HERE = Path(__file__).resolve().parent
JOB = HERE / "joint_install_job"
HEAD = "7a9957bfef8a7e0be624bbeb9d04e9d6fd1b3bbf"
BASE = "250ab18458f4d806aa8bcdf98d787fff5bb57df4"
MERGE = "c015c0059526b57b679f019f3a8ce2867a919b34"
RECORD_COMMIT = "d" * 40
HASH = "a" * 64
URLS = [
    "https://github.com/krshk30/project-mai-tai/actions/runs/37166028478/job/111328967755",
    "https://github.com/krshk30/project-mai-tai/actions/runs/37166026605/job/111328961970",
]
START_PIDS = {
    "market-data": "2907", "oms": "2910", "strategy": "2911", "schwab-1m-v2": "2912",
    "orb": "2913", "momentum-paper": "2914", "orb-schwab": "2915", "control": "2916",
    "market-capture": "2917", "reconciler": "2918", "redis-server": "926",
    "postgresql@16-main": "1037",
}
START_HASHES = {
    "/home/trader/preopen.sh": "79d500d82c73271d68c1ff75ab64f9533713e1116d42944baa524d6762faa239",
    "/home/trader/restart_evidence/expected_flags.json": "d15b588540000b18253a8d6cc4d01d9e389a83799d02d219dbdab317da1a1eff",
    "/home/trader/restart_evidence/expected_numeric.json": "934fd2cc177cd7dc900ebc1b85543ae08e3857364cd0a60f99f2d5a5cb2c2084",
    "/home/trader/after-hours/2026-10-05/guard-start-job/start-guard-20261005.sh": "f9ae601b2477813fdc02f31286b8c61c473b3e71386908145ba181931e9b1a81",
    "/etc/project-mai-tai/project-mai-tai.env": "68dfb7e39692da9022657f462a6682b25e25b5777b68a466f4633696ec6ca1a7",
}


def load_module(path: Path, name: str):
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


class StageReviewTests(unittest.TestCase):
    def setUp(self):
        self.stage = load_module(HERE / "stage_joint_review.py", "stage_under_test")
        self.policy = load_module(JOB / "policy.py", "policy")
        modules = patch.dict(sys.modules, {"policy": self.policy})
        modules.start()
        self.addCleanup(modules.stop)
        self.gate = load_module(JOB / "review_gate.py", "gate_under_test")

    def review(self, pr=1085):
        return {
            "pr": pr, "head": HEAD, "base": BASE,
            "record_commit": RECORD_COMMIT,
            "pin_path": f"records/{HEAD}/pr-{pr}--{BASE}--claude-1.json", "pin_sha256": HASH,
            "ci_urls": [f"https://github.com/krshk30/project-mai-tai/actions/runs/{n}"
                        for n in (1, 2)],
        }

    def release(self):
        p, g = self.policy, self.gate
        service = {
            "MainPID": "100", "ExecMainStartTimestamp": "Sat 2026-10-03 22:03:17 UTC",
            "InvocationID": "b" * 32, "NRestarts": "0",
            "ActiveState": "active", "SubState": "running",
        }
        return {
            "schema_version": 1, "ready": True, "job": p.JOB,
            "window": dict(p.WINDOW), "denominators": dict(p.COUNTS), "flags": dict(p.FLAGS),
            "application": {
                "box_sha": BASE, "approved_sha": "c" * 40, "tree": "e" * 40,
                "rpg_merge_sha": MERGE, "coldstart_merge_sha": "c" * 40,
                "commits": [MERGE, "c" * 40], "changed_paths": ["src/example.py"],
                "source_sha256": {"src/example.py": HASH},
            },
            "plan": {"commit": "f" * 40,
                     "path": "docs/review-artifacts/rpg1/JOINT_INSTALL_PLAN_2026-10-03.md",
                     "sha256": HASH},
            "reviews": [self.review(1085), self.review(1088)],
            "host": {
                "captured_at": "2026-10-03T21:00:00-04:00",
                "services": {name: dict(service) for name in p.SERVICES},
                "tv_alerts": {"MainPID": "0", "ActiveState": "inactive"},
                "files": {name: {"sha256": p.FLAT_HASH if name == str(p.FLAT) else HASH,
                                  "uid": 0, "gid": 0, "mode": 0o600}
                          for name in g.FILE_PATHS},
                "units": {name: HASH for name in g.UNIT_NAMES},
                "subscription_inputs": {
                    name: {"symbols": [], "evidence": "independently read current empty input",
                           "sha256": HASH} for name in p.OWNERS},
                "accepted_unexplained_orders": [], "intent_ids": [],
                "status_contract": copy.deepcopy(g.STATUS_CONTRACT),
                "pager_url": "https://ntfy.sh/test-only-not-used",
            },
            "artifacts": {name: HASH for name in p.ARTIFACTS},
        }

    def pr_row(self):
        return {
            "state": "MERGED", "headRefOid": HEAD, "baseRefOid": BASE,
            "mergeCommit": {"oid": MERGE},
            "statusCheckRollup": [
                {"name": "independent-review-pin", "conclusion": "SUCCESS",
                 "completedAt": "2026-10-04T01:08:49Z"},
                *({"name": "validate", "conclusion": "SUCCESS", "detailsUrl": url}
                  for url in URLS),
            ],
        }

    def starting_host(self):
        host = self.release()["host"]
        for name, pid in START_PIDS.items():
            host["services"][name]["MainPID"] = pid
            host["services"][name]["ExecMainStartTimestamp"] = {
                "redis-server": "Sat 2026-10-03 21:41:33 UTC",
                "postgresql@16-main": "Sat 2026-10-03 21:41:36 UTC",
            }.get(name, "Sat 2026-10-03 22:03:17 UTC")
        for path, digest in START_HASHES.items():
            host["files"][path]["sha256"] = digest
        return host

    def test_known_starting_host_accepted_without_mutation(self):
        host = self.starting_host()
        before = copy.deepcopy(host)
        self.assertIsNone(self.stage.validate_start_host(host))
        self.assertEqual(host, before)
        release = self.release()
        release["host"] = host
        self.gate.validate_release(release)

    def test_each_starting_pid_drift_refused(self):
        for name in START_PIDS:
            for value in ("999999", "0", int(START_PIDS[name]), None):
                with self.subTest(service=name, pid=value):
                    host = self.starting_host()
                    host["services"][name]["MainPID"] = value
                    with self.assertRaises(RuntimeError):
                        self.stage.validate_start_host(host)

    def test_each_starting_timestamp_drift_refused(self):
        for name in START_PIDS:
            for value in ("Sat 2026-10-03 22:03:18 UTC", "", None):
                with self.subTest(service=name, start=value):
                    host = self.starting_host()
                    host["services"][name]["ExecMainStartTimestamp"] = value
                    with self.assertRaises(RuntimeError):
                        self.stage.validate_start_host(host)

    def test_each_starting_restart_drift_refused(self):
        for name in START_PIDS:
            for value in ("1", "-1", "unknown", 0, None):
                with self.subTest(service=name, restarts=value):
                    host = self.starting_host()
                    host["services"][name]["NRestarts"] = value
                    with self.assertRaises(RuntimeError):
                        self.stage.validate_start_host(host)

    def test_missing_or_extra_starting_service_refused(self):
        for missing in START_PIDS:
            with self.subTest(missing=missing):
                host = self.starting_host()
                del host["services"][missing]
                with self.assertRaises(RuntimeError):
                    self.stage.validate_start_host(host)
        host = self.starting_host()
        host["services"]["unreviewed-worker"] = dict(host["services"]["oms"])
        with self.assertRaises(RuntimeError):
            self.stage.validate_start_host(host)

    def test_each_starting_file_hash_drift_refused(self):
        for path in START_HASHES:
            for value in ("0" * 64, "", None):
                with self.subTest(path=path, digest=value):
                    host = self.starting_host()
                    host["files"][path]["sha256"] = value
                    with self.assertRaises(RuntimeError):
                        self.stage.validate_start_host(host)

    def test_missing_starting_file_refused(self):
        for path in START_HASHES:
            with self.subTest(path=path):
                host = self.starting_host()
                del host["files"][path]
                with self.assertRaises((RuntimeError, KeyError)):
                    self.stage.validate_start_host(host)

    def test_unknown_service_state_or_empty_invocation_refused_by_release_gate(self):
        for name in START_PIDS:
            for key, value in (("ActiveState", "unknown"), ("ActiveState", "failed"),
                               ("ActiveState", "activating"), ("SubState", "exited"),
                               ("SubState", "unknown"), ("InvocationID", "")):
                with self.subTest(service=name, key=key, value=value):
                    release = self.release()
                    release["host"] = self.starting_host()
                    release["host"]["services"][name][key] = value
                    with self.assertRaises(self.policy.Refusal):
                        self.gate.validate_release(release)

    def test_unit_manifest_missing_extra_or_malformed_refused(self):
        for name in self.gate.UNIT_NAMES:
            with self.subTest(unit=name):
                release = self.release()
                del release["host"]["units"][name]
                with self.assertRaises(self.policy.Refusal):
                    self.gate.validate_release(release)
        for name, value in (("unexpected.service", HASH),
                            (next(iter(self.gate.UNIT_NAMES)), "unknown")):
            with self.subTest(unit=name, digest=value):
                release = self.release()
                release["host"]["units"][name] = value
                with self.assertRaises(self.policy.Refusal):
                    self.gate.validate_release(release)

    def test_capture_hashes_actual_unit_text_including_dropins(self):
        with patch.dict(sys.modules, {"review_gate": self.gate}):
            proofs = load_module(JOB / "proofs.py", "proofs_under_test")
        host = self.starting_host()
        unit_text = {name: f"# /etc/systemd/system/{name}\n[Service]\nType=simple"
                     for name in self.gate.UNIT_NAMES}
        changed = "project-mai-tai-control.service"
        base_text = unit_text[changed]
        unit_text[changed] += (
            "\n\n# /etc/systemd/system/project-mai-tai-control.service.d/override.conf"
            "\n[Service]\nTimeoutStopSec=45"
        )

        def fake_command(*args, **kwargs):
            if args[:2] == ("systemctl", "cat"):
                self.assertEqual(len(args), 3)
                return unit_text[args[2]]
            if args == (str(proofs.REPO / ".venv/bin/python"), str(proofs.FLAT)):
                return "offline flat output"
            self.fail(f"unexpected external command in capture: {args}")

        with tempfile.TemporaryDirectory(prefix="capture-contract-test-") as temp, ExitStack() as stack:
            pinned = Path(temp) / "pinned.txt"
            pinned.write_text("offline file pin\n")
            replacements = {
                "window": lambda *a, **kw: None,
                "settings": lambda: types.SimpleNamespace(),
                "token": lambda config: {}, "durable": lambda config: {},
                "RedisRead": lambda config: object(), "redis_state": lambda reader: {},
                "broker_orders": AsyncMock(return_value={"observed": []}),
                "digest": lambda path: proofs.FLAT_HASH,
                "parse_flat": lambda raw: {}, "command": fake_command,
                "identity": lambda name: copy.deepcopy(
                    host["tv_alerts"] if name == "tv-alerts" else host["services"][name]),
                "input_proofs": lambda config, reader: host["subscription_inputs"],
                "archive_intents": lambda reader: [], "FILE_PATHS": {str(pinned)},
            }
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(proofs, name, replacement))
            captured = proofs.capture_host()
        self.assertEqual(captured["units"], {
            name: hashlib.sha256(text.encode()).hexdigest() for name, text in unit_text.items()})
        self.assertNotEqual(captured["units"][changed], hashlib.sha256(base_text.encode()).hexdigest())

    def test_main_refuses_fresh_pid_drift_before_manifest_upload_or_staging(self):
        host = self.starting_host()
        host["services"]["control"]["MainPID"] = "999999"

        def fake_command(*args, **kwargs):
            if args[0] == "ssh" and any("mktemp -d" in part for part in args):
                return b"/tmp/joint-review.OFFLINE\n"
            if args[0] == "scp":
                self.assertFalse(any(part.endswith("release.json") for part in args),
                                 "drifted host must be refused before release upload")
                return b""
            if args[0] == "ssh" and args[-1] == "capture":
                return json.dumps(host).encode()
            self.fail(f"unexpected staging or external command: {args}")

        with patch.object(self.stage, "assemble", return_value=self.release()), \
                patch.object(self.stage, "command", side_effect=fake_command), \
                patch.object(sys, "argv", ["stage_joint_review.py", "--approved-sha", "c" * 40]):
            with self.assertRaisesRegex(RuntimeError, "identity drift"):
                self.stage.main()

    def merged_review(self, row=None):
        row = row or self.pr_row()
        record = {"pr_number": 1085, "range_head": HEAD, "range_base": BASE,
                  "reviewer": "claude-1"}

        def fake_command(*args, **kwargs):
            self.assertEqual(args[:4], ("gh", "pr", "view", "1085"))
            return json.dumps(row).encode()

        def fake_git(*args):
            if args[0] == "log":
                return RECORD_COMMIT
            self.assertEqual(args[0], "rev-parse")
            return "e" * 40 if args[-1].endswith("^{tree}") else RECORD_COMMIT

        with patch.object(self.stage, "command", side_effect=fake_command), \
                patch.object(self.stage, "git", side_effect=fake_git), \
                patch.object(self.stage, "blob", return_value=json.dumps(record).encode()) as blob:
            result = self.stage.merged_review(1085, HEAD, BASE)
        return result, blob.call_args_list

    def test_valid_release_fixture(self):
        self.gate.validate_release(self.release())

    def test_template_cannot_be_ready(self):
        release = self.release()
        release["ready"] = False
        with self.assertRaises(self.policy.Refusal):
            self.gate.validate_release(release)

    def test_136_cannot_drop_coldstart_readers(self):
        release = self.release()
        release["denominators"]["combined"] = 136
        with self.assertRaises(self.policy.Refusal):
            self.gate.validate_release(release)

    def test_manifest_pins_immutable_ledger_commit(self):
        (_, review), calls = self.merged_review()
        self.assertEqual(review.get("record_commit"), RECORD_COMMIT)
        self.assertTrue(any(call.args[0] == RECORD_COMMIT for call in calls),
                        "manifest digest must come from its immutable record_commit")

    def test_real_ledger_path_accepted(self):
        release = self.release()
        release["reviews"][0]["pin_path"] = f"records/{HEAD}/pr-1085--{BASE}--claude-1.json"
        self.gate.validate_release(release)

    def test_obsolete_docs_ledger_path_refused(self):
        release = self.release()
        release["reviews"][0]["pin_path"] = "docs/review-pins/1085.json"
        with self.assertRaises(self.policy.Refusal):
            self.gate.validate_release(release)

    def test_actual_validate_job_urls_normalized(self):
        (_, review), _ = self.merged_review()
        self.assertEqual(review["ci_urls"], [url.split("/job/", 1)[0] for url in URLS])

    def test_stager_review_passes_runner_schema(self):
        (_, review), _ = self.merged_review()
        release = self.release()
        release["reviews"][0] = review
        self.gate.validate_release(release)

    def test_wrong_review_identity_refused(self):
        for key, value in (("state", "OPEN"), ("headRefOid", "0" * 40),
                           ("baseRefOid", "1" * 40)):
            with self.subTest(key=key):
                row = self.pr_row()
                row[key] = value
                with self.assertRaises(RuntimeError):
                    self.merged_review(row)

    def test_latest_failed_pin_refused(self):
        row = self.pr_row()
        row["statusCheckRollup"].append({
            "name": "independent-review-pin", "conclusion": "FAILURE",
            "completedAt": "2026-10-04T01:09:00Z"})
        with self.assertRaises(RuntimeError):
            self.merged_review(row)

    def test_failed_or_missing_validate_refused(self):
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                row = self.pr_row()
                if incomplete:
                    row["statusCheckRollup"].pop()
                else:
                    row["statusCheckRollup"][-1]["conclusion"] = "FAILURE"
                with self.assertRaises(RuntimeError):
                    self.merged_review(row)

    def assemble(self, directory, *, dirty=False, drift=None, approved=None, cold_readers=4):
        approved = approved or "c" * 40
        source = {"src/a.py": b"source a\n", "src/package/b.py": b"source b\n"}
        template = self.release()
        template["ready"] = False
        flags = [{"name": f"existing_{n}", "expected": True,
                  "also_check_services": ["oms"] if n < 4 else []} for n in range(122)]
        flags.extend([
            {"name": "market_data_subscription_startup_enabled", "expected": True,
             "also_check_services": ["schwab-1m-v2", "orb", "orb-schwab", "momentum-paper"][:cold_readers]},
            {"name": "strategy_schwab_1m_v2_atr_reprice_handoff_enabled", "expected": True},
        ])
        numeric = [{"name": f"numeric_{n}",
                    "also_check_services": ["oms"] if n < 3 else []} for n in range(5)]

        def fake_git(*args):
            if args == ("status", "--porcelain"):
                return " M owned-by-another-writer.py" if dirty else ""
            if args == ("rev-parse", "HEAD"):
                return "f" * 40
            if args == ("diff", "--name-only", approved, "origin/main"):
                return "\n".join(drift or [])
            if args == ("ls-tree", "-r", "--name-only", approved, "src/"):
                return "\n".join(source)
            if args == ("rev-parse", f"{approved}^{{tree}}"):
                return "e" * 40
            if args == ("rev-list", "--reverse", f"{BASE}..{approved}"):
                return "\n".join((MERGE, approved))
            if args == ("diff", "--name-only", BASE, approved):
                return "ops/health/expected_flags.json\nsrc/a.py"
            self.fail(f"unexpected git call: {args}")

        def fake_blob(revision, path):
            if path == "ops/health/expected_flags.json":
                self.assertEqual(revision, approved)
                return json.dumps({"flags": flags}).encode()
            if path == "ops/health/expected_numeric.json":
                self.assertEqual(revision, approved)
                return json.dumps({"settings": numeric}).encode()
            if path.endswith("/release.json"):
                return json.dumps(template).encode()
            if path.startswith("src/"):
                self.assertEqual(revision, approved)
                return source[path]
            self.assertEqual(revision, "f" * 40)
            return ("committed artifact " + path).encode()

        def fake_command(*args, **kwargs):
            self.assertIn(args, (("git", "fetch", "origin", "main", "review-pins"),
                                 ("git", "merge-base", "--is-ancestor", approved, "origin/main")))
            return b""

        def fake_review(pr, head, base):
            self.assertEqual((head, base), (HEAD, BASE) if pr == 1085
                             else (self.stage.COLD_HEAD, MERGE))
            return (MERGE if pr == 1085 else approved), self.review(pr)

        with patch.object(self.stage, "git", side_effect=fake_git), \
                patch.object(self.stage, "blob", side_effect=fake_blob), \
                patch.object(self.stage, "command", side_effect=fake_command), \
                patch.object(self.stage, "merged_review", side_effect=fake_review):
            return self.stage.assemble(approved, directory), source

    def test_assemble_binds_nine_artifacts_plan_and_every_source(self):
        with tempfile.TemporaryDirectory(prefix="assemble-contract-test-") as temp:
            root = Path(temp)
            release, source = self.assemble(root)
            self.assertEqual(set(release["artifacts"]), set(self.policy.ARTIFACTS))
            self.assertEqual(len(release["artifacts"]), 9)
            for name, digest in release["artifacts"].items():
                self.assertEqual(digest, hashlib.sha256((root / name).read_bytes()).hexdigest())
            self.assertEqual(release["application"]["source_sha256"], {
                path: hashlib.sha256(data).hexdigest() for path, data in source.items()})
            self.assertEqual(release["plan"]["sha256"],
                             hashlib.sha256((root / self.stage.PLAN).read_bytes()).hexdigest())
            self.assertFalse((root / "approval.json").exists())

    def test_dirty_plan_refused_before_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="assemble-contract-test-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(RuntimeError, "clean committed"):
                self.assemble(root, dirty=True)
            self.assertEqual(list(root.iterdir()), [])

    def test_main_code_drift_refused_before_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="assemble-contract-test-") as temp:
            root = Path(temp)
            with self.assertRaisesRegex(RuntimeError, "non-docs"):
                self.assemble(root, drift=["src/unreviewed.py"])
            self.assertEqual(list(root.iterdir()), [])

    def test_main_docs_only_drift_allowed(self):
        with tempfile.TemporaryDirectory(prefix="assemble-contract-test-") as temp:
            release, _ = self.assemble(Path(temp), drift=["docs/review-note.md"])
            self.assertEqual(release["application"]["approved_sha"], "c" * 40)

    def test_branch_tip_refused_before_commands(self):
        with tempfile.TemporaryDirectory(prefix="assemble-contract-test-") as temp:
            with self.assertRaisesRegex(RuntimeError, "full application SHA"):
                self.assemble(Path(temp), approved="origin/main")

    def test_assemble_rejects_catalog_missing_four_consumer_readers(self):
        with tempfile.TemporaryDirectory(prefix="assemble-contract-test-") as temp:
            with self.assertRaisesRegex(RuntimeError, "denominator"):
                self.assemble(Path(temp), cold_readers=0)

    def stage_preflight(self, *, invalid_host=False, extra_validator=""):
        # Execute only the inline Python validator, never the surrounding shell.
        match = re.search(r"(?m)^([^\n]+)<<'PY'\n(.*?)\nPY$", self.stage.STAGE, re.S)
        self.assertIsNotNone(match, "update harness if the staging validator CLI changes")
        words = shlex.split(match.group(1))
        flags = words[1:words.index("-")]
        with tempfile.TemporaryDirectory(prefix="stage-contract-test-") as temp:
            root = Path(temp)
            release = self.release()
            for name in self.policy.ARTIFACTS:
                if name in {"policy.py", "review_gate.py"}:
                    payload = (JOB / name).read_bytes()
                else:
                    payload = b"offline test artifact\n"
                if name == "policy.py":
                    payload += b"\n# Freeze only the clock guard in this offline fixture.\nwindow = lambda *args, **kwargs: None\n"
                (root / name).write_bytes(payload)
                release["artifacts"][name] = hashlib.sha256(payload).hexdigest()
            (root / self.stage.PLAN).write_bytes(b"offline plan\n")
            release["plan"]["sha256"] = hashlib.sha256(b"offline plan\n").hexdigest()
            if invalid_host:
                release["host"]["services"] = {}
            (root / "release.json").write_text(json.dumps(release))
            # Guard inside the child interpreter, where inline staging code runs.
            guard = textwrap.dedent("""\
                import subprocess
                from unittest.mock import patch

                def offline_run(args, **kwargs):
                    prefix = ['sudo', '-u', 'trader', 'git', '-C',
                              '/home/trader/project-mai-tai', 'fetch', '--no-tags', 'origin']
                    if (not isinstance(args, list) or args[:len(prefix)] != prefix
                            or not args[len(prefix):]
                            or any(not isinstance(ref, str) or len(ref) != 40
                                   or any(char not in '0123456789abcdef' for char in ref)
                                   for ref in args[len(prefix):])
                            or kwargs != {'check': True, 'timeout': 120}):
                        raise AssertionError('unmocked staging subprocess refused: ' + repr(args))
                    return subprocess.CompletedProcess(args, 0)

                with patch('subprocess.run', side_effect=offline_run), \\
                     patch('subprocess.Popen', side_effect=AssertionError('process spawn forbidden')):
                """)
            guarded = guard + textwrap.indent(match.group(2) + "\n" + extra_validator, "    ")
            # A clean environment deliberately does not inherit the test driver's -B.
            result = subprocess.run(
                [sys.executable, *flags, "-", str(root)], input=guarded,
                text=True, capture_output=True, timeout=10,
                env={"PATH": "/usr/bin:/bin"}, cwd=temp,
            )
            nonfiles = [path.name for path in root.iterdir() if not path.is_file()]
            return result, nonfiles

    def test_remote_preflight_leaves_no_bytecode_directory(self):
        result, nonfiles = self.stage_preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(nonfiles, [], "copy loop rejects directories after creating the job")

    def test_invalid_host_rejected_before_install(self):
        result, _ = self.stage_preflight(invalid_host=True)
        self.assertNotEqual(result.returncode, 0, "staging must validate host/release before install")

    def test_inline_exact_object_fetch_is_mocked(self):
        result, _ = self.stage_preflight(extra_validator=(
            "subprocess.run(['sudo', '-u', 'trader', 'git', '-C', "
            "'/home/trader/project-mai-tai', 'fetch', '--no-tags', 'origin', "
            "'c' * 40], check=True, timeout=120)\n"
        ))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_inline_unexpected_command_cannot_spawn(self):
        result, _ = self.stage_preflight(extra_validator="subprocess.run(['ssh', 'forbidden'])\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unmocked staging subprocess refused", result.stderr)

    def test_inline_alternate_subprocess_api_cannot_spawn(self):
        result, _ = self.stage_preflight(extra_validator="subprocess.Popen(['ssh', 'forbidden'])\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("process spawn forbidden", result.stderr)


if __name__ == "__main__":
    unittest.main()
