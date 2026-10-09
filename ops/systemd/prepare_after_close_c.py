"""Read-only C preparation. No install, re-pin publication, restart, or release sealing."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, time, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from zoneinfo import ZoneInfo

REPO = Path("/home/trader/project-mai-tai")
DAILY = Path("/home/trader/preopen-daily")
GATE = Path("/home/trader/preopen.sh")
READ_GATE = Path("/home/trader/after-hours/2026-10-08/quote-guard-c180af22/job/gate_readonly.py")
READ_GATE_HASH = "6117fad26ec63a4aa7a731fc20e0160f103fb1a5e54980697ef5d64ffcc5248f"
O_FLAG = "MAI_TAI_ORB_SCHWAB_MACD_FILL_ENABLED"
L_FLAG = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED"
RPG_FLAG = "MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED"
LABELS = {"control": "CONTROL_", "orb-schwab": "ORB_SCHWAB_", "schwab-1m-v2": "",
          "oms": "OMS_", "strategy": "STRATEGY_"}
UTC = timezone.utc
ET = ZoneInfo("America/New_York")


class Refusal(ValueError):
    pass


def need(condition, reason):
    if not condition:
        raise Refusal(reason)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def full_sha(value):
    need(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "full final SHA required")
    return value


def verify_main(repo, expected_sha):
    full_sha(expected_sha)
    result = subprocess.run(["git", "-C", str(repo), "ls-remote", "--exit-code", "--refs", "origin",
                             "refs/heads/main"], capture_output=True, text=True, check=True, timeout=20)
    need(result.stdout.strip() == expected_sha + "\trefs/heads/main", "final merged main moved/unbound")
    return {"verified_main_sha": expected_sha, "verified_at_utc": datetime.now(UTC).isoformat(),
            "execution_authorized": False}


def sequence(orb, line, fi=False):
    return [*(["oms"] if fi else []), *(["schwab-1m-v2"] if line or fi else []),
            *(["orb-schwab"] if orb else []), "control"]


def restart_group(orb, line, fi=False):
    return set(sequence(orb, line, fi)) | ({"strategy"} if fi else set())


def after_close(now, date_et):
    need(now.tzinfo is not None, "un-zoned clock")
    local = now.astimezone(ET)
    need(local.date().isoformat() == date_et and local.time() >= time(16), "not bound after-close date")


def env_candidate(raw, *, orb, line):
    """Construct bytes only; preserve every non-authorized line, including secrets."""
    text = raw.decode("utf-8")
    keys = re.findall(r"^([A-Za-z_][A-Za-z0-9_]*)=", text, re.M)
    need(len(keys) == len(set(keys)), "duplicate environment assignment")
    need(re.findall(rf"^{RPG_FLAG}=(.*)$", text, re.M) == ["false"], "RPG must remain explicit false")
    for key in ([O_FLAG] if orb else []) + ([L_FLAG] if line else []):
        pattern = rf"^{key}=.*$"
        if key in keys:
            text = re.sub(pattern, key + "=true", text, flags=re.M)
        else:
            text += ("" if text.endswith("\n") else "\n") + key + "=true\n"
    return text.encode()


def validate_process_flags(before, after, *, orb, line, fi=False):
    need(set(before) == set(after), "process flag fleet incomplete")
    changes = ([O_FLAG] if orb else []) + ([L_FLAG] if line else [])
    allowed = {(owner, key) for owner in restart_group(orb, line, fi) for key in changes}
    for owner in before:
        for key in before[owner].keys() | after[owner].keys():
            if (owner, key) in allowed:
                need(after[owner].get(key) == "true", "authorized process opt-in absent")
            else:
                need(before[owner].get(key) == after[owner].get(key), "unapproved process setting changed")
        if RPG_FLAG in before[owner] or RPG_FLAG in after[owner]:
            need(before[owner].get(RPG_FLAG) == after[owner].get(RPG_FLAG) == "false", "process RPG not false")


def moment(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    need(result.tzinfo is not None, "un-zoned receipt time")
    return result.astimezone(UTC)


def flat_receipt(receipt, rc, started, now):
    """Check the reviewed gate's actual result; do not reinterpret operator holdings."""
    need(rc == 0 and receipt.get("rc") == 0, "direct broker/SQL gate refused")
    need(receipt.get("blockers") == [] and receipt.get("unknown") == []
         and receipt.get("errors") == [], "direct gate incomplete")
    need(started <= moment(receipt["started_at"]) <= moment(receipt["completed_at"]) <= now,
         "receipt not produced by this invocation")
    sql = receipt["sql"]
    need(sql.get("complete") is True, "SQL census incomplete")
    need(0 <= (now - moment(sql["observed_at"])).total_seconds() <= 120, "SQL stale/future")
    for label in ("managed_rows", "virtual_rows", "working_orders", "inflight_intents"):
        need(sql.get(label) == [], "nonempty/unmeasured " + label)
    for account in ("live:schwab_1m_v2", "live:orb"):
        source = receipt["brokers"][account]
        need(source.get("complete") is True and source.get("identity_bound") is True,
             "broker account unbound/incomplete")
        need(source.get("working_orders") == [], "broker working order")
        need(0 <= (now - moment(source["started_at"])).total_seconds() <= 120, "broker stale/future")


def direct_gate():
    need(os.geteuid() == 0, "live read-only gate requires root")
    need(not READ_GATE.is_symlink() and digest(READ_GATE.read_bytes()) == READ_GATE_HASH,
         "reviewed direct gate missing/drifted")
    started = datetime.now(UTC)
    result = subprocess.run([str(REPO / ".venv/bin/python"), str(READ_GATE)],
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, timeout=240, check=False)
    need(len(result.stdout) + len(result.stderr) <= 4_000_000, "gate output overflow")
    # Exceptions may contain provider/credential details. Do not echo raw stderr.
    need(not result.stderr.strip(), "direct gate stderr; retain securely before any install")
    receipt = json.loads(result.stdout)
    flat_receipt(receipt, result.returncode, started, datetime.now(UTC))
    return {"verdict": "READ_ONLY_FLAT", "completed_at": receipt["completed_at"],
            "receipt_sha256": digest(result.stdout), "gate_sha256": READ_GATE_HASH}


def replace_assignment(text, key, value):
    pattern = rf"^{key}=.*$"
    need(len(re.findall(pattern, text, re.M)) == 1, "missing/duplicate preopen assignment: " + key)
    return re.sub(pattern, lambda _: key + "=" + shlex.quote(str(value)), text, flags=re.M)


def preopen_candidate(text, expected_sha, before, after, record, *, orb, line, fi=False):
    """Pure gate construction from measured identities, never a cumulative restart group."""
    full_sha(expected_sha)
    targets = restart_group(orb, line, fi)
    need(set(before) == set(after) and targets <= set(before), "fleet census differs/incomplete")
    need(record["snapshot_captured_at_utc"] and record["source_journal"], "install evidence absent")
    expected_actions = {name: "restarted" if name in targets else "deliberately_untouched" for name in before}
    need(record.get("service_actions") == expected_actions, "install action group differs")
    need('EXPECTED_DATE="$(TZ=America/New_York date +%F)"' in text
         and 'v2-restart-evidence-${EXPECTED_DATE//-/}.md' in text
         and str(DAILY / "daily.py") + " paper" in text, "dynamic daily/paper contract absent")
    for name in before:
        if name not in targets:
            need(before[name] == after[name], "untouched identity changed: " + name)
            continue
        state = after[name]
        need(state["MainPID"].isdigit() and int(state["MainPID"]) > 0
             and state["MainPID"] != before[name]["MainPID"] and state["NRestarts"] == "0"
             and state["ActiveState"] == "active" and state["SubState"] == "running", "failed new start: " + name)
        start = datetime.strptime(state["ExecMainStartTimestamp"], "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=UTC)
        need(moment(record["snapshot_captured_at_utc"]) <= start <= moment(record["completed_at_utc"]),
             "start outside actual install: " + name)
        for suffix, field in (("PID", "MainPID"), ("START", "ExecMainStartTimestamp")):
            text = replace_assignment(text, "EXPECTED_" + LABELS[name] + suffix, state[field])
    text = replace_assignment(text, "EXPECTED_SHA", expected_sha)
    for key, field in (("SNAPSHOT", "snapshot"), ("INSTALL_RECORD", "install_record")):
        path = record[field]
        need(re.fullmatch(r"/home/trader/[A-Za-z0-9_./-]+", path) and ".." not in Path(path).parts,
             "unsafe evidence path")
        text = replace_assignment(text, key, path)
    need(re.findall(rf"--expect-flag '[^']*:{RPG_FLAG}=([^']+)'", text)
         and set(re.findall(rf"--expect-flag '[^']*:{RPG_FLAG}=([^']+)'", text)) == {"false"},
         "preopen RPG contract must remain false")
    text = re.sub(r"^  --restarted [a-z0-9-]+ \\\n", "", text, flags=re.M)
    anchor = '  --install-record "$INSTALL_RECORD" \\\n'
    need(text.count(anchor) == 1, "install-record report argument ambiguous")
    additions = "".join("  --restarted " + name + " \\\n" for name in sorted(targets))
    # The collector rejects flags naming an owner outside this actual restart.
    text = re.sub(r"^  --expect-flag '([^:']+):[^']+' \\\n",
                  lambda match: match.group(0) if match.group(1) in targets else "", text, flags=re.M)
    for owner, key in (("orb-schwab", O_FLAG), ("schwab-1m-v2", L_FLAG)):
        if owner not in targets or key == L_FLAG and not line:
            continue
        pattern = rf"^  --expect-flag '{owner}:{key}=[^']+' \\\n"
        need(len(re.findall(pattern, text, re.M)) <= 1, "duplicate target flag pin")
        text = re.sub(pattern, "", text, flags=re.M)
        additions += "  --expect-flag '" + owner + ":" + key + "=true' \\\n"
    return text.replace(anchor, anchor + additions).encode()


def runtime_candidate(runtime, binding, candidate_gate, artifact_hashes, evidence_hashes, install):
    """Construct a new daily manifest, retaining every historical dependency/hash."""
    need(runtime["approved_sha"] == binding["approved_sha"], "old daily bindings disagree")
    full_sha(install["approved_sha"])
    full_sha(install["tree"])
    need(set(artifact_hashes) == {"binding.json"} and "binding.json" in runtime["artifacts"],
         "only the measured binding artifact may change")
    need(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (*artifact_hashes.values(), *evidence_hashes.values())),
         "new artifact/evidence digest malformed")
    required_inputs = {install[key] for key in ("snapshot", "install_record", "source_journal")}
    need(required_inputs <= set(evidence_hashes), "current install evidence omitted")
    need(not (set(evidence_hashes) & set(runtime["evidence_inputs"])), "historical input replacement refused")
    result_binding = {"approved_sha": install["approved_sha"], "tree": install["tree"],
                      "historical_binding": deepcopy(binding), "current_install": deepcopy(install)}
    binding_raw = (json.dumps(result_binding, indent=2, sort_keys=True) + "\n").encode()
    need(artifact_hashes["binding.json"] == digest(binding_raw), "binding bytes/hash disagree")
    result = deepcopy(runtime)
    result.update(approved_sha=install["approved_sha"], gate_sha256=digest(candidate_gate),
                  current_install=deepcopy(install))
    result["artifacts"].update(artifact_hashes)
    result["evidence_inputs"].update(evidence_hashes)
    return result_binding, result


def describe(expected_sha, *, orb, line):
    if expected_sha is not None:
        full_sha(expected_sha)
    return {
        "verdict": "PREPARATION_ONLY_NOT_SEALED", "can_execute": False,
        "expected_sha": expected_sha, "services": sequence(orb, line),
        "environment_changes": {key: "true" for key in ([O_FLAG] if orb else []) + ([L_FLAG] if line else [])},
        "deploy_environment": {"MAI_TAI_EXPECTED_SHA": expected_sha, "MAI_TAI_RUN_MIGRATIONS": "0",
                               "MAI_TAI_ALLOW_LIVE_RESTART": "0", "MAI_TAI_HOLD_STRATEGY": "0"},
        "read_only_gate": [str(REPO / ".venv/bin/python"), str(READ_GATE)],
        "required": ["exact final merged-main SHA and reviewer pins", "after-close fresh direct gate per step",
                     "all non-target service identities and flags unchanged; RPG=false",
                     "env backup/hash before replacement; one attempt/service; no recovery after first write",
                     "G2 post-archive replay pinned to LINE head" if line else "LINE deliberately excluded"],
        "unsupported": ["installed re-pin tool hard-codes historical OMS/strategy restart group",
                        *([] if line else ["native C6 report requires a v2 restart; do not invent one"])],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha")
    parser.add_argument("--orb", action="store_true")
    parser.add_argument("--line", action="store_true")
    parser.add_argument("--read-only-gate", action="store_true")
    parser.add_argument("--check-main", type=Path, help="read-only ls-remote against this repository's origin")
    args = parser.parse_args(argv)
    try:
        result = describe(args.expected_sha, orb=args.orb, line=args.line)
        if args.check_main is not None:
            result["measured_main"] = verify_main(args.check_main, args.expected_sha)
        if args.read_only_gate:
            result["measured_gate"] = direct_gate()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (Refusal, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"verdict": "REFUSED", "can_execute": False, "error_type": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
