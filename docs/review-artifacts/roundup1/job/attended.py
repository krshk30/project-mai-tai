"""Literal attended Oct6 mechanics. Root execution requires an exact published release approval.

No trading API writes, migration, rollback, recovery, or non-scoped application actions.
Tests inject effects into Sequence, not a production fake-mode CLI.
"""
from datetime import datetime, timezone
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import signal
import stat
import subprocess
import sys
import time
from io import StringIO
from dotenv.parser import parse_stream

from daily import exclusive
from release_policy import (APP, BASELINE_GATE, BOX, CHANGED, ENV_UPDATES, ET, NEW_ENV, NUMERIC_ARTIFACT, PHASES, RETRY_ENABLED,
                            SERVICES, TREE, Stop, approval, canonical, digest, first_write_window,
                            need, row47, states)

REPO = Path("/home/trader/project-mai-tai")
PY = REPO / ".venv/bin/python"
ENV = Path("/etc/project-mai-tai/project-mai-tai.env")
GATE = Path("/home/trader/preopen.sh")
UNIT_FIELDS = ("MainPID", "NRestarts", "ActiveState", "SubState", "Result", "ExecMainCode",
               "ExecMainStatus", "ExecMainStartTimestamp", "ExecMainStartTimestampMonotonic",
               "FragmentPath", "DropInPaths", "EnvironmentFiles", "InactiveEnterTimestamp", "InvocationID")


class Sequence:
    def __init__(self, effects):
        self.fx = effects
        self.phase = "initial"
        self.completed = 0
        self.first_stopped = False

    def run(self):
        try:
            self.fx.initial()
            self.phase = "backup-and-source-env"
            first_write_window(self.fx.now())
            self.fx.prepare()
            for action, name in PHASES:
                self.phase = action + "-" + name
                self.fx.gates(self.completed)
                if action == "stop" and name == "schwab-1m-v2":
                    self.fx.v2_gate()
                    first_write_window(self.fx.now())
                if action == "stop" and name == "oms":
                    self.fx.oms_gate()
                self.fx.action(action, name)
                if action == "stop":
                    self.first_stopped = True
                self.completed += 1
                self.fx.checkpoint(self.completed)
            self.phase = "post-start-proof"
            self.fx.finish_proof()
            self.phase = "single-preopen-timer-closeout"
            self.fx.closeout()
            self.phase = "complete"
            self.fx.complete()
        except BaseException:
            self.fx.abort(self.phase, self.completed)
            raise


def parse_helper(raw):
    index = 0 if raw.startswith("{") else raw.find("\n{") + 1
    need(index >= 0, "strict helper lacks JSON receipt")
    result = json.loads(raw[index:])
    need(result["rc"] == 0 and not result["blockers"], "strict admission blocked")
    return result


def env_candidate(raw):
    lines = raw.decode().splitlines(keepends=True)
    bindings = list(parse_stream(StringIO(raw.decode())))
    need(not any(binding.error for binding in bindings), "malformed EnvironmentFile")
    retained = [(binding.key, binding.value) for binding in bindings
                if binding.key is not None and binding.key.upper() == RETRY_ENABLED]
    need(retained == [(RETRY_ENABLED, "true")], "retry-enabled must already be explicit true unchanged")
    seen = {}
    for i, line in enumerate(lines):
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            key = match[1].upper()
            need(key not in seen, "duplicate/case-alias EnvironmentFile definition")
            need(key not in ENV_UPDATES or match[1] == key, "case-alias env update forbidden")
            seen[key] = i
    for key, value in ENV_UPDATES.items():
        if key in seen:
            lines[seen[key]] = key + "=" + value + "\n"
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(key + "=" + value + "\n")
    return "".join(lines).encode()


def local_database_binding(rows):
    need(len(rows) == 1 and rows[0].get("database") == "project_mai_tai", "gate database name differs")
    value = rows[0].get("server_address")
    if value is None:
        return  # Local Unix socket, as already admitted.
    try:
        address = ipaddress.ip_interface(value)
    except (ValueError, TypeError):
        raise Stop("gate database address unreadable") from None
    need(address.ip in {ipaddress.ip_address("127.0.0.1"), ipaddress.ip_address("::1")}
         and address.network.prefixlen == address.max_prefixlen, "hardtuple gate DSN not local production database")


def verify(job, expected, now):
    raw = (job / "release.json").read_bytes()
    need(digest(raw) == expected, "published release hash drift")
    release = json.loads(raw)
    need(not release.get("blocking_acceptance"), "immutable release has unresolved acceptance blockers; cannot run")
    approval(release, expected, json.loads((job / "approval.json").read_bytes()), now)
    for name, sha in release["artifacts"].items():
        need(Path(name).name == name, "release path escape")
        file = job / name
        need(not file.is_symlink() and file.stat().st_uid == 0 and file.stat().st_mode & 0o022 == 0
             and digest(file.read_bytes()) == sha, "release artifact owner/mode/hash drift: " + name)
    for file in (job, job / "release.json", job / "approval.json"):
        need(not file.is_symlink() and file.stat().st_uid == 0 and file.stat().st_mode & 0o022 == 0,
             "release directory/approval writable outside root")
    return release


class Real:
    def __init__(self, job, release, attempt):
        self.job, self.release, self.attempt = job, release, attempt
        self.counter = 0
        self.before = None
        self.redis_baseline = None
        self.log_base = None
        self.latest_flat = None
        self.started = {}
        self.start_returned = {}
        self.last = None

    def now(self):
        return datetime.now(timezone.utc)

    def receipt(self, name, raw):
        self.counter += 1
        path = self.attempt / f"{self.counter:03d}-{name}"
        exclusive(path, raw)
        journal = self.attempt / "runner-journal.jsonl"
        fd = os.open(journal, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as file:
            metadata = os.fstat(file.fileno())
            need(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
                 and metadata.st_mode & 0o022 == 0, "unsafe runner journal")
            at = self.now().isoformat()
            file.write((json.dumps(dict(at_utc=at, receipt=path.name,
                         sha256=digest(raw), bytes=len(raw)), sort_keys=True) + "\n").encode())
        print(at + " receipt=" + path.name, flush=True)

    def command(self, args, *, check=True, timeout=30, limit=3_000_000):
        args = list(map(str, args))
        # This list is literal code, never supplied by an approval file or arbitrary CLI input.
        result = subprocess.run(args, capture_output=True, timeout=timeout)
        need(len(result.stdout) + len(result.stderr) <= limit, "command output exceeds bound")
        self.receipt("command.json", canonical(dict(argv=args, rc=result.returncode,
                                                    at_utc=self.now().isoformat())))
        self.receipt("stdout.txt", result.stdout)
        self.receipt("stderr.txt", result.stderr)
        if check:
            need(result.returncode == 0, "command failed: " + args[0])
        return result

    def reader(self, name, *args):
        for count in range(3):
            result = self.command(["nice", "-n", "19", PY, self.job / name, *args], check=False,
                                  timeout=660 if name == "census_readonly.py" else 210, limit=8_000_000)
            if result.returncode == 0:
                return result.stdout.decode()
            need(result.returncode == 2 and count < 2, "read-only blocker or exhausted UNKNOWN: " + name)
            time.sleep(60)
        raise Stop("unreachable retry")

    def fleet(self):
        result = {}
        for name in SERVICES:
            # Redis/Postgres service names do not share the trading unit prefix.
            unit = name + ".service" if name in {"redis", "postgresql"} else "project-mai-tai-" + name + ".service"
            output = self.command(["systemctl", "show", unit, "--all", *["--property=" + field for field in UNIT_FIELDS]])
            state = dict(line.split("=", 1) for line in output.stdout.decode().splitlines() if "=" in line)
            if name in {"redis", "postgresql"} and "EnvironmentFiles" not in state:
                # systemctl omits this empty array even with --all; prove it via typed D-Bus.
                object_reply = self.command(["busctl", "call", "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "GetUnit", "s", unit]).stdout.decode().strip()
                need(object_reply.startswith("o "), "system unit D-Bus object unreadable")
                object_path = json.loads(object_reply[2:])
                need(re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", object_path), "system unit D-Bus path unreadable")
                array = self.command(["busctl", "get-property", "org.freedesktop.systemd1", object_path,
                    "org.freedesktop.systemd1.Service", "EnvironmentFiles"]).stdout.decode().strip()
                need(array == "a(sb) 0", "omitted system EnvironmentFiles not proven empty")
                state["EnvironmentFiles"] = ""
            need(set(state) == set(UNIT_FIELDS), "unreadable fleet state: " + name)
            for key in ("MainPID", "NRestarts", "ExecMainCode", "ExecMainStatus", "ExecMainStartTimestampMonotonic"):
                state[key] = int(state[key])
            result[name] = state
        return result

    def sql(self, since=None):
        sys.path.insert(0, str(REPO / "src"))
        from sqlalchemy import create_engine, text
        from project_mai_tai.settings import Settings
        engine = create_engine(Settings(_env_file=ENV).database_url, connect_args={"connect_timeout": 5})
        queries = {
            "database_binding": "SELECT current_database() database,inet_server_addr()::text server_address",
            "revision": "SELECT version_num FROM alembic_version LIMIT 2",
            "columns": "SELECT column_name,data_type,character_maximum_length,is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name='oms_managed_positions' AND column_name IN ('entry_order_id','entry_client_order_id') ORDER BY column_name LIMIT 3",
            "ticket_count": "SELECT count(*) total FROM dashboard_snapshots WHERE snapshot_type='atr_reprice_handoff'",
            "tickets": "SELECT id,payload FROM dashboard_snapshots WHERE snapshot_type='atr_reprice_handoff' ORDER BY id LIMIT 1025",
        }
        if since:
            queries["buys"] = "SELECT b.id FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND lower(b.side)='buy' AND b.submitted_at>=:since LIMIT 65"
            queries["buy_intents"] = "SELECT t.id FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND lower(t.side)='buy' AND t.created_at>=:since LIMIT 65"
            queries["buy_fills"] = "SELECT f.id FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND upper(f.side)='BUY' AND f.filled_at>=:since LIMIT 65"
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.exec_driver_sql("SET LOCAL statement_timeout='5s'")
                result = {key: [dict(row) for row in connection.execute(text(query), {"since": since}).mappings()]
                          for key, query in queries.items()}
                need(all(len(rows) <= (1024 if key == "tickets" else 64)
                         for key, rows in result.items()), "SQL proof overflow")
                need(result["ticket_count"] == [{"total": len(result["tickets"])}], "incomplete all-date ticket capture")
                connection.rollback()
        finally:
            engine.dispose()
        need(result["revision"] == [{"version_num": "20261005_0022"}], "installed schema not0022; no migration")
        local_database_binding(result["database_binding"])
        columns = {row["column_name"]: row for row in result["columns"]}
        need(set(columns) == {"entry_order_id", "entry_client_order_id"}
             and columns["entry_order_id"]["data_type"] == "uuid"
             and columns["entry_client_order_id"]["data_type"] == "character varying"
             and columns["entry_client_order_id"]["character_maximum_length"] == 128
             and all(row["is_nullable"] == "YES" for row in columns.values()), "binding schema differs")
        self.receipt("sql.json", json.dumps(result, sort_keys=True, default=str).encode())
        from ticket_inventory import require_idle, stable_bindings
        try:
            self.receipt("ticket-dispositions.json", canonical(require_idle(result["tickets"])))
        except ValueError as exc:
            raise Stop(str(exc)) from None
        if since:
            need(not any(result[key] for key in ("buys", "buy_intents", "buy_fills")), "entry activity during install")
            try:
                stable_bindings(self.db_before["tickets"], result["tickets"])
            except ValueError as exc:
                raise Stop(str(exc)) from None
        return result

    def redis(self):
        args = [] if self.redis_baseline is None else ["--baseline", self.redis_baseline]
        result = self.command([PY, self.job / "redis_checkpoint.py", *args])
        if self.redis_baseline is None:
            self.redis_baseline = self.attempt / "redis-baseline.json"
            exclusive(self.redis_baseline, result.stdout)

    def flat(self, service=None):
        args = ["--service", service] if service else []
        self.latest_flat = parse_helper(self.reader("strict_flat_readonly.py", *args))
        return self.latest_flat

    def oms_gate(self):
        self.restart_gate("oms", [REPO / "ops/preflight/preflight_oms_restart.sh", "--require-all-account-positions-flat"])

    def restart_gate(self, name, args, clock_reason=None):
        from raw_gate_admission import admit, original_go
        before = self.flat()
        if name == "v2":
            self.armed()
        result = self.command(args, check=False)
        if name == "v2":
            self.armed()
        after = self.flat()
        if result.returncode:
            receipt = admit(name, result.returncode, result.stdout.decode(), before, after, self.now(), clock_reason=clock_reason)
            need(not result.stderr.strip(), "gate stderr unreadable; cannot admit")
            self.receipt(name + "-dated-residual-admission.json", canonical(receipt))
        else:
            need(not before["broker_holdings"] and not after["broker_holdings"], "original GO conflicts with direct nonflat proof")
            need(not result.stderr.strip(), "zero gate rc has unreadable stderr")
            original_go(name, result.returncode, result.stdout.decode(), clock_reason=clock_reason)

    def armed(self):
        # No retry into an empty/failed query: one explicit bounded current read.
        self.command([PY, self.job / "armed_readonly.py"], timeout=15, limit=300_000)

    def v2_gate(self):
        args = [REPO / "ops/preflight/preflight_v2_restart.sh"]
        reason = None
        if self.now().astimezone(ET).hour < 18:
            reason = "Operator 2026-10-06 after-close ruling; after16:00 clock only; raw armed/managed checks remain mandatory; exact dated IPDN residual policy, NOT whole-account flatness"
            args += ["--clock-override", reason, "--i-accept-clock"]
        self.restart_gate("v2", args, reason)

    def source(self, sha, *, verify_objects=True):
        head = self.command(["runuser", "-u", "trader", "--", "git", "-C", REPO, "rev-parse", "HEAD"]).stdout.decode().strip()
        dirty = self.command(["runuser", "-u", "trader", "--", "git", "-C", REPO, "status", "--porcelain"]).stdout
        need(head == sha and not dirty, "checkout head/cleanliness drift")
        if verify_objects:
            for path, expected in self.release["application_blobs"].items():
                result = self.command(["git", "-C", REPO, "show", APP + ":" + path])
                need(digest(result.stdout) == expected, "candidate blob drift")
        tree = self.command(["git", "-C", REPO, "rev-parse", APP + "^{tree}"]).stdout.decode().strip()
        need(tree == TREE, "candidate whole tree drift")
        if sha == APP:
            for path, expected in self.release["application_blobs"].items():
                need(digest((REPO / path).read_bytes()) == expected, "working source blob drift")

    def initial(self):
        first_write_window(self.now())
        self.source(BOX)
        self.command(["git", "-C", REPO, "merge-base", "--is-ancestor", BOX, APP])
        self.command(["git", "-C", REPO, "merge-base", "--is-ancestor", APP, "origin/main"])
        paths = self.command(["git", "-C", REPO, "diff", "--name-only", APP, "origin/main"]).stdout.decode().splitlines()
        need(all(path.startswith("docs/") for path in paths), "moving main has application changes")
        need(digest(GATE.read_bytes()) == BASELINE_GATE, "preopen baseline drift")
        need(not ENV.is_symlink() and ENV.stat().st_uid == 0 and ENV.stat().st_mode & 0o777 == 0o600, "env mode/owner")
        self.env_before = ENV.read_bytes()
        self.env_after = env_candidate(self.env_before)
        from difflib import unified_diff
        exclusive(self.attempt / "env.prewrite.diff", "".join(unified_diff(
            self.env_before.decode().splitlines(True), self.env_after.decode().splitlines(True),
            fromfile="env.before", tofile="env.reviewed-after", n=0)).encode())
        from retry_zero_readonly import catalog
        numeric = self.command(["git", "-C", REPO, "show", APP + ":ops/health/expected_numeric.json"]).stdout
        reviewed = (self.job / NUMERIC_ARTIFACT).read_bytes()
        catalog(reviewed, numeric)
        exclusive(self.attempt / "numeric.prewrite.diff", "".join(unified_diff(
            numeric.decode().splitlines(True), reviewed.decode().splitlines(True),
            fromfile="golden-source-numeric", tofile="reviewed-isolated-numeric")).encode())
        self.receipt("prewrite-env-catalog.json", canonical(dict(before_sha256=digest(self.env_before),
                     after_sha256=digest(self.env_after), env_updates=ENV_UPDATES,
                     retained={RETRY_ENABLED: "true"}, source_numeric_sha256=digest(numeric),
                     reviewed_numeric_sha256=digest(reviewed), numeric_checks=10, total_checks=153)))
        self.before = self.fleet()
        need(all(self.before[name]["MainPID"] > 0 and self.before[name]["ActiveState"] == "active"
                 and self.before[name]["NRestarts"] == 0 and self.before[name]["Result"] == "success"
                 for name in CHANGED), "owning services not healthy before attempt")
        exclusive(self.attempt / "fleet-before.json", canonical(self.before))
        self.db_before = self.sql()
        self.flat("oms")
        self.flat("strategy")
        self.reader("census_readonly.py", "--require-reviewed")
        self.redis()
        self.oms_gate()
        self.v2_gate()
        self.control_proof("before", self.before["control"]["MainPID"])
        self.install_started = self.now()
        self.preflight_targets()

    def preflight_targets(self):
        from closeout import CATALOGS, HELPERS, ROOT, UNIT_DIRECTORY
        root = ROOT
        need(not root.exists(), "unexpected existing daily installation; do not overwrite")
        for name in ("project-mai-tai-preopen.service", "project-mai-tai-preopen-failure.service", "project-mai-tai-preopen.timer"):
            need(not (UNIT_DIRECTORY / name).exists(), "unexpected preopen unit")
        need(GATE.stat().st_uid == pwd.getpwnam("trader").pw_uid and GATE.stat().st_mode & 0o777 == 0o700, "gate owner/mode")
        self.old_helpers = {}
        for name in CATALOGS:
            path = HELPERS / name
            need(path.is_file() and not path.is_symlink(), "missing/symlink isolated helper baseline")
            expected = self.command(["git", "-C", REPO, "show", BOX + ":ops/health/" + name]).stdout
            need(digest(path.read_bytes()) == digest(expected), "isolated helper not reviewed BOX baseline: " + name)
            self.old_helpers[name] = digest(expected)

    def prepare(self):
        self.gates(0)
        need(ENV.read_bytes() == self.env_before, "env changed after prewrite admission")
        from retry_zero_readonly import catalog
        catalog((self.job / NUMERIC_ARTIFACT).read_bytes(),
                self.command(["git", "-C", REPO, "show", APP + ":ops/health/expected_numeric.json"]).stdout)
        # Backups precede all source/env/catalog/gate replacement, with O_EXCL and hashes.
        for label, path in (("env", ENV), ("preopen", GATE)):
            exclusive(self.attempt / (label + ".before"), path.read_bytes())
        from closeout import HELPERS
        for name, expected in self.old_helpers.items():
            raw = (HELPERS / name).read_bytes()
            need(digest(raw) == expected, "isolated helper changed before source backup")
            exclusive(self.attempt / (name + ".before"), raw)
        archive = self.command(["git", "-C", REPO, "archive", BOX], limit=40_000_000).stdout
        exclusive(self.attempt / "source-before.tar", archive)
        self.receipt("backup-hashes.json", canonical({name: digest((self.attempt / name).read_bytes())
                      for name in ("env.before", "preopen.before", "source-before.tar")}))
        self.command([PY, REPO / "ops/health/v2_restart_evidence.py", "snapshot", "--output", self.attempt / "before-restart.json"])
        self.command(["runuser", "-u", "trader", "--", "git", "-C", REPO, "switch", "--detach", APP])
        self.command(["runuser", "-u", "trader", "--", REPO / ".venv/bin/pip", "install", "--no-deps", "-e", REPO], timeout=120)
        self.source(APP)
        self.command(["runuser", "-u", "trader", "--", PY, "-c", 'import project_mai_tai; assert project_mai_tai.__file__.startswith("/home/trader/project-mai-tai/src/")'])
        self.flat()
        need(ENV.read_bytes() == (self.attempt / "env.before").read_bytes(), "env changed during source advance")
        original = ENV.read_bytes()
        updated = env_candidate(original)
        need(updated == self.env_after, "env candidate differs from prewrite receipt")
        from difflib import unified_diff
        diff = "".join(unified_diff(original.decode().splitlines(True), updated.decode().splitlines(True),
                                    fromfile="env.before", tofile="env.after", n=0))
        exclusive(self.attempt / "env.diff", diff.encode())
        self.replace(ENV, updated, 0o600, ENV.stat().st_uid, ENV.stat().st_gid)
        self.receipt("env-hashes.json", canonical(dict(before_sha256=digest(original), after_sha256=digest(updated),
                                                     diff_sha256=digest(diff.encode()), enabled_keys=NEW_ENV,
                                                     env_updates=ENV_UPDATES, retained={RETRY_ENABLED: "true"},
                                                     retry_key_removed=False)))

    def replace(self, path, raw, mode, uid=0, gid=0):
        temporary = path.with_name(path.name + ".oct6-exclusive.tmp")
        exclusive(temporary, raw, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
        need(digest(path.read_bytes()) == digest(raw) and path.stat().st_mode & 0o777 == mode, "installed bytes/mode drift")

    def gates(self, completed):
        current = self.fleet()
        states(self.before, current, completed)
        for name, identity in self.started.items():
            need(current[name] == identity, "new owning process changed between phases")
        self.flat()
        self.reader("census_readonly.py", "--require-reviewed")
        self.redis()
        self.sql(self.install_started)
        if completed:
            self.source(APP, verify_objects=False)
        # Historical reads may be slow; refresh direct flatness last before each action.
        self.flat()

    def action(self, action, name):
        need((action, name) in PHASES, "out-of-scope service action")
        unit = "project-mai-tai-" + name + ".service"
        before = self.fleet()[name]
        if action in {"stop", "restart"}:
            need(before == self.before[name], "intended old process changed before stop")
        if action == "restart":
            need(name == "control", "only control atomic restart authorized")
            self.control_proof("before", before["MainPID"])
            path = Path("/var/log/project-mai-tai/control.log")
            stat = path.stat()
            self.log_base["control"] = dict(path=str(path), inode=stat.st_ino, device=stat.st_dev, offset=stat.st_size)
        since = self.now()
        result = self.command(["systemctl", action, unit], check=False, timeout=120)
        after = self.fleet()[name]
        if action == "stop" and name == "orb-schwab" and after["ActiveState"] == "failed":
            need(result.returncode == 0 and not result.stderr.strip(), "intended ORB stop command not proven successful")
            until = self.now()
            raw = self.command(["journalctl", "--unit=" + unit, "--since=" + since.isoformat(),
                                "--until=" + until.isoformat(), "--output=json", "--no-pager", "--lines=257"],
                               timeout=10, limit=500_000).stdout
            rows = [json.loads(line) for line in raw.decode().splitlines()]
            need(len(rows) <= 256, "shutdown journal overflow")
            classification = row47(before, after, rows, since, until, intended=True, unit=unit)
            self.receipt("row47-classification.json", canonical(classification))
            # Exactly one named reset after complete proof; never a blanket failed-unit reset.
            self.command(["systemctl", "reset-failed", unit])
            cleaned = self.fleet()[name]
            need(cleaned["MainPID"] == 0 and cleaned["ActiveState"] == "inactive" and cleaned["Result"] == "success", "ORB reset not clean/PID0")
        else:
            need(result.returncode == 0, "intended systemctl action failed")
            if action == "restart":
                need(not result.stderr.strip(), "control restart stderr unreadable")
        if action in {"start", "restart"}:
            self.start_returned[name] = self.now()
            self.receipt(name + "-start-returned.json", canonical(dict(post_return_utc=self.start_returned[name].isoformat())))
        if action == "restart":
            need(0 <= (self.now() - since).total_seconds() <= 120, "control restart exceeds120s owner-gap bound")
            states(self.before, self.fleet(), len(PHASES))
            self.control_proof("after", after["MainPID"], before["MainPID"])

    def control_proof(self, phase, pid, old_pid=None):
        from control_display_proof import validate
        args = [PY, self.job / "control_display_proof.py", "--phase", phase, "--pid", str(pid)]
        if old_pid is not None:
            args += ["--old-pid", str(old_pid)]
        since = self.now()
        result = self.command(args, check=False, timeout=60, limit=2_000_000)
        need(result.returncode == 0 and not result.stderr.strip(), "control owner/page proof blocked")
        value = validate(json.loads(result.stdout), phase, pid, old_pid, since, self.now())
        oms = self.started.get("oms", self.before["oms"])
        need(value["adapter"]["pid"] == oms["MainPID"], "control proof not bound to phase-specific OMS identity")
        self.latest_control = value
        self.receipt("control-" + phase + "-proof.json", canonical(value))

    def checkpoint(self, completed):
        current = self.fleet()
        states(self.before, current, completed)
        for action, name in PHASES[:completed]:
            if action in {"start", "restart"} and name not in self.started:
                self.started[name] = current[name]
        for name, expected in self.started.items():
            need(current[name] == expected, "new process drift after start")
        self.last = current
        exclusive(self.attempt / f"phase-{completed}.json", canonical(current))
        if completed == 3:
            self.log_base = {}
            for name in CHANGED:
                path = Path("/var/log/project-mai-tai") / (name + ".log")
                stat = path.stat()
                self.log_base[name] = dict(path=str(path), inode=stat.st_ino, device=stat.st_dev, offset=stat.st_size)
            exclusive(self.attempt / "logs-old-processes-stopped.json", canonical(self.log_base))

    def finish_proof(self):
        self.gates(len(PHASES))
        self.control_proof("after", self.started["control"]["MainPID"], self.before["control"]["MainPID"])
        from post_proof import collect
        proof = collect(self, self.log_base, self.started)
        self.receipt("post-start-proof.json", canonical(proof))
        self.gates(len(PHASES))

    def closeout(self):
        from closeout import install
        install(self)

    def complete(self):
        self.gates(len(PHASES))
        self.control_proof("after", self.started["control"]["MainPID"], self.before["control"]["MainPID"])
        exclusive(self.attempt / "COMPLETE.json", canonical(dict(application=APP, tree=TREE, completed_at_utc=self.now().isoformat(),
                                                               actions=PHASES, actual=self.fleet(),
                                                               flaggate_coverage=json.loads((self.attempt / "flaggate-coverage.json").read_bytes()),
                                                               control_display=self.latest_control,
                                                               next_session_proof="UNMEASURED")))
        from closeout import JOURNAL
        with JOURNAL.open("a") as file:
            file.write("\n" + self.now().isoformat() + " COMPLETE codex install1 application=" + APP
                       + " plan=" + self.release["plan_commit"] + " attempt=" + str(self.attempt)
                       + " receipt=" + str(self.attempt / "COMPLETE.json")
                       + " live-delivery/scanner/first-daily-rehearsal=UNMEASURED\n")
            file.flush()
            os.fsync(file.fileno())

    def abort(self, phase, completed):
        # Always preserve actual state; no start, rollback or automatic recovery in this path.
        try:
            actual = self.fleet()
        except Exception as exc:
            actual = {"unreadable": type(exc).__name__}
        raw = canonical(dict(verdict="STOP", phase=phase, completed=completed, actual=actual,
                             at_utc=self.now().isoformat(), recovery_authorized=False))
        exclusive(self.attempt / "STOP.json", raw)
        try:
            result = self.command([REPO / "ops/health/preopen_alert.sh", "ERROR", "Oct6 attended install STOP phase=" + phase,
                                   self.attempt / "STOP.json"], check=False, timeout=35)
            exclusive(self.attempt / "abort-delivery.json", canonical(dict(adapter_rc=result.returncode, confirmed=result.returncode == 0)))
        except Exception as exc:
            exclusive(self.attempt / "abort-delivery.json", canonical(dict(confirmed=False, error_type=type(exc).__name__)))


def main():
    need(os.geteuid() == 0 and len(sys.argv) == 2 and re.fullmatch(r"[0-9a-f]{64}", sys.argv[1]), "root + published release SHA256 required")
    need("TZ" not in os.environ, "native UTC systemd timestamps required; no TZ export")
    need(not any(key in os.environ for key in ("MAI_TAI_ENV_FILE", "POS_MAX_AGE_S")), "restart gate environment override forbidden")
    need(not any(key.startswith("MAI_TAI_") for key in os.environ), "inherited Settings override forbidden; use reviewed EnvironmentFile only")
    job = Path(__file__).resolve().parent
    release = verify(job, sys.argv[1], datetime.now(timezone.utc))
    with Path("/run/lock/project-mai-tai-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        attempt = job / "attempt-oct6-attended"
        attempt.mkdir(mode=0o700)
        def interrupted(signum, frame):
            raise Stop("attended runner interrupted by signal " + str(signum))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        Sequence(Real(job, release, attempt)).run()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Driver and HTTP exceptions can contain credentials; receipts retain raw safe commands.
        print("STOP attended error_type=" + type(exc).__name__ + " reason="
              + (str(exc) if isinstance(exc, Stop) else "protected unreadable error"), file=sys.stderr)
        raise SystemExit(1 if isinstance(exc, Stop) else 2)
