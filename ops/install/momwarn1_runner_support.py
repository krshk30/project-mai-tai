#!/usr/bin/env python3
"""Local runner helpers. Mutations require the separately approved runner mode."""
import argparse
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import momwarn1_readonly_rehearsal as receipt

SOURCE = "32d5a2f7aecbbc4fc7490f05409be1be053f3a9d"
ENV = Path("/etc/project-mai-tai/project-mai-tai.env")
CONFIG = Path("/run/project-mai-tai/momentum-warning-health.json")
FLAG = "MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_ENABLED"


def need(value, message):
    if not value:
        raise ValueError(message)


def bounded(path, limit=8192):
    path = Path(path)
    need(not path.is_symlink() and path.is_file(), "regular non-symlink input required")
    with path.open("rb") as stream:
        raw = stream.read(limit+1)
    need(len(raw) <= limit, "input byte bound")
    return raw


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def approval(path, mode):
    path = Path(path)
    info = path.stat()
    need(info.st_uid == 0 and info.st_mode & 0o777 == 0o600, "approval must be root-owned mode600")
    request = json.loads(bounded(path))
    need(request.get("task_id") == "MOMWARN1-INSTALL-REHEARSAL", "wrong task")
    need(request.get("execution_approved") is True and request.get("mode") == mode, "not approved for this mode")
    need(isinstance(request.get("human_reference"), str) and request["human_reference"].strip(), "named human approval required")
    today = datetime.now(ZoneInfo("America/New_York"))
    need(request.get("approved_date_et") == today.date().isoformat()
         and request["approved_date_et"] in {"2026-10-12", "2026-10-13"},
         "approval date must match the explicitly approved Oct12/13 afterclose window")
    need(today.hour >= 18, "native unchanged after18 ET clock; no inherited override")
    need(request.get("source_sha") == SOURCE, "warning source differs")
    target = request.get("target_sha", "")
    need(re.fullmatch(r"[0-9a-f]{40}", target), "exact target SHA required")
    evidence = Path(request.get("evidence_directory", ""))
    need(evidence.parent == Path("/home/trader/after-hours") / request["approved_date_et"]
         and evidence.name.startswith("MOMWARN1-"), "isolated evidence directory required")
    if mode == "install":
        need(request.get("qualification_reviewed") is True, "main's exact-head qualification required")
        scope=request.get("restart_scope")
        need(isinstance(scope,list) and len(scope)==len(set(scope)) and "market-data" in scope
             and set(scope) <= {"control", "market-data", "momentum-paper"},
             "gateway required; OMS/strategy/v2 forbidden in this runner")
        if "momentum-paper" in scope:
            need(request.get("paper_start_approved") is True, "existing paper consumer start/restart needs explicit approval")
        else:
            paper=receipt.process("momentum-paper")
            need(paper.get("ActiveState")=="active" and paper.get("flags_and_settings",{}).get(FLAG)=="true",
                 "paper consumer cannot be silently omitted when inactive/old flag")
        if "control" in scope:
            need(request.get("control_display_restart_approved") is True
                 and request.get("control_restart_rationale")=="load_warning_page_and_metrics_display",
                 "control restart is optional, display-only, separately approved")
        need(request.get("actual_A_window_0400_acknowledged") is True,
             "actual source window starts04:00 ET, not operator stated04:11")
        need(request.get("health_setup_reviewed") is True, "actual health provenance must be reviewed")
        need(request.get("single_existing_provider_socket_plan_reviewed") is True, "review single-socket plan")
        need(re.fullmatch(r"[0-9a-f]{64}", request.get("bundle_sha256", "")), "bundle hash required")
        need(re.fullmatch(r"refs/heads/codex/[A-Za-z0-9_./-]+", request.get("bundle_ref", "")), "exact codex bundle ref required")
        raw = bounded(request["health_config_path"])
        need(digest(raw) == request.get("health_config_sha256"), "health configuration hash differs")
        check_config(json.loads(raw))
    return request


def check_config(config, *, runtime=False):
    required = ("oms_cadence_source", "source_timezone_ref", "v2_probe_source_ref", "v2_expectation_ref", "v2_first_bar_source_ref")
    need(config.get("source_config_version") == 1, "only live source configuration schema1")
    need(all(isinstance(config.get(key), str) and config[key].strip() for key in required), "missing actual reviewed source references")
    oms, v2 = receipt.process("oms"), receipt.process("schwab-1m-v2")
    for name, process in (("oms", oms), ("v2", v2)):
        need(process["ActiveState"] == "active" and process["SubState"] == "running", "source process not running")
        need(config.get(name+"_pid") == int(process["MainPID"]) and config.get(name+"_instance_id") == process["instance_id"], "source instance changed; review/rebind required")
        need(config.get(name+"_started_ns") == process["started_ns_second_precision"], "start fence differs from captured systemd second-precision start")
        settings = process["flags_and_settings"]
        zone = settings.get("TZ") or receipt.run(["timedatectl", "show", "-p", "Timezone", "--value"])
        need(ZoneInfo(zone).key == ZoneInfo(config["verified_timezone"]).key, "verified source timezone differs")
    need(config.get("oms_sync_interval_seconds") == float(oms["flags_and_settings"]["MAI_TAI_OMS_BROKER_SYNC_INTERVAL_SECONDS"]), "actual OMS cadence differs")
    probes = v2["flags_and_settings"]["MAI_TAI_STRATEGY_SCHWAB_1M_V2_MACD_PROBE_SYMBOLS"]
    need(config.get("v2_probe_symbols") == [value.strip() for value in probes.split(",") if value.strip()], "actual probe coverage differs")
    if runtime:
        sys.path.insert(0, str(receipt.REPO / "src"))
        from project_mai_tai.momwarn_live_health import LiveWarningHealthSource
        from project_mai_tai.momwarn_health_contract import sourced_first_due_ns
        LiveWarningHealthSource(config, receipt.LOGS)
        sourced_first_due_ns(config, observed_ns=int(datetime.now(UTC).timestamp())*1_000_000_000)
    # Constructor/provenance validation is NOT a live healthy sample or Wednesday acceptance.


def flat():
    process = receipt.process("oms")
    data = receipt.sql_gate(process["flags_and_settings"]["configured_database_name"])
    arms = receipt.v2_state(process["flags_and_settings"].get("MAI_TAI_REDIS_STREAM_PREFIX", "mai_tai"))
    need(data["existing_E_gate_pass_now"], "strict existing E SQL gate not satisfied")
    need(arms["strict_zero_armed_30s"], "strict existing E armed-state gate not satisfied")
    print(json.dumps({"database_gate":data, "v2_state":arms}))


def set_flag(value, request_path):
    need(os.geteuid() == 0, "remote root required")
    request=approval(request_path,"install" if value=="true" else "rollback")
    need(receipt.run(["git","-C",str(receipt.REPO),"rev-parse","HEAD"]) == request["target_sha"], "unexpected checkout for flag mutation")
    if value=="true":
        check_config(json.loads(bounded(request["health_config_path"])),runtime=True)
    need(not ENV.is_symlink() and ENV.is_file(), "regular environment file required")
    raw = ENV.read_bytes()
    need(len(raw) <= 262144, "environment byte bound")
    lines = raw.splitlines(keepends=True)
    matches = [i for i,line in enumerate(lines) if line.startswith((FLAG+"=").encode())]
    need(len(matches) <= 1, "duplicate warning flag definitions")
    replacement = (FLAG+"="+value+"\n").encode()
    if matches:
        lines[matches[0]] = replacement
    else:
        if lines and not lines[-1].endswith(b"\n"):
            lines[-1] += b"\n"
        lines.append(replacement)
    info = ENV.stat()
    temp = ENV.with_name(ENV.name+".momwarn1-new")
    fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, info.st_mode & 0o777)
    try:
        os.fchown(fd, info.st_uid, info.st_gid)
        os.fchmod(fd, info.st_mode & 0o777)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"".join(lines)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temp, ENV)
    finally:
        if temp.exists():
            temp.unlink()
    print("Only warning flag changed; credentials were not printed")


def check_after(before_path, after_path, target, request_path):
    request=approval(request_path,"install")
    scope=set(request["restart_scope"])
    before, after = (json.loads(bounded(path,262144)) for path in (before_path,after_path))
    need(after["head"] == target and after["worktree_clean"] is True, "post-checkout source identity differs")
    for name in receipt.UNITS:
        old, new = before["units"][name], after["units"][name]
        if name not in scope:
            need(old.get("instance_id") == new.get("instance_id") and old.get("ActiveState") == new.get("ActiveState"), "untouched unit changed: "+name)
        else:
            need(new.get("ActiveState") == "active" and new.get("SubState") == "running" and new.get("NRestarts") == "0", "restart state failed: "+name)
            need(new.get("instance_id") and new.get("instance_id") != old.get("instance_id"), "new instance missing: "+name)
        old_flags = dict(old.get("flags_and_settings", {}))
        new_flags = dict(new.get("flags_and_settings", {}))
        if old.get("MainPID") != "0":
            old_flags.pop(FLAG,None); new_flags.pop(FLAG,None)
            need(old_flags == new_flags, "unrelated proc settings changed: "+name)
    for name in scope:
        need(after["units"][name]["flags_and_settings"].get(FLAG) == "true", "warning flag not loaded: "+name)


def install_record(snapshot_path, request_path):
    request=approval(request_path,"install")
    snapshot=json.loads(bounded(snapshot_path,262144))
    print(json.dumps({"schema_version":1,
        "snapshot_captured_at_utc":snapshot["captured_at_utc"],
        "source_journal":str(Path(request["evidence_directory"])/"runner.log"),
        "service_actions":{name:("restarted" if name in request["restart_scope"] else "deliberately_untouched")
                           for name in snapshot["services"]}},indent=2))


def main():
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest="command",required=True)
    p=sub.add_parser("approval"); p.add_argument("path"); p.add_argument("mode",choices=("install","rollback"))
    sub.add_parser("flat")
    p=sub.add_parser("set-flag"); p.add_argument("value",choices=("true","false")); p.add_argument("approval_path")
    p=sub.add_parser("check-after"); p.add_argument("before"); p.add_argument("after"); p.add_argument("target"); p.add_argument("approval_path")
    p=sub.add_parser("install-record"); p.add_argument("snapshot"); p.add_argument("approval_path")
    args=parser.parse_args()
    if args.command=="approval":
        print(json.dumps(approval(args.path,args.mode)))
    elif args.command=="flat":
        flat()
    elif args.command=="set-flag":
        set_flag(args.value,args.approval_path)
    elif args.command=="check-after":
        check_after(args.before,args.after,args.target,args.approval_path)
    else:
        install_record(args.snapshot,args.approval_path)


if __name__=="__main__":
    main()
