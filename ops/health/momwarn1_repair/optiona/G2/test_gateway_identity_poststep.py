"""Offline restart, drift and publication controls; no remote or service actions."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import gateway_identity_poststep as ops

G1 = Path(__file__).resolve().parents[1] / "G1" / "daily_guard.py"


def pin(pid, ticks, stamp):
    return {"version": 1, "unit": ops.UNIT, "main_pid": pid, "start_ticks": ticks,
            "exec_main_start_timestamp": stamp}


def state(identity):
    return {"MainPID": str(identity["main_pid"]), "ActiveState": "active", "NRestarts": "0",
            "ExecMainStartTimestamp": identity["exec_main_start_timestamp"]}


class PoststepControls(unittest.TestCase):
    def repair(self):
        manifest = deepcopy(self.manifest)
        manifest["gateway_identity"] = self.new_pin
        old = (json.dumps(manifest) + "\n").encode()
        request = dict(self.request, operation="reviewed_guard_source_repair",
            guard_source_repair_authorized=True, manifest_before_sha256=ops.sha(old),
            before_gateway_identity=self.new_pin, after_gateway_identity=self.new_pin)
        for stem in ("guard_rules", "sampler"):
            request[stem + "_before_sha256"] = manifest["artifacts"][stem + ".py"]
            raw = ("CONTROLLED reviewed " + stem).encode()
            (self.root / (stem + ".py")).write_bytes(raw)
            request["reviewed_" + stem + "_sha256"] = ops.sha(raw)
        return old, request

    def test_authorized_same_identity_source_repair_three_explicit_hashes_only(self):
        old, request = self.repair()
        original = json.loads(old)
        result = json.loads(ops.proposal(self.root, request, old, self.new_pin))
        self.assertEqual(result["gateway_identity"], original["gateway_identity"])
        self.assertEqual(result["application_sha"], original["application_sha"])
        for name, value in original["artifacts"].items():
            if name not in {"daily_guard.py", "guard_rules.py", "sampler.py"}:
                self.assertEqual(result["artifacts"][name], value)

    def test_source_repair_refuses_missing_authority_missing_preimages_or_new_identity(self):
        old, request = self.repair()
        for change in ({"guard_source_repair_authorized": False}, {"sampler_before_sha256": None},
                       {"reviewed_sampler_sha256": None}, {"guard_rules_before_sha256": "0" * 64},
                       {"before_gateway_identity": self.old_pin}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                ops.proposal(self.root, dict(request, **change), old, self.new_pin)

    def test_sampler_cannot_be_blindly_rebaselined_in_ordinary_restart(self):
        (self.root / "sampler.py").write_bytes(b"unsolicited drift")
        with self.assertRaisesRegex(ValueError, "sampler.py"):
            self.build()

    def test_explicit_two_source_window_cut_preserves_all_other_pins(self):
        original = json.loads(self.old)
        rules = self.root / "guard_rules.py"
        self.request["guard_rules_before_sha256"] = original["artifacts"]["guard_rules.py"]
        rules.write_bytes(b"CONTROLLED reviewed date-window rules")
        self.request["reviewed_guard_rules_sha256"] = ops.sha(rules.read_bytes())
        result = ops.proposal(self.root, self.request, self.old, self.new_pin)
        proposed = json.loads(result)
        for name, value in original["artifacts"].items():
            if name not in {"daily_guard.py", "guard_rules.py"}:
                self.assertEqual(proposed["artifacts"][name], value)
        self.assertEqual(proposed["artifacts"]["guard_rules.py"], self.request["reviewed_guard_rules_sha256"])
        self.assertEqual(proposed["application_sha"], original["application_sha"])

    def test_rules_cut_requires_both_reviewed_hash_and_exact_old_preimage(self):
        for values in ({"reviewed_guard_rules_sha256": "0" * 64},
                       {"guard_rules_before_sha256": "0" * 64},
                       {"reviewed_guard_rules_sha256": "0" * 64, "guard_rules_before_sha256": "0" * 64}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                ops.proposal(self.root, dict(self.request, **values), self.old, self.new_pin)
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "guard"
        self.root.mkdir()
        self.backup = Path(self.temp.name) / "backup"
        self.backup.mkdir()
        self.proc = Path(self.temp.name) / "proc"
        self.old_pin = pin(2907, 100, "Sat 2026-10-03 22:03:17 UTC")
        self.new_pin = pin(3036178, 79383409, "Tue 2026-10-13 02:11:59 UTC")
        self.old_source = b"reviewed previous daily guard"
        self.source = G1.read_bytes()
        artifacts = {}
        for name in ops.REQUIRED_ARTIFACTS:
            raw = self.source if name == "daily_guard.py" else ("pinned fixture " + name).encode()
            (self.root / name).write_bytes(raw)
            artifacts[name] = ops.sha(self.old_source if name == "daily_guard.py" else raw)
        self.manifest = {"application_sha": "legacy-application-sha", "authority": "unchanged operator authority",
                         "artifacts": artifacts, "unrelated_service_identity": {"oms_pid": 3051895}}
        self.old = (json.dumps(self.manifest, indent=2) + "\n").encode()
        (self.root / "manifest.json").write_bytes(self.old)
        (self.root / "manifest.json").chmod(0o640)
        self.request = {"version": 1, "install_id": "fixture-approved-gateway-restart",
                        "gateway_restart_authorized": True, "restart_completed": True,
                        "manifest_before_sha256": ops.sha(self.old),
                        "daily_guard_before_sha256": ops.sha(self.old_source),
                        "reviewed_daily_guard_sha256": ops.sha(self.source),
                        "before_gateway_identity": self.old_pin,
                        "after_gateway_identity": self.new_pin,
                        "post_checks": {"identity": "PASS", "flags": "PASS", "trading": "PASS"}}
        self.write_stat()

    def write_stat(self, identity=None, process_state="S", name="gateway (fixture)"):
        identity = identity or self.new_pin
        directory = self.proc / str(identity["main_pid"])
        directory.mkdir(parents=True, exist_ok=True)
        fields = [process_state] + ["0"] * 18 + [str(identity["start_ticks"])] + ["0"] * 30
        (directory / "stat").write_text(str(identity["main_pid"]) + " (" + name + ") " + " ".join(fields))

    def capture(self, reader=None):
        return ops.capture(reader or (lambda: state(self.new_pin)), self.proc)

    def build(self, request=None, old=None):
        return ops.proposal(self.root, request or self.request, old or self.old, self.capture())

    def publish(self, reader=None):
        return ops.publish(self.root, self.request, self.backup,
                           reader=reader or (lambda: state(self.new_pin)), proc_root=self.proc)

    def assert_refused(self, request=None, old=None):
        with self.assertRaises((ValueError, OSError)):
            self.build(request, old)
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)

    def test_original_case_2907_to_actual_gateway_and_g1_accepts(self):
        result = self.publish()
        current = json.loads((self.root / "manifest.json").read_bytes())
        self.assertEqual(current["gateway_identity"], self.new_pin)
        self.assertEqual((self.backup / (self.request["install_id"] + "-manifest-before.json")).read_bytes(), self.old)
        self.assertEqual(result["status"], "PUBLISHED")
        spec = importlib.util.spec_from_file_location("g1_guard_control", G1)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.verify_gateway_identity(current, state(self.new_pin), proc_root=self.proc)
        self.assertEqual((self.root / "manifest.json").stat().st_mode & 0o777, 0o640)

    def test_preserves_all_unrelated_manifest_and_artifact_pins(self):
        new = json.loads(self.build())
        expected = deepcopy(self.manifest)
        expected["gateway_identity"] = self.new_pin
        expected["artifacts"]["daily_guard.py"] = ops.sha(self.source)
        self.assertEqual(new, expected)

    def test_subsequent_restart_uses_new_actual_identity(self):
        old = deepcopy(self.manifest)
        old["gateway_identity"] = self.old_pin
        raw = json.dumps(old).encode()
        request = deepcopy(self.request)
        request["manifest_before_sha256"] = ops.sha(raw)
        self.assertEqual(json.loads(self.build(request, raw))["gateway_identity"], self.new_pin)

    def test_preview_does_not_write_manifest_or_backup(self):
        self.build()
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)
        self.assertEqual(list(self.backup.iterdir()), [])

    def test_explicit_authorization_and_completed_restart_required(self):
        for field in ("gateway_restart_authorized", "restart_completed"):
            with self.subTest(field=field):
                request = deepcopy(self.request)
                request[field] = False
                self.assert_refused(request)

    def test_unknown_or_failed_post_checks_refused(self):
        for name in ("identity", "flags", "trading"):
            for value in (None, "UNKNOWN", "FAIL", 0):
                with self.subTest(name=name, value=value):
                    request = deepcopy(self.request)
                    request["post_checks"][name] = value
                    self.assert_refused(request)

    def test_no_restart_or_stale_after_receipt_refused(self):
        request = deepcopy(self.request)
        request["before_gateway_identity"] = self.new_pin
        self.assert_refused(request)
        request = deepcopy(self.request)
        request["after_gateway_identity"]["start_ticks"] += 1
        self.assert_refused(request)

    def test_same_pid_reuse_and_clock_regression_refused(self):
        for pid, ticks in ((self.new_pin["main_pid"], 10), (2907, 79383410)):
            request = deepcopy(self.request)
            request["before_gateway_identity"]["main_pid"] = pid
            request["before_gateway_identity"]["start_ticks"] = ticks
            self.assert_refused(request)

    def test_missing_malformed_or_boolean_identity_refused(self):
        for key, value in (("main_pid", True), ("main_pid", 0), ("start_ticks", False),
                           ("exec_main_start_timestamp", ""), ("unit", "other.service"), ("version", 2)):
            request = deepcopy(self.request)
            request["after_gateway_identity"][key] = value
            self.assert_refused(request)

    def test_unreviewed_guard_and_unrelated_artifact_drift_refused(self):
        for name in ("daily_guard.py", "guard_rules.py", "sampler.py"):
            with self.subTest(name=name):
                saved = (self.root / name).read_bytes()
                (self.root / name).write_bytes(b"unexpected source drift")
                self.assert_refused()
                (self.root / name).write_bytes(saved)

    def test_source_preimage_and_manifest_preimage_drift_refused(self):
        for field in ("manifest_before_sha256", "daily_guard_before_sha256"):
            request = deepcopy(self.request)
            request[field] = "0" * 64
            self.assert_refused(request)

    def test_existing_identity_drift_not_silently_rebased(self):
        old = deepcopy(self.manifest)
        old["gateway_identity"] = pin(99, 77, "unexpected baseline")
        raw = json.dumps(old).encode()
        request = deepcopy(self.request)
        request["manifest_before_sha256"] = ops.sha(raw)
        self.assert_refused(request, raw)

    def test_artifact_path_escape_and_missing_pin_refused(self):
        for change in ("escape", "missing"):
            old = deepcopy(self.manifest)
            if change == "escape":
                old["artifacts"]["../escape.py"] = "0" * 64
            else:
                old["artifacts"].pop("sampler.py")
            raw = json.dumps(old).encode()
            request = deepcopy(self.request)
            request["manifest_before_sha256"] = ops.sha(raw)
            self.assert_refused(request, raw)

    def test_dead_missing_and_malformed_proc_refused(self):
        for process_state in ("Z", "X", "x"):
            self.write_stat(process_state=process_state)
            with self.assertRaises(ValueError):
                self.capture()
        (self.proc / str(self.new_pin["main_pid"]) / "stat").unlink()
        with self.assertRaises(OSError):
            self.capture()

    def test_active_state_restart_count_and_empty_pid_refused(self):
        for name, value in (("ActiveState", "inactive"), ("NRestarts", "1"), ("NRestarts", ""), ("MainPID", "0")):
            row = state(self.new_pin)
            row[name] = value
            with self.assertRaises(ValueError):
                self.capture(lambda: row)

    def test_systemd_race_refused(self):
        rows = [state(self.new_pin), state(pin(99, 22, "raced"))]
        with self.assertRaises(ValueError):
            self.capture(lambda: rows.pop(0))

    def test_process_dies_during_capture_refused(self):
        calls = 0
        def reader():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.write_stat(process_state="Z")
            return state(self.new_pin)
        with self.assertRaises(ValueError):
            self.capture(reader)

    def test_manifest_race_does_not_overwrite_other_writer(self):
        raced = b'{"external_writer": "retain"}'
        calls = 0
        def reader():
            nonlocal calls
            calls += 1
            if calls == 3:
                (self.root / "manifest.json").write_bytes(raced)
            return state(self.new_pin)
        with self.assertRaises(ValueError):
            self.publish(reader)
        self.assertEqual((self.root / "manifest.json").read_bytes(), raced)

    def test_artifact_race_before_publication_refused(self):
        calls = 0
        def reader():
            nonlocal calls
            calls += 1
            if calls == 3:
                (self.root / "guard_rules.py").write_bytes(b"unexpected drift during capture")
            return state(self.new_pin)
        with self.assertRaises(ValueError):
            self.publish(reader)
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)

    def test_noop_service_runner_or_guard_main_called(self):
        with patch.object(ops.subprocess, "check_output", side_effect=AssertionError("no external calls")):
            self.publish()

    def test_publication_race_preserves_manifest(self):
        calls = 0
        def reader():
            nonlocal calls
            calls += 1
            if calls == 3:
                return state(pin(99, 22, "raced before publication"))
            return state(self.new_pin)
        with self.assertRaises((ValueError, OSError)):
            self.publish(reader)
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)

    def test_existing_backup_refused_and_not_overwritten(self):
        backup = self.backup / (self.request["install_id"] + "-manifest-before.json")
        backup.write_bytes(b"retain original evidence")
        with self.assertRaises(FileExistsError):
            self.publish()
        self.assertEqual(backup.read_bytes(), b"retain original evidence")
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)


if __name__ == "__main__":
    unittest.main(verbosity=2)
