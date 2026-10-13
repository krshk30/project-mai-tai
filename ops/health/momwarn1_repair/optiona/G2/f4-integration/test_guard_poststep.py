"""Offline G2 integration controls using actual updated G1 read-only program."""
from contextlib import redirect_stdout
from datetime import UTC, datetime
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import guard_poststep as integration

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_gateway_identity_poststep as legacy


class IntegrationControls(legacy.PoststepControls):
    def setUp(self):
        super().setUp()
        self.request["afterclose_install_authorized"] = True
        self.request["prerequisite_receipts"] = {}
        for key in ("F2_permission_apply", "F3_actual_trader_read"):
            # Synthetic receipt fixtures only; these are not box evidence.
            value = {"status": "PASS", "actual_box": True}
            if key.startswith("F3"):
                value.update(service_user="trader", read_live_calls=2, exceptions=[], own_service_environment=True)
            path = Path(self.temp.name) / (key + ".json")
            raw = json.dumps(value).encode()
            path.write_bytes(raw)
            self.request["prerequisite_receipts"][key] = {"path": str(path), "sha256": integration.ops.sha(raw)}
        self.calls = []

    def actual_guard_dryrun(self, command, **kwargs):
        self.assertEqual(command[-1], "--dry-run-prestart")
        self.assertEqual(command[-2], str(self.root / "daily_guard.py"))
        spec = importlib.util.spec_from_file_location("controlled_actual_G1", self.root / "daily_guard.py")
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        guard.ROOT = self.root
        owners = {name: set() for name in ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper", "momentum-warning")}
        redis = SimpleNamespace(close=lambda: self.calls.append("redis_closed"))
        rules = SimpleNamespace(
            Blind=RuntimeError, HEARTBEAT_AGE_BOUND=10, _owners=lambda *_: owners,
            RedisSafety=lambda _: SimpleNamespace(sample=lambda: (None, {})),
            LiveSignals=lambda *_: SimpleNamespace(heartbeat=lambda _: ("healthy", 1, {})),
            _audit=lambda *_: self.fail("dry-run wrote audit"),
            run_guard=lambda *_: self.fail("dry-run started sampler"),
            stop_paper=lambda *_: self.fail("dry-run stopped/started paper"))
        settings = SimpleNamespace(redis_url="offline-fixture", redis_stream_prefix="mai_tai",
                                   market_data_momentum_warning_enabled=True)
        modules = {
            "redis": SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **k: redis)),
            "project_mai_tai.settings": SimpleNamespace(Settings=lambda **k: settings),
            "project_mai_tai.strategy_core.time_utils": SimpleNamespace(US_MARKET_HOLIDAYS=set())}
        verify = guard.verify_gateway_identity
        buffer = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(guard, "load_guard", return_value=rules), \
                patch.object(guard, "state", return_value=legacy.state(self.new_pin)), \
                patch.object(guard, "verify_gateway_identity", side_effect=lambda m, s: verify(m, s, proc_root=self.proc)), \
                redirect_stdout(buffer):
            rc = guard.main(["--dry-run-prestart"])
        self.calls.append("dryrun")
        return SimpleNamespace(returncode=rc, stdout=buffer.getvalue(), stderr="")

    def complete(self, runner=None):
        return integration.finish(self.root, self.request, self.backup,
                                  reader=lambda: legacy.state(self.new_pin), proc_root=self.proc,
                                  run=runner or self.actual_guard_dryrun)

    def test_publish_then_actual_G1_readonly_prestart_complete(self):
        self.assertEqual(integration.ops.sha(self.source), integration.G1_SHA)
        result = self.complete()
        self.assertEqual(result["status"], "IDENTITY_AND_PRESTART_PASS")
        self.assertEqual(result["future_session_health"], "NOT_ASSERTED")
        self.assertFalse(result["sampler_or_paper_start"])
        self.assertEqual(self.calls, ["redis_closed", "dryrun"])
        self.assertFalse((self.root / "2026-10-13").exists())

    def test_failed_dryrun_never_claims_guard_admission_PASS(self):
        result = self.complete(lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="controlled refusal"))
        self.assertEqual(result["status"], "IDENTITY_PUBLISHED_DRYRUN_FAILED")
        self.assertEqual(result["guard_admission"], "UNKNOWN")
        self.assertNotIn("controlled refusal", json.dumps(result))

    def test_missing_malformed_or_wrong_process_positive_receipt_refused(self):
        for raw in ("not-json", "{}", json.dumps({"status": "PASS", "dry_run_prestart": False}),
                    json.dumps({"status": "PASS", "dry_run_prestart": True, "gateway": {"MainPID": "2907"}})):
            with self.subTest(raw=raw):
                # Reset manifest/backup between independently controlled publications.
                (self.root / "manifest.json").write_bytes(self.old)
                backup = self.backup / (self.request["install_id"] + "-manifest-before.json")
                if backup.exists():
                    backup.unlink()
                result = self.complete(lambda *a, **k: SimpleNamespace(returncode=0, stdout=raw, stderr=""))
                self.assertEqual(result["status"], "IDENTITY_PUBLISHED_DRYRUN_FAILED")

    def test_unknown_or_wrong_user_F3_stops_before_publication(self):
        reference = self.request["prerequisite_receipts"]["F3_actual_trader_read"]
        for change in ({"status": "UNKNOWN"}, {"actual_box": False}, {"service_user": "root"},
                       {"own_service_environment": False}, {"read_live_calls": 1}, {"exceptions": ["PermissionError"]}):
            row = dict(status="PASS", actual_box=True, service_user="trader", own_service_environment=True,
                       read_live_calls=2, exceptions=[])
            row.update(change)
            raw = json.dumps(row).encode()
            Path(reference["path"]).write_bytes(raw)
            reference["sha256"] = integration.ops.sha(raw)
            with self.assertRaises(ValueError):
                self.complete()
            self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)
            self.assertEqual(list(self.backup.iterdir()), [])

    def test_prerequisite_receipt_hash_drift_refused(self):
        self.request["prerequisite_receipts"]["F2_permission_apply"]["sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.complete()
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)

    def test_old_guard_hash_cannot_skip_new_owner_dryrun_source(self):
        self.request["reviewed_daily_guard_sha256"] = "48c26a01eabb05585176b745c7cb11d9d0066e9623017dbaaffdedc679eb0f62"
        with self.assertRaises(ValueError):
            self.complete()
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)

    def test_process_race_after_dryrun_refused(self):
        def race(*args, **kwargs):
            result = self.actual_guard_dryrun(*args, **kwargs)
            changed = dict(self.new_pin, start_ticks=self.new_pin["start_ticks"] + 1)
            self.write_stat(changed)
            return result
        self.assertEqual(self.complete(race)["status"], "IDENTITY_PUBLISHED_DRYRUN_FAILED")

    def test_strict_afterclose_stage_order(self):
        seen = []
        def stage(name, status):
            def run():
                seen.append(name)
                return {"status": status}
            return run
        result = integration.ordered_afterclose(stage("permissions", "PASS"), stage("F3", "PASS"),
                     stage("gateway_restart", "PASS"), stage("identity_dryrun", "IDENTITY_AND_PRESTART_PASS"))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(seen, ["permissions", "F3", "gateway_restart", "identity_dryrun"])

    def test_failed_F3_prevents_gateway_restart_callback(self):
        callbacks = []
        def forbidden():
            callbacks.append("must-not-run")
            self.fail("later stage ran after failed F3")
        with self.assertRaises(ValueError):
            integration.ordered_afterclose(lambda: {"status": "PASS"}, lambda: {"status": "UNKNOWN"}, forbidden, forbidden)
        self.assertEqual(callbacks, [])

    def test_recorded_restart_mode_accepts_logged_open_exposure_not_an_inferred_flat_gate(self):
        path = Path(self.temp.name) / "before-test.json"
        row = {"before_gateway_identity": self.request["before_gateway_identity"],
               "positions_per_account": {"schwab": [{"symbol": "CONTROLLED", "qty": 2}]},
               "working_orders_per_account": {"webull": [{"client_order_id": "CONTROLLED"}]}}
        raw = json.dumps(row).encode()
        path.write_bytes(raw)
        self.request.update(recorded_restart_test_authorized=True,
                            restart_test_before={"path": str(path), "sha256": integration.ops.sha(raw)})
        integration.install_scope(self.request, recorded_test=True,
                                  now=datetime(2026, 10, 13, 12, tzinfo=UTC))
        self.assertEqual((self.root / "manifest.json").read_bytes(), self.old)
        for now in (datetime(2026, 10, 14, 12, tzinfo=UTC), datetime(2026, 10, 13, 13, 30, tzinfo=UTC)):
            with self.assertRaises(ValueError):
                integration.install_scope(self.request, recorded_test=True, now=now)
        path.write_bytes(raw + b" ")
        with self.assertRaises(ValueError):
            integration.install_scope(self.request, recorded_test=True,
                                      now=datetime(2026, 10, 13, 12, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main(verbosity=2)
