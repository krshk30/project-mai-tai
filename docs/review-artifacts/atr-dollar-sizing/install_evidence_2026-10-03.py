"""Read-only install proofs and append-only evidence; never controls services."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

APPROVED_SHA = "608339894a1cfb33284e695196df55c18f312889"
ROOT = Path("/home/trader/after-hours/2026-10-03/sizing-nfq1-install")
JOURNAL = Path("/home/trader/fleet_health/deployments-20261003.md")
EXPECTED = {
    "oms": ("2386879", "Fri 2026-10-02 00:16:42 UTC"),
    "strategy": ("2387016", "Fri 2026-10-02 00:17:03 UTC"),
    "schwab-1m-v2": ("1664453", "Wed 2026-09-30 00:08:16 UTC"),
    "orb": ("1665228", "Wed 2026-09-30 00:09:18 UTC"),
    "orb-schwab": ("2387072", "Fri 2026-10-02 00:17:19 UTC"),
    "market-data": ("2346625", "Thu 2026-10-01 21:01:04 UTC"),
    "momentum-paper": ("2477935", "Fri 2026-10-02 11:20:32 UTC"),
    "reconciler": ("1626620", "Mon 2026-09-14 21:34:21 UTC"),
}
RESTARTED = {"oms", "strategy", "schwab-1m-v2"}
SIZING = {
    "strategy_schwab_1m_v2_entry_notional_usd": 600,
    "strategy_schwab_1m_v2_webull_entry_notional_usd": 300,
    "strategy_schwab_1m_v2_entry_max_shares": 1000,
}
NFQ = {"oms_v2_webull_mirror_fresh_price_enabled": True,
       "oms_v2_webull_mirror_quote_max_age_ms": 10000}
RETAINED = {
    "oms_v2_cw_target_pct": 5.0, "oms_v2_cw_hard_stop_pct": 8.0,
    "oms_v2_cw_floor_exit_enabled": False,
    "strategy_schwab_1m_v2_retry_one_enabled": True,
    "oms_v2_eod_oco_transition_enabled": True,
    "oms_v2_overnight_flatten_enabled": True,
    "orb_live_schwab_orders_enabled": True,
    "oms_v2_cw_target_stay_enabled": True,
    "oms_v2_eh_resting_entry_quote_max_age_ms": 2000,
}


def command(*args):
    return subprocess.check_output(args, text=True, timeout=15).strip()


def identities():
    result = {}
    for name in EXPECTED:
        raw = command("systemctl", "show", f"project-mai-tai-{name}.service",
                      "-p", "MainPID", "-p", "ExecMainStartTimestamp", "-p", "ActiveState",
                      "-p", "SubState", "-p", "NRestarts")
        result[name] = dict(line.split("=", 1) for line in raw.splitlines())
    return result


def verify_identities(rows, after=False):
    for name, row in rows.items():
        assert row["ActiveState"] == "active" and row["SubState"] == "running", (name, row)
        assert row["NRestarts"] == "0" and int(row["MainPID"]) > 0, (name, row)
        identity = row["MainPID"], row["ExecMainStartTimestamp"]
        if after and name in RESTARTED:
            assert identity[0] != EXPECTED[name][0] and identity[1] != EXPECTED[name][1], (name, row)
            stamp = datetime.strptime(identity[1], "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=ZoneInfo("UTC"))
            assert stamp.astimezone(ZoneInfo("America/New_York")).date().isoformat() == "2026-10-03"
        else:
            assert identity == EXPECTED[name], (name, row)


def save(name, value):
    with (ROOT / name).open("x") as out:
        json.dump(value, out, sort_keys=True, indent=2)
        out.write("\n")
    print(json.dumps(value, sort_keys=True), flush=True)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def process_settings(pid):
    from project_mai_tai.settings import Settings
    env = dict(item.decode().split("=", 1) for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in item)
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        settings = Settings(_env_file=None)
    finally:
        os.environ.clear()
        os.environ.update(original)
    return env, settings


def preflight():
    now = datetime.now(ZoneInfo("America/New_York"))
    assert now.date().isoformat() == "2026-10-03", "expired installation date"
    rows = identities()
    verify_identities(rows)
    assert digest("/home/trader/preopen.sh") == "d58aa3510d16977ad245d98e05e0d0c4dff9fc6db41d58907d22f86e7e46cbd2"
    assert digest("/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py") == "831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e"
    jobs = command("systemctl", "list-jobs", "--no-pager", "--no-legend")
    assert not any(f"project-mai-tai-{name}.service" in jobs for name in EXPECTED), jobs
    processes = command("ps", "-eo", "pid,args")
    conflicts = [line for line in processes.splitlines() if re.search(r"(?:^|/)(?:deploy_main|deploy_service)\.sh(?:\s|$)", line)]
    assert not conflicts, conflicts
    assert not any("option_a_treatment_guard.py" in line or "option_a_treatment_1008_sampler.py" in line for line in processes.splitlines()), "old guard/sampler process present"
    log = Path("/var/log/project-mai-tai/market-data.log")
    stat = log.stat()
    fd = Path("/proc/2346625/fd/1").stat()
    assert (fd.st_dev, fd.st_ino) == (stat.st_dev, stat.st_ino), "gateway log inode drift"
    status = [line for line in Path("/var/lib/logrotate/status").read_text().splitlines() if line.startswith('"/var/log/project-mai-tai/market-data.log" ')]
    assert len(status) == 1 and "2026-10-3-" in status[0], "latest nightly rotation not proven"
    rotated = Path("/var/log/project-mai-tai/market-data.log-20261003")
    assert rotated.exists(), "rotated copy missing"
    env, settings = process_settings(rows["schwab-1m-v2"]["MainPID"])
    window = {name: str(getattr(settings, name)) for name in type(settings).model_fields if "schwab_1m_v2" in name and ("end_hour" in name or "end_minute" in name)}
    save("preflight.json", {"at_et": now.isoformat(), "identities": rows, "systemd_jobs": jobs,
        "rotation_status": status[0], "gateway_log_inode": stat.st_ino,
        "rotated_size": rotated.stat().st_size, "v2_window_settings": window})


def final_identities():
    rows = identities()
    verify_identities(rows, after=True)
    save("final-identities.json", rows)


def loaded_values():
    rows = identities()
    verify_identities(rows, after=True)
    result = {}
    for name in ("oms", "schwab-1m-v2", "strategy"):
        env, settings = process_settings(rows[name]["MainPID"])
        expected = ({**SIZING, **NFQ, **RETAINED} if name == "oms" else SIZING if name == "schwab-1m-v2" else {"strategy_polygon_30s_enabled": False})
        values = {}
        for field, wanted in expected.items():
            actual = getattr(settings, field)
            key = "MAI_TAI_" + field.upper()
            if field in SIZING or field in NFQ:
                assert key in env, f"missing process env {name}:{key}"
                assert env[key].strip().lower() == str(wanted).lower(), (name, key, env[key])
            assert actual == wanted, (name, field, actual, wanted)
            values[field] = {"effective": str(actual), "process_env": env.get(key, "<default>")}
        result[name] = {"pid": rows[name]["MainPID"], "values": values}
    assert identities() == rows, "process moved during /proc read"
    save("loaded-values.json", result)


def oms_health():
    from redis import Redis
    from project_mai_tai.settings import Settings
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    row = identities()["oms"]
    assert row["MainPID"] != EXPECTED["oms"][0] and row["NRestarts"] == "0", row
    started = datetime.strptime(row["ExecMainStartTimestamp"], "%a %Y-%m-%d %H:%M:%S UTC").replace(tzinfo=ZoneInfo("UTC"))
    deadline = time.monotonic() + 180
    with Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5, decode_responses=True) as client:
        while time.monotonic() < deadline:
            assert identities()["oms"] == row, "OMS identity moved during health proof"
            # At most 25 small heartbeat envelopes; no snapshot payload read.
            for _, fields in client.xrevrange(f"{settings.redis_stream_prefix}:heartbeats", count=25):
                event = json.loads(fields["data"])
                if event.get("source_service") != "oms-risk":
                    continue
                stamp = datetime.fromisoformat(event["produced_at"].replace("Z", "+00:00"))
                age = (datetime.now(ZoneInfo("UTC")) - stamp).total_seconds()
                if stamp > started and 0 <= age < 30 and event["payload"]["status"] == "healthy":
                    save("oms-health.json", {"identity": row, "heartbeat": event, "age_seconds": age})
                    return
                break
            time.sleep(1)
    raise RuntimeError("new OMS healthy heartbeat unproven after 180 seconds")


def journal(result):
    stamp = datetime.now(ZoneInfo("America/New_York")).isoformat()
    with JOURNAL.open("a") as out:
        out.write(f"\n## Sizing + NFQ1 install {stamp}: {result}\n")
        out.write(f"Application SHA `{APPROVED_SHA}`; evidence `{ROOT}`.\n")
        release = Path(__file__).with_name("release.json")
        if release.exists():
            out.write(f"Plan commit `{json.loads(release.read_text())['plan_commit']}`.\n")
        for name in ("preflight.json", "final-identities.json", "loaded-values.json", "oms-health.json", "redis-preflight.json", "strategy-redis-before.json", "flags-and-numeric.txt", "numeric-only.txt", "installed.sha256", "backups.sha256", "env-after.sha256", "preopen.sha256", "v2-restart-preflight.txt"):
            path = ROOT / name
            if path.exists():
                text = path.read_text()
                assert len(text) < 100_000, f"oversize evidence {path}"
                out.write(f"\n### {name}\n```text\n{text}\n```\n")
        out.write("\nRPG1 excluded. No gateway/orb/orb-schwab/paper/reconciler restart authorized. "
                  "Partial-parent native OCO and first live NFQ handling remain UNEXERCISED; "
                  "BOOT-HOLD/warm-up is reported literally in v2-startup.log and v2-closeout.log, "
                  "not inferred from service state. Monday guard has not been scheduled by this runner.\n")
    print(f"JOURNAL result={result} path={JOURNAL}", flush=True)


if __name__ == "__main__":
    actions = {"preflight": preflight, "identities": final_identities, "loaded": loaded_values,
               "oms-health": oms_health,
               "complete": lambda: journal("INSTALL COMPLETE"), "abort": lambda: journal("STOPPED - see steps.log and actual service states")}
    actions[sys.argv[1]]()
