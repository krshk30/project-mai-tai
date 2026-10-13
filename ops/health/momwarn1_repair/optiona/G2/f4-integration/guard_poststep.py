#!/usr/bin/env python3
"""Future reviewed restart post-step: identity publication then read-only G1 checks."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

OPS_PATH = Path(__file__).resolve().parents[1] / "gateway_identity_poststep.py"
OPS_SHA = "51c3ea010375b668496f7d81fda570d0f3fdf8a7597c2bb0d77280477a89b8d3"
G1_SHA = "0911ad42c491a52b5ac5502afd6a9c190d8e5c375e4966ff00ef38a0b2001c90"
if hashlib.sha256(OPS_PATH.read_bytes()).hexdigest() != OPS_SHA:
    raise RuntimeError("frozen G2 publisher source drift")
spec = importlib.util.spec_from_file_location("frozen_g2_publisher", OPS_PATH)
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


def prerequisite_evidence(request):
    """Require retained actual stage receipts; the installer remains the sole authority."""
    refs = request.get("prerequisite_receipts")
    ops.need(isinstance(refs, dict), "F2/F3 actual stage receipts required")
    for key in ("F2_permission_apply", "F3_actual_trader_read"):
        ref = refs.get(key)
        ops.need(isinstance(ref, dict) and isinstance(ref.get("path"), str), "missing stage receipt: " + key)
        raw = ops.bounded_read(Path(ref["path"]))
        ops.need(ops.sha(raw) == ops.digest(ref.get("sha256")), "stage receipt hash mismatch: " + key)
        row = json.loads(raw)
        ops.need(row.get("status") == "PASS" and row.get("actual_box") is True,
                 "stage not actual successful box evidence: " + key)
        if key == "F3_actual_trader_read":
            ops.need(row.get("service_user") == "trader" and row.get("read_live_calls") == 2
                     and type(row.get("read_live_calls")) is int and row.get("exceptions") == []
                     and row.get("own_service_environment") is True,
                     "F3 service-user/environment/two-read evidence incomplete")


def finish(root, request, backup_dir, *, reader=None, proc_root=Path("/proc"), run=None):
    prerequisite_evidence(request)
    ops.need(request.get("reviewed_daily_guard_sha256") == G1_SHA,
             "G1 owner-compatible dry-run source hash not reviewed")
    published = ops.publish(root, request, backup_dir, reader=reader, proc_root=proc_root)
    installed = root / "daily_guard.py"
    ops.need(ops.sha(ops.bounded_read(installed)) == G1_SHA, "G1 source changed before dry-run")
    runner = run or subprocess.run
    # This flag enters G1's read-only branch; never call the ordinary session main.
    result = runner([sys.executable, "-B", str(installed), "--dry-run-prestart"],
                    capture_output=True, text=True, timeout=30)
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    failure = dict(published, status="IDENTITY_PUBLISHED_DRYRUN_FAILED", guard_admission="UNKNOWN",
                   dryrun_rc=result.returncode, stdout_sha256=ops.sha(stdout.encode()),
                   stderr_sha256=ops.sha(stderr.encode()))
    if result.returncode != 0 or len(stdout.encode()) > 8192:
        return failure
    try:
        receipt = json.loads(stdout)
        ops.need(isinstance(receipt, dict) and receipt.get("status") == "PASS"
                 and receipt.get("dry_run_prestart") is True, "dry-run positive receipt missing")
        gateway = receipt.get("gateway")
        pin = published["gateway_identity"]
        ops.need(isinstance(gateway, dict) and gateway.get("MainPID") == str(pin["main_pid"])
                 and gateway.get("ExecMainStartTimestamp") == pin["exec_main_start_timestamp"]
                 and gateway.get("ActiveState") == "active" and gateway.get("NRestarts") == "0",
                 "dry-run gateway receipt differs from published identity")
        ops.need(ops.capture(reader, proc_root) == pin, "gateway changed during dry-run")
        ops.need(ops.sha(ops.bounded_read(installed)) == G1_SHA, "G1 source changed during dry-run")
        ops.need(ops.sha(ops.bounded_read(root / "manifest.json")) == published["manifest_after_sha256"],
                 "manifest changed during dry-run")
    except (ValueError, TypeError, KeyError, IndexError, OSError):
        return failure
    return dict(published, status="IDENTITY_AND_PRESTART_PASS", guard_admission="CURRENT_PRESTART_PASS",
                dryrun=receipt, dryrun_source_sha256=G1_SHA,
                future_session_health="NOT_ASSERTED", sampler_or_paper_start=False)


def ordered_afterclose(permission_apply, trader_f3_proof, gateway_restart, identity_and_dryrun):
    """Attach these stages to main's approved installer after its time/trading gates."""
    results = []
    for name, action in (("permission_apply", permission_apply), ("actual_trader_F3", trader_f3_proof),
                         ("gateway_restart", gateway_restart), ("identity_and_readonly_prestart", identity_and_dryrun)):
        row = action()
        expected = "IDENTITY_AND_PRESTART_PASS" if name == "identity_and_readonly_prestart" else "PASS"
        ops.need(isinstance(row, dict) and row.get("status") == expected, "afterclose stage failed: " + name)
        results.append({"stage": name, "receipt": row})
    return {"status": "PASS", "stages": results, "paper_reconciler_continuation": "MAIN_OWNED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--apply-reviewed-afterclose", action="store_true", required=True)
    args = parser.parse_args()
    raw = ops.bounded_read(args.request)
    ops.need(ops.sha(raw) == ops.digest(args.request_sha256), "restart request hash mismatch")
    request = json.loads(raw)
    ops.need(isinstance(request, dict) and request.get("afterclose_install_authorized") is True,
             "explicit reviewed afterclose scope required")
    result = finish(args.root, request, args.backup_dir)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "IDENTITY_AND_PRESTART_PASS" else 1)


if __name__ == "__main__":
    main()
