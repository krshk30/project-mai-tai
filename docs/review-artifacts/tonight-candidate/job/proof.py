"""Read-only checkpoint helper for the exact 7823 install; stdout is the receipt.

Parent runner owns O_EXCL receipts, env edits, schema GO, stops/starts, backups
and paging. This helper NEVER performs those writes or restores a service.
`repin` emits a candidate/diff, NOT an installed script or routing approval.
Run with PYTHONDONTWRITEBYTECODE=1. No broker/token refresh or remote execution.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from difflib import unified_diff
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

SHA = "7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf"
TREE = "ee6f058c248eeebf475fd392845eadfef7af59eb"
DAY = "2026-10-05"
NEXT = "2026-10-06"
CHANGED = ("oms", "schwab-1m-v2", "strategy")
SERVICES = ("control", "market-capture", "market-data", "oms", "orb", "orb-schwab",
            "reconciler", "schwab-1m-v2", "strategy", "momentum-paper")
FLAGS = tuple("MAI_TAI_STRATEGY_SCHWAB_1M_V2_" + name for name in (
    "PM_PRINT_ASK_CONFIRM_ENABLED", "PM_FLIP_WAIT_ENABLED", "PM_REST_REPRICE_ENABLED",
    "ATR_REPRICE_HANDOFF_ENABLED"))
ENV_FILES = ("/etc/project-mai-tai/project-mai-tai.env", "/etc/project-mai-tai/orb-paper.env")
MAX_BYTES = 3_000_000


class Unknown(RuntimeError):
    pass


class Blocked(RuntimeError):
    pass


def need(condition, message):
    if not condition:
        raise Blocked(message)


def read(path):
    with Path(path).open("rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise Unknown("file exceeds bounded proof size")
    return data


def sha(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(read(path))


def run(args, *, check=True):
    result = subprocess.run(args, capture_output=True, timeout=30)
    if len(result.stdout) + len(result.stderr) > MAX_BYTES:
        raise Unknown("command proof exceeds bound")
    if check and result.returncode:
        raise Unknown("read-only command failed: " + str(args[0]))
    return result.returncode, result.stdout.decode(), result.stderr.decode()


def wall(*, action=False):
    now = datetime.now(UTC)
    local = now.astimezone(ZoneInfo("America/New_York"))
    need(local.date().isoformat() == DAY and local.hour < 20 and (not action or local.hour >= 18),
         "outside October5 window (actions require18<=hour<20); no clock override")
    return now.isoformat()


def moment(value):
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise Unknown("proof timestamp lacks timezone")
    return result.astimezone(UTC)


def service(name):
    fields = ("MainPID", "NRestarts", "ActiveState", "SubState", "ExecMainStartTimestamp",
              "ExecMainStartTimestampMonotonic", "EnvironmentFiles", "FragmentPath", "DropInPaths")
    _, output, _ = run(["systemctl", "show", f"project-mai-tai-{name}.service",
                        *[f"--property={field}" for field in fields]])
    result = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if set(result) != set(fields):
        raise Unknown("incomplete service identity: " + name)
    for key in ("MainPID", "NRestarts", "ExecMainStartTimestampMonotonic"):
        result[key] = int(result[key])
    return result


def env_metadata():
    result = {}
    for name in ENV_FILES:
        path = Path(name)
        data = read(path)
        keys = re.findall(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=", data.decode(), re.M)
        need(len(keys) == len(set(keys)), "duplicate EnvironmentFile definition")
        info = path.stat()
        result[name] = {"sha256": sha(data), "mode": oct(info.st_mode & 0o777),
                        "uid": info.st_uid, "gid": info.st_gid, "keys": sorted(keys)}
    need(result[ENV_FILES[0]]["mode"] == "0o600", "primary EnvironmentFile is not0600")
    return result


def log_offsets():
    result = {}
    for name in CHANGED:
        path = Path(f"/var/log/project-mai-tai/{name}.log")
        info = path.stat()
        result[name] = {"path": str(path), "inode": info.st_ino, "device": info.st_dev,
                        "offset": info.st_size}
    return result


def database(repo, *, since=None):
    sys.path.insert(0, str(Path(repo).resolve() / "src"))
    from sqlalchemy import create_engine, text
    from project_mai_tai.settings import Settings
    engine = create_engine(Settings(_env_file=ENV_FILES[0]).database_url,
                           connect_args={"connect_timeout": 5})
    queries = {
        "revision": "SELECT version_num FROM alembic_version LIMIT 2",
        "columns": "SELECT column_name,data_type,character_maximum_length,is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name='oms_managed_positions' AND column_name IN ('entry_order_id','entry_client_order_id') ORDER BY column_name",
        "tickets": "SELECT id,payload FROM dashboard_snapshots WHERE snapshot_type='atr_reprice_handoff' ORDER BY id LIMIT 65",
        "account_stamps": "SELECT a.name account,max(p.updated_at) updated_at FROM broker_accounts a LEFT JOIN account_positions p ON p.broker_account_id=a.id WHERE a.name IN ('live:orb','live:schwab_1m_v2') GROUP BY a.name ORDER BY a.name",
        "live_bars": "SELECT symbol,max(bar_time) last_live_bar,count(*) bars FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND source='live' AND bar_time>=now()-interval '10 minutes' GROUP BY symbol ORDER BY symbol LIMIT 65",
    }
    if since is not None:
        queries.update({
            "new_buys": "SELECT b.id,b.symbol,b.client_order_id,b.submitted_at FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND lower(b.side)='buy' AND b.submitted_at>=:since ORDER BY b.submitted_at LIMIT 65",
            "new_open_intents": "SELECT t.id,t.symbol,t.created_at,t.status FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND lower(t.side)='buy' AND t.intent_type='open' AND t.created_at>=:since ORDER BY t.created_at LIMIT 65",
            "new_buy_fills": "SELECT f.id,f.symbol,f.quantity,f.filled_at FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND upper(f.side)='BUY' AND f.filled_at>=:since ORDER BY f.filled_at LIMIT 65",
        })
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout='5s'")
            result = {}
            for key, query in queries.items():
                rows = [dict(row) for row in connection.execute(text(query),
                        {"since": moment(since)} if since else {}).mappings()]
                if len(rows) > 64:
                    raise Unknown("all-date census overflow: " + key)
                result[key] = rows
            connection.rollback()
    finally:
        engine.dispose()
    need(len(result["revision"]) == 1, "schema revision not single-valued")
    return result


def capture(repo):
    at = wall()
    _, head, _ = run(["git", "-C", repo, "rev-parse", "HEAD"])
    _, tree, _ = run(["git", "-C", repo, "rev-parse", "HEAD^{tree}"])
    _, dirty, _ = run(["git", "-C", repo, "status", "--porcelain"])
    need(not dirty, "checkout is dirty")
    result = {"schema_version": 1, "captured_at_utc": at, "approved_sha": SHA,
              "head": head.strip(), "tree": tree.strip(), "services": {name: service(name) for name in SERVICES},
              "env_files": env_metadata(), "logs": log_offsets(), "db": database(repo),
              "preopen_sha256": sha(read("/home/trader/preopen.sh"))}
    return result


def active(state):
    return state["MainPID"] > 0 and state["NRestarts"] == 0 and (
        state["ActiveState"], state["SubState"]) == ("active", "running")


def validate_checkpoint(before, after, phase):
    need(before["schema_version"] == after["schema_version"] == 1, "bad checkpoint shape")
    need(before["approved_sha"] == after["approved_sha"] == SHA, "checkpoint target mismatch")
    need(set(before["services"]) == set(after["services"]) == set(SERVICES), "incomplete fleet census")
    need(after["head"] == SHA and after["tree"] == TREE, "not exact approved source/tree")
    need(moment(after["captured_at_utc"]) >= moment(before["captured_at_utc"]), "reversed checkpoints")
    need(after["env_files"][ENV_FILES[1]] == before["env_files"][ENV_FILES[1]], "orb-paper.env changed")
    need(after["preopen_sha256"] == before["preopen_sha256"], "preopen changed before reviewed repin")
    for name in set(SERVICES) - set(CHANGED):
        need(after["services"][name] == before["services"][name], "untouched identity changed: " + name)
    stops = {"v2-stopped": {"schwab-1m-v2"}, "strategy-stopped": {"schwab-1m-v2", "strategy"},
             "oms-stopped": set(CHANGED), "migrated": set(CHANGED),
             "oms-started": {"schwab-1m-v2", "strategy"}, "v2-started": {"strategy"}, "final": set()}
    stopped = stops[phase]
    for name in CHANGED:
        state, old = after["services"][name], before["services"][name]
        if name in stopped:
            need(state["MainPID"] == 0 and state["ActiveState"] == "inactive", "not stopped: " + name)
        elif phase in {"oms-started", "v2-started", "final"}:
            need(active(state) and state["ExecMainStartTimestampMonotonic"] > old["ExecMainStartTimestampMonotonic"],
                 "new active identity not proven: " + name)
            need(state["MainPID"] != old["MainPID"], "PID reused/unproven: " + name)
        else:
            need(state == old, "early owning service change: " + name)
    expected = "20261005_0022" if phase in {"migrated", "oms-started", "v2-started", "final"} else "20260916_0021"
    need(after["db"]["revision"] == [{"version_num": expected}], "unexpected schema checkpoint")
    if expected.endswith("0022"):
        columns = {row["column_name"]: row for row in after["db"]["columns"]}
        need(set(columns) == {"entry_order_id", "entry_client_order_id"}, "binding columns missing")
        need(columns["entry_order_id"]["data_type"] == "uuid" and columns["entry_order_id"]["is_nullable"] == "YES",
             "entry_order_id type/nullability mismatch")
        need(columns["entry_client_order_id"]["data_type"] == "character varying" and
             columns["entry_client_order_id"]["character_maximum_length"] == 128 and
             columns["entry_client_order_id"]["is_nullable"] == "YES", "entry client type/nullability mismatch")
    else:
        need(not after["db"]["columns"], "early/unexpected binding schema")


def proc_flags(checkpoint):
    result = {}
    for name in ("oms", "schwab-1m-v2"):
        state = service(name)
        need(active(state) and state == checkpoint["services"][name], "PID moved during proc proof")
        pairs = [row.split(b"=", 1) for row in read(f"/proc/{state['MainPID']}/environ").split(b"\0") if b"=" in row]
        values = {}
        for key in FLAGS:
            matches = [value.decode() for field, value in pairs if field.decode() == key]
            need(matches == ["true"], "loaded key missing/duplicate/not literaltrue: " + name + ":" + key)
            values[key] = True
        need(service(name) == state, "PID moved after proc proof")
        result[name] = {"pid": state["MainPID"], "flags": values}
    return result


def post_logs(before):
    result = {}
    for name, offset in before["logs"].items():
        current = Path(offset["path"])
        candidates = sorted(current.parent.glob(current.name + "*"))
        matching = [p for p in candidates if p.is_file() and
                    (p.stat().st_ino, p.stat().st_dev) == (offset["inode"], offset["device"])]
        if len(matching) != 1:
            raise Unknown("log baseline inode lost/ambiguous: " + name)
        original, = matching
        need(original.stat().st_size >= offset["offset"], "log copytruncate/reset: " + name)
        with original.open("rb") as stream:
            stream.seek(offset["offset"])
            data = stream.read(MAX_BYTES + 1)
        if original != current:
            data += read(current)
        if len(data) > MAX_BYTES:
            raise Unknown("new log proof exceeds bound: " + name)
        lines = data.decode("utf-8", errors="strict").splitlines()
        need(not any("Traceback (most recent call last):" in line for line in lines), "new traceback: " + name)
        result[name] = {"bytes": len(data), "sha256": sha(data), "lines": lines}
    return result


def sync_proof(logs, db, started):
    pattern = r"live:orb: ok=(\d+) failed=(\d+) consecutive_now=(\d+)"
    rows = [re.search(pattern, line) for line in logs["oms"]["lines"] if "[BROKER-SYNC-CENSUS]" in line]
    matches = [match for match in rows if match]
    if not matches:
        raise Unknown("no post-start live:orb sync census; ok0 is not proof")
    need(all(int(match[2]) == 0 for match in matches), "post-start Webull sync failed")
    need(any(int(match[1]) > 0 for match in matches), "post-start Webull sync never succeeded")
    stamps = {row["account"]: row["updated_at"] for row in db["account_stamps"]}
    need(set(stamps) == {"live:orb", "live:schwab_1m_v2"}, "account timestamp census incomplete")
    for stamp in stamps.values():
        if stamp is None or moment(stamp) < started or not 0 <= (datetime.now(UTC) - moment(stamp)).total_seconds() <= 120:
            raise Unknown("fresh post-start account stamp not yet observed")
    return {"live_orb": [{"ok": int(m[1]), "failed": int(m[2])} for m in matches], "account_stamps": stamps}


def census_after(repo, before):
    from project_mai_tai.oms.atr_reprice_handoff import old_buy_proven_clear
    db = database(repo, since=before["captured_at_utc"])
    for key in ("new_buys", "new_open_intents", "new_buy_fills"):
        need(not db[key], "post-checkpoint entry activity: " + key)
    prior = {str(row["id"]): row["payload"] for row in before["db"]["tickets"]}
    current = {str(row["id"]): row["payload"] for row in db["tickets"]}
    need(set(current) == set(prior), "ticket population changed; new shape needs review")
    disposition = []
    for token, job in current.items():
        old = prior[token]
        need(job["old"] == old["old"], "old ticket identity changed: " + token)
        phase = job["phase"]
        proven = old_buy_proven_clear(job)
        need(phase not in {"ready", "clear", "waiting", "cancelling", "prepared", "submitted", "submit_unknown"},
             "startup ticket unfinished/unproven: " + token)
        need(phase in {"expired", "refused", "held_unknown", "filled", "placed"}, "unknown phase: " + token)
        if phase == "placed":
            need(old["phase"] == "placed" and job.get("replacement_client_order_id") == old.get("replacement_client_order_id"),
                 "new saved placement after window: " + token)
        elif phase in {"expired", "refused"}:
            need(proven or phase == old["phase"], "unproven old order newly released: " + token)
        if old.get("no_rebuy"):
            need(job.get("no_rebuy"), "durable no-rebuy lost: " + token)
        disposition.append({"token": token, "phase": phase, "reason": job.get("reason"),
                            "old_proven_clear": proven, "no_rebuy": job.get("no_rebuy", False)})
    return {"dispositions": disposition, "new_buys": [], "new_open_intents": [], "new_buy_fills": [],
            "limit": "SQL activity/identity proof only; separate fresh broker dispatch census mandatory"}


def parse_gate(output, code, flags, numerics):
    total = sum(1 + len(row.get("also_check_services", [])) for row in flags["flags"] + numerics["settings"])
    numeric_names = {row["name"] for row in numerics["settings"]}
    numeric_total = sum(1 + len(row.get("also_check_services", [])) for row in numerics["settings"])
    need(total == 147 and numeric_total == 8, "unexpected final denominator; ROUND excluded")
    lines = output.splitlines()
    final = [line for line in lines if line.startswith("Final call:")]
    rows = [line for line in lines if re.match(r"^(PASS|UNKNOWN|REAL FAILURE) flag=", line)]
    need(len(rows) == total and len(final) == 1, "incomplete/repeated flag gate output")
    identities = [re.search(r"flag=(\S+) service=(\S+)", line).groups() for line in rows]
    expected = {(row["name"], service) for row in flags["flags"] + numerics["settings"]
                for service in [row["owning_service"], *row.get("also_check_services", [])]}
    need(len(set(identities)) == len(identities) and set(identities) == expected, "gate row identities disagree with catalog")
    need(not any(line.startswith("REAL FAILURE") for line in rows), "loaded gate mismatch")
    unknown = {identity for identity, line in zip(identities, rows) if line.startswith("UNKNOWN")}
    cold = [row for row in flags["flags"] if row["name"] == "market_data_subscription_startup_enabled"]
    need(len(cold) == 1 and "momentum-paper" in cold[0].get("also_check_services", []),
         "literal COLDSTART paper catalog identity missing")
    allowed = {("momentum_paper_enabled", "momentum-paper"), ("market_data_subscription_startup_enabled", "momentum-paper")}
    need(unknown == allowed and code == 2 and final == ["Final call: UNKNOWN; checked=145/147 mismatches=0 unknown=2"],
         "not the exact acknowledged paper UNKNOWN2 disposition")
    need(all(line.startswith("PASS") for identity, line in zip(identities, rows) if identity[0] in numeric_names),
         "numeric8/8 not proven")
    return {"actual_checker_rc": code, "actual_verdict": "UNKNOWN", "checked": 145, "total": total,
            "numeric_pass": 8, "numeric_total": 8, "unknown_rows": sorted(unknown),
            "disposition": "exact acknowledged paper coverage limit; NOT FLAGGATE PASS"}


def gates(args):
    for path, expected in ((args.checker, args.checker_sha256), (args.catalog, args.catalog_sha256),
                           (args.numeric, args.numeric_sha256)):
        need(re.fullmatch(r"[0-9a-f]{64}", expected) is not None and sha(read(path)) == expected, "isolated gate hash mismatch")
    need(service("momentum-paper")["MainPID"] == 0 and service("momentum-paper")["ActiveState"] == "inactive",
         "paper inactive coverage disposition no longer applicable")
    code, output, error = run([sys.executable, args.checker, "--catalog", args.catalog,
                               "--numeric-catalog", args.numeric], check=False)
    need(not error.strip(), "gate checker stderr; inspect without hiding")
    result = parse_gate(output, code, load(args.catalog), load(args.numeric))
    result["raw_output"] = output
    return result


def replace_assignment(script, key, value):
    pattern = rf"^{re.escape(key)}=.*$"
    need(len(re.findall(pattern, script, re.M)) == 1, "ambiguous preopen assignment: " + key)
    return re.sub(pattern, key + "=" + shlex.quote(str(value)), script, flags=re.M)


def repin(args):
    before, after = load(args.before), load(args.after)
    validate_checkpoint(before, after, "final")
    original = read(args.old_script).decode()
    need(sha(original.encode()) == args.old_sha256 == before["preopen_sha256"], "old preopen hash changed")
    need(Path(args.old_script).stat().st_mode & 0o777 == 0o700, "preopen mode not0700")
    snapshot, record = load(args.snapshot), load(args.install_record)
    need(snapshot["alembic_version"] == "20260916_0021", "official before snapshot not0021")
    need(record["snapshot_captured_at_utc"] == snapshot["captured_at_utc"], "official receipt timestamp mismatch")
    actions = record["service_actions"]
    need(set(actions) == set(snapshot["services"]), "official action census incomplete")
    need({name for name, action in actions.items() if action == "restarted"} == set(CHANGED), "restart declarations not exactthree")
    need(all(action == ("restarted" if name in CHANGED else "deliberately_untouched") for name, action in actions.items()),
         "new/extra service action not authorized")
    script = original
    for key, value in {"EXPECTED_DATE": NEXT, "EXPECTED_SHA": SHA, "SNAPSHOT": args.snapshot,
                       "INSTALL_RECORD": args.install_record}.items():
        script = replace_assignment(script, key, value)
    prefixes = {"oms": "OMS_", "strategy": "STRATEGY_", "schwab-1m-v2": "",
                "orb": "ORB_", "orb-schwab": "ORB_SCHWAB_", "market-data": "MARKET_DATA_",
                "momentum-paper": "PAPER_", "control": "CONTROL_"}
    for name, prefix in prefixes.items():
        state = after["services"][name]
        script = replace_assignment(script, "EXPECTED_" + prefix + "PID", state["MainPID"])
        script = replace_assignment(script, "EXPECTED_" + prefix + "START", state["ExecMainStartTimestamp"])
    for name in ("orb", "orb-schwab"):
        line = "  --restarted " + name + " \\\n"
        need(script.count(line) == 1, "old restart declaration missing")
        script = script.replace(line, "")
    need(script.count("--expected-alembic-head 20260916_0021") == 1 and script.count("--no-schema-change") == 1,
         "old schema declaration not uniquely identified")
    script = script.replace("--expected-alembic-head 20260916_0021", "--expected-alembic-head 20261005_0022")
    script = script.replace("  --no-schema-change \\\n", "  --schema-column oms_managed_positions.entry_order_id \\\n  --schema-column oms_managed_positions.entry_client_order_id \\\n")
    additions = "".join("  --expect-flag '" + name + ":" + flag + "=true' \\\n" for name in ("oms", "schwab-1m-v2") for flag in FLAGS)
    script = script.replace("  --expected-alembic-head 20261005_0022", additions + "  --expected-alembic-head 20261005_0022")
    need("v2-restart-evidence-20261006.md" in script, "preopen report date notOct6")
    # No check_identity function, helper routing or final-verdict logic is rewritten.
    paper = after["services"]["momentum-paper"]
    pending = paper["MainPID"] == 0
    return {"old_sha256": args.old_sha256, "candidate_sha256": sha(script.encode()), "candidate": script,
            "diff": "".join(unified_diff(original.splitlines(True), script.splitlines(True), fromfile="preopen.before", tofile="preopen.candidate")),
            "write_authorized": False, "required_backup": "parent O_EXCL copy preserving bytes/owner/mode; verify old hash before replace",
            "review_required": ["inactive-paper check_identity currently demands active/running; candidate PID0 stays REAL FAILURE, no routing bypass"] if pending else [],
            "required_validation": "bash -n candidate; mode0700; fresh snapshot/record and hash; isolated checker/catalog/numeric co-located exact target bytes; preserve helper routing; reviewer decision before first preopen write",
            "official_snapshot_command": [sys.executable, str(Path(args.repo) / "ops/health/v2_restart_evidence.py"), "snapshot", "--output", args.snapshot]}


def redis_proof(before=None):
    import redis
    from project_mai_tai.settings import Settings
    client = redis.Redis.from_url(Settings(_env_file=ENV_FILES[0]).redis_url, decode_responses=True,
                                  socket_timeout=5, socket_connect_timeout=5)
    prefix = Settings(_env_file=ENV_FILES[0]).redis_stream_prefix
    try:
        result = {"evicted_keys": client.info("stats")["evicted_keys"],
                  "used_memory": client.info("memory")["used_memory"]}
        need(result["used_memory"] <= 1_600_000_000, "Redis memory ceiling")
        if before is not None:
            need(result["evicted_keys"] == before["redis"]["evicted_keys"], "Redis eviction increased")
        key = prefix + ":market-data-subscription-owners"
        fields = ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper",
                  "_migration_complete", "_last_applied_id")
        need(client.hlen(key) == len(fields), "owner field count mismatch")
        need(sum(client.hstrlen(key, field) for field in fields) <= 100_000, "owner pre-read bound exceeded")
        owners = client.hgetall(key)
        need(set(owners) == set(fields) and owners["_migration_complete"] == "1", "owner/marker missing")
        for field in fields[:5]:
            need(isinstance(json.loads(owners[field]), list), "owner value not a symbol list")
        result["owners"] = owners
        streams = ("heartbeats", "market-data", "market-data-subscriptions", "order-events",
                   "runtime-controls", "snapshot-batches", "strategy-intents", "strategy-state", "strategy-state-isolated")
        result["stream_types"] = {name: client.type(prefix + ":" + name) for name in streams}
        need(all(kind == "stream" for kind in result["stream_types"].values()), "stream missing/wrong type")
        return result
    finally:
        client.close()


def armed_only(path):
    output = read(path).decode()
    blocks = [line.strip() for line in output.splitlines() if "[BLOCK]" in line]
    need(len(blocks) == 1 and re.fullmatch(r"\[BLOCK\] [1-9]\d* ARMED SEGMENT\(S\) \[published state, \d+s old\]: .+", blocks[0]) is not None,
         "refusal is not exclusively current published armed set")
    for text in ("[ok]    past 18:00 ET", "[ok]    zero open managed rows",
                 "[ok]    broker flat on both real-money accounts (operator manuals excluded)"):
        need(text in output, "other restart fence not explicitly passed")
    need("[OVERRIDE]" not in output, "override is not authorized")
    return {"disposition": "WAIT_ONLY_NO_WRITES", "block": blocks[0]}


def redis_safety(before_path, baseline_path=None):
    script = Path(__file__).resolve().parent / "redis_checkpoint.py"
    release = load(script.parent / "release.json")
    need(sha(read(script)) == release["artifacts"]["redis_checkpoint.py"], "Redis checkpoint artifact drift")
    baseline = Path(baseline_path) if baseline_path else Path(before_path).parent / "redis-baseline.json"
    code, output, error = run([sys.executable, str(script), "--baseline", str(baseline)], check=False)
    if code != 0 or error.strip():
        raise Blocked("Redis checkpoint refused/unreadable; stop now, no warmup retry")
    result = json.loads(output)
    prior = load(baseline)
    need(result["evicted_keys"] == prior["evicted_keys"] and
         result["stream_types"] == prior["stream_types"] and result["used_memory"] <= 1_600_000_000,
         "Redis safety result disagrees with baseline")
    return result


def logs_until(repo, before_path, started, until, redis_baseline=None):
    before = load(before_path)
    need(all(before["services"][name]["MainPID"] == 0 for name in CHANGED),
         "log baseline is not after all three old processes stopped")
    lower = moment(started)
    need(moment(before["captured_at_utc"]) <= lower <= datetime.now(UTC), "post-start lower bound invalid")
    deadline = time.monotonic() + until
    while True:
        safety = redis_safety(before_path, redis_baseline)  # Before logs/DB EVERY iteration, including --until0.
        result = post_logs(before)
        try:
            if not any("[V2-BOOT-HOLD]" in line for line in result["schwab-1m-v2"]["lines"]):
                raise Unknown("new PID BOOT-HOLD observation not yet available")
            if not any("[BROKER-SYNC-CENSUS]" in line for line in result["oms"]["lines"]):
                raise Unknown("new OMS sync census not yet available")
            result["sync"] = sync_proof(result, database(repo), lower)
            result["redis"] = safety
            result["started_lower_bound_utc"] = lower.isoformat()
            result["boot_hold_limit"] = "observed literal lines after old processes exited; not assumed released/PASS"
            return result
        except Unknown:
            if time.monotonic() >= deadline:
                raise
            time.sleep(min(5, max(0, deadline - time.monotonic())))


def start_time(state):
    _, value, _ = run(["date", "--date=" + state["ExecMainStartTimestamp"], "--iso-8601=seconds"])
    return moment(value.strip())


def bar_holes(repo, stopped, started):
    from sqlalchemy import create_engine, text
    from project_mai_tai.settings import Settings
    engine = create_engine(Settings(_env_file=ENV_FILES[0]).database_url, connect_args={"connect_timeout": 5})
    query = """
    WITH series AS (
      SELECT symbol,bar_time FROM strategy_bar_history
      WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND source='live'
        AND bar_time>=:stopped-interval '10 minutes' AND bar_time<=now()
    ), live_at_stop AS (
      SELECT symbol,max(bar_time) last_at_stop FROM series WHERE bar_time<=:stopped
      GROUP BY symbol HAVING max(bar_time)>=:stopped-interval '180 seconds'
    ) SELECT live.symbol,live.last_at_stop,
      (SELECT max(bar_time) FROM series s WHERE s.symbol=live.symbol AND bar_time<:started) prior,
      (SELECT min(bar_time) FROM series s WHERE s.symbol=live.symbol AND bar_time>:started) following
      FROM live_at_stop live ORDER BY live.symbol LIMIT 65
    """
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            connection.exec_driver_sql("SET LOCAL statement_timeout='5s'")
            rows = [dict(row) for row in connection.execute(text(query),
                {"stopped": stopped, "started": started}).mappings()]
            connection.rollback()
    finally:
        engine.dispose()
    if len(rows) > 64:
        raise Unknown("bar-hole proof overflow")
    pending = [row["symbol"] for row in rows if row["prior"] is None or row["following"] is None]
    gaps = [row["symbol"] for row in rows if row["prior"] is not None and row["following"] is not None
            and (moment(row["following"]) - moment(row["prior"])).total_seconds() > 90]
    return {"stop_checkpoint_utc": stopped, "new_v2_start_utc": started, "live_at_stop": len(rows),
            "rows": rows, "pending": pending, "spanning_over90s": gaps,
            "verdict": "PASS" if rows and not pending and not gaps else "UNKNOWN",
            "limit": "quiet/empty or >90s needs reviewed v2_restart_evidence report/independent market-silence proof; not waived"}


def runner_mode(command, arguments):
    """Positional hooks emit JSON; parent redirects it to O_EXCL receipts."""
    repo = "/home/trader/project-mai-tai"
    sys.path.insert(0, repo + "/src")
    if command == "armed-only":
        wall(action=True)
        return armed_only(arguments[0])
    name = arguments[0] if command in {"stopped", "started"} else None
    attempt = Path(arguments[1] if name else arguments[0])
    if command == "capture":
        result = capture(repo)
        result["redis"] = redis_proof()
        need(result["db"]["revision"] == [{"version_num": "20260916_0021"}], "initial revision not0021")
        need(result["head"] == "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89", "initial box SHA drift")
        return result
    before = load(attempt / "proof-before.json")
    if command == "redis":
        return redis_proof(before)
    wall(action=True)
    if command == "verify-source":
        release = load(attempt.parent / "release.json")
        _, tree, _ = run(["git", "-C", repo, "rev-parse", SHA + "^{tree}"])
        need(tree.strip() == TREE, "approved source tree drift")
        _, migration, _ = run(["git", "-C", repo, "show", SHA + ":sql/migrations/versions/20261005_0022_managed_entry_binding.py"])
        need(sha(migration.encode()) == release["migration_sha256"], "release migration SHA mismatch")
        for filename in ("expected_flags_check.py", "expected_flags.json", "expected_numeric.json"):
            _, content, _ = run(["git", "-C", repo, "show", SHA + ":ops/health/" + filename])
            need(sha(content.encode()) == release["artifacts"][filename], "isolated artifact not approved source: " + filename)
        return {"approved_sha": SHA, "tree": TREE, "migration_sha256": release["migration_sha256"]}
    if command == "gates":
        release = load(attempt.parent / "release.json")
        args = argparse.Namespace()
        for key, filename in (("checker", "expected_flags_check.py"), ("catalog", "expected_flags.json"), ("numeric", "expected_numeric.json")):
            setattr(args, key, str(attempt.parent / filename))
            setattr(args, key + "_sha256", release["artifacts"][filename])
        return gates(args)
    if command == "repin":
        return repin(argparse.Namespace(repo=repo, before=str(attempt / "proof-before.json"), after=str(attempt / "final.json"),
            old_script="/home/trader/preopen.sh", old_sha256=before["preopen_sha256"],
            snapshot=str(attempt / "before-restart.json"), install_record=str(attempt / "install-record.json")))
    if command == "warmup":
        post_stop = load(attempt / "oms-stopped.json")
        deadline = time.monotonic() + 180
        while True:
            logs = post_logs(post_stop)
            need(active(service("strategy")), "strategy not active during warmup")
            evidence = [line for line in logs["strategy"]["lines"] if "prefilled momentum alert history from" in line]
            if evidence:
                return {"logs": logs, "warmup_lines": evidence, "limit": "bar-hole/BOOT-HOLD and sync validated separately; no assumed release"}
            if time.monotonic() >= deadline:
                raise Unknown("warmup completion not measured in180s")
            time.sleep(5)
    after = capture(repo)
    if command == "unchanged":
        need(after["head"] == SHA and after["tree"] == TREE, "not approved checkout")
        need(after["services"] == before["services"], "service drift before first stop")
        need(after["env_files"][ENV_FILES[1]] == before["env_files"][ENV_FILES[1]], "secondary env changed")
        return after
    phases = {("stopped", "schwab-1m-v2"): "v2-stopped", ("stopped", "strategy"): "strategy-stopped",
              ("stopped", "oms"): "oms-stopped", ("started", "oms"): "oms-started", ("started", "schwab-1m-v2"): "v2-started"}
    phase = "migrated" if command == "schema" else "final" if command == "final" else phases[(command, name)]
    validate_checkpoint(before, after, phase)
    if command == "final":
        after["proc"] = proc_flags(after)
        after["redis"] = redis_proof(before)
        post_stop = load(attempt / "oms-stopped.json")
        oms_receipt = load(attempt / "oms-started.json")
        need(oms_receipt["services"]["oms"] == after["services"]["oms"], "OMS receipt no longer same PID")
        logs = post_logs(post_stop)
        oms_start = moment(oms_receipt["captured_at_utc"])
        # Restrict sync and BOOT-HOLD to the NEW process, not old setup logs.
        for label in ("oms", "schwab-1m-v2"):
            cutoff = start_time(after["services"][label])
            recent = []
            for line in logs[label]["lines"]:
                match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", line)
                if match and datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=UTC) >= cutoff:
                    recent.append(line)
            logs[label]["lines"] = recent
        after["sync"] = sync_proof(logs, after["db"], oms_start)
        need(any("[V2-BOOT-HOLD]" in line for line in logs["schwab-1m-v2"]["lines"]), "new PID BOOT-HOLD unobserved")
        after["post_logs"] = logs
        after["census_after"] = census_after(repo, before)
        v2_stopped = load(attempt / "v2-stopped.json")
        after["bar_continuity"] = bar_holes(repo, moment(v2_stopped["captured_at_utc"]),
                                            start_time(after["services"]["schwab-1m-v2"]))
        after["proof_verdict"] = after["bar_continuity"]["verdict"]
        after["official_report_required"] = "v2_restart_evidence.py report with new snapshot/install-record/schema columns; do not execute date-fixed preopen early"
    return after


def main():
    hooks = {"redis", "capture", "armed-only", "verify-source", "unchanged", "stopped", "started", "schema", "warmup", "final", "gates", "repin"}
    if len(sys.argv) >= 3 and sys.argv[1] in hooks and not sys.argv[2].startswith("--"):
        try:
            result = runner_mode(sys.argv[1], sys.argv[2:])
            print(json.dumps(result, indent=2, default=str))
            return 2 if (sys.argv[1] == "repin" and result["review_required"]) or result.get("proof_verdict") == "UNKNOWN" else 0
        except Blocked as exc:
            print(json.dumps({"verdict": "BLOCKED", "reason": str(exc), "recovery_authorized": False}))
            return 1
        except Exception as exc:
            print(json.dumps({"verdict": "UNKNOWN", "error_type": type(exc).__name__, "recovery_authorized": False}))
            return 2
    return detailed_main()


def detailed_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="/home/trader/project-mai-tai")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capture")
    check = commands.add_parser("checkpoint")
    check.add_argument("--before", required=True)
    check.add_argument("--phase", required=True, choices=("v2-stopped", "strategy-stopped", "oms-stopped", "migrated", "oms-started", "v2-started", "final"))
    proc = commands.add_parser("proc")
    proc.add_argument("--after", required=True)
    logs = commands.add_parser("logs")
    logs.add_argument("--before", required=True)
    logs.add_argument("--started", required=True, help="UTC marker captured AFTER OMS start command returned")
    logs.add_argument("--redis-baseline", required=True, help="read-only Redis checkpoint baseline JSON")
    logs.add_argument("--until", type=int, default=0, choices=range(181))
    census = commands.add_parser("census-after")
    census.add_argument("--before", required=True)
    bars = commands.add_parser("bar-holes")
    bars.add_argument("--stopped", required=True, help="v2-stopped checkpoint JSON")
    bars.add_argument("--after", required=True, help="final checkpoint JSON")
    gate = commands.add_parser("gates")
    for key in ("checker", "catalog", "numeric"):
        gate.add_argument("--" + key, required=True)
        gate.add_argument("--" + key + "-sha256", required=True)
    pin = commands.add_parser("repin")
    for key in ("before", "after", "old-script", "old-sha256", "snapshot", "install-record"):
        pin.add_argument("--" + key, required=True)
    args = parser.parse_args()
    try:
        if args.command == "capture":
            result = capture(args.repo)
        elif args.command == "checkpoint":
            result = capture(args.repo)
            validate_checkpoint(load(args.before), result, args.phase)
        elif args.command == "proc":
            result = proc_flags(load(args.after))
        elif args.command == "census-after":
            sys.path.insert(0, str(Path(args.repo).resolve() / "src"))
            wall()
            result = census_after(args.repo, load(args.before))
        elif args.command == "bar-holes":
            sys.path.insert(0, str(Path(args.repo).resolve() / "src"))
            before, after = load(args.stopped), load(args.after)
            need(before["services"]["schwab-1m-v2"]["MainPID"] == 0, "v2 stop checkpoint not stopped")
            need(after["services"]["schwab-1m-v2"] == service("schwab-1m-v2"), "v2 identity moved before bar query")
            result = bar_holes(args.repo, moment(before["captured_at_utc"]), start_time(after["services"]["schwab-1m-v2"]))
        elif args.command == "gates":
            result = gates(args)
        elif args.command == "repin":
            result = repin(args)
        else:
            result = logs_until(args.repo, args.before, args.started, args.until, args.redis_baseline)
        print(json.dumps(result, indent=2, default=str))
        return 2 if (args.command == "repin" and result["review_required"]) or result.get("verdict") == "UNKNOWN" else 0
    except Blocked as exc:
        print(json.dumps({"verdict": "BLOCKED", "reason": str(exc), "recovery_authorized": False}))
        return 1
    except Exception as exc:
        # Driver/DB exceptions may contain a protected URL: only emit the type.
        print(json.dumps({"verdict": "UNKNOWN", "error_type": type(exc).__name__, "recovery_authorized": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
