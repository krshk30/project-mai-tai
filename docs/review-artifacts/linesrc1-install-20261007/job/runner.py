"""[codex] Literal Oct7 install: only v2 stop/start; no recovery on any abort."""
from datetime import datetime, timezone
from difflib import unified_diff
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time

import release_policy as p
from ticket_inventory import stable_bindings

REPO = Path("/home/trader/project-mai-tai")
PY = REPO / ".venv/bin/python"
ENV = Path("/etc/project-mai-tai/project-mai-tai.env")
GATE = Path("/home/trader/preopen.sh")
DAILY = Path("/home/trader/preopen-daily")
HELPERS = Path("/home/trader/restart_evidence")
LOG = Path("/var/log/project-mai-tai/schwab-1m-v2.log")
FIELDS = ("MainPID", "NRestarts", "ActiveState", "SubState", "Result", "ExecMainCode",
          "ExecMainStatus", "ExecMainStartTimestamp", "ExecMainStartTimestampMonotonic",
          "FragmentPath", "DropInPaths", "EnvironmentFiles", "InactiveEnterTimestamp", "InvocationID")


def exclusive(path, raw, mode=0o600):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode), "wb") as file:
        file.write(raw)
        file.flush()
        os.fsync(file.fileno())


class Sequence:
    def __init__(self, effects):
        self.fx, self.phase, self.completed = effects, "initial-readonly", 0

    def run(self):
        try:
            self.fx.initial()
            self.phase = "claim-backup-source-env"
            p.first_stop_window(self.fx.now())
            self.fx.claim()
            self.fx.prepare()
            self.phase = "stop-v2"
            self.fx.gates(0)
            self.fx.v2_gate()
            p.first_stop_window(self.fx.now())
            self.fx.action("stop")
            self.completed = 1
            self.fx.checkpoint(1)
            self.phase = "start-v2"
            self.fx.gates(1)
            # No wall-clock abort after a clean stop; restoring this one owner
            # is the literal approved success path, not an abort recovery action.
            self.fx.action("start")
            self.completed = 2
            self.fx.checkpoint(2)
            self.phase = "new-process-proof"
            self.fx.prove()
            self.phase = "repin-existing-morning"
            self.fx.closeout()
            self.phase = "complete"
            self.fx.complete()
        except p.Pending:
            if self.fx.claimed:
                self.fx.abort(self.phase, self.completed)
            raise
        except BaseException:
            self.fx.abort(self.phase, self.completed)
            raise


def verify(job, expected):
    raw = (job / "release.json").read_bytes()
    p.need(p.digest(raw) == expected, "published release bytes differ")
    value = json.loads(raw)
    p.need((value["application"], value["tree"], value["box"], value["date_et"], value["scope"])
           == (p.APP, p.TREE, p.BOX, p.DAY, p.SCOPE), "release scope differs")
    from make_release import ARTIFACTS
    p.need(set(value["artifacts"]) == set(ARTIFACTS), "artifact inventory differs")
    for name, sha in value["artifacts"].items():
        file = job / name
        p.need(Path(name).name == name and not file.is_symlink()
               and file.stat().st_uid == 0 and file.stat().st_mode & 0o022 == 0
               and p.digest(file.read_bytes()) == sha, "package bytes/owner/mode differ: " + name)
    for file in (job, job / "release.json", job / "approval.json"):
        p.need(not file.is_symlink() and file.stat().st_uid == 0 and file.stat().st_mode & 0o022 == 0,
               "package/approval not root immutable")
    decision = dict(authority="operator-standing-mechanics-authority", decision="APPROVED",
                    application=p.APP, plan_commit=value["plan_commit"], date_et=p.DAY,
                    scope=p.SCOPE, release_sha256=expected)
    p.need(json.loads((job / "approval.json").read_bytes()) == decision, "exact same-user GO missing")
    p.need(re.fullmatch(r"[0-9a-f]{40}", value["plan_commit"]), "plan commit unbound")
    return value


class Real:
    def __init__(self, job, release, attempt):
        self.job, self.release, self.attempt = job, release, attempt
        self.claimed, self.counter, self.started = False, 0, None
        self.since = None

    def now(self):
        return datetime.now(timezone.utc)

    def receipt(self, label, raw):
        self.counter += 1
        path = self.attempt / f"{self.counter:03d}-{label}"
        exclusive(path, raw)
        fd = os.open(self.attempt / "runner-journal.jsonl", os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "ab") as file:
            metadata = os.fstat(file.fileno())
            p.need(stat.S_ISREG(metadata.st_mode) and metadata.st_uid == os.geteuid()
                   and metadata.st_mode & 0o022 == 0, "unsafe receipt journal")
            file.write(p.canonical(dict(receipt=path.name, sha256=p.digest(raw), bytes=len(raw),
                                        at_utc=self.now().isoformat())).replace(b"\n", b" ") + b"\n")
            file.flush()
            os.fsync(file.fileno())
        return path

    def command(self, args, check=True, timeout=30, limit=8_000_000):
        args = list(map(str, args))
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(args, 124, exc.stdout or b"", exc.stderr or b"")
        self.receipt("command.json", p.canonical(dict(argv=args, rc=result.returncode, at_utc=self.now().isoformat())))
        self.receipt("stdout.txt", result.stdout)
        self.receipt("stderr.txt", result.stderr)
        p.need(len(result.stdout) + len(result.stderr) <= limit, "command reply bound exceeded")
        if check:
            p.need(result.returncode == 0 and not result.stderr.strip(), "command failed/unreadable: " + args[0])
        return result

    def reader(self, name, *args):
        for count in range(3):
            result = self.command(["nice", "-n", "19", PY, "-B", self.job / name, *args], check=False, timeout=210)
            if result.returncode == 0:
                p.need(not result.stderr.strip(), "read-only success stderr unreadable")
                return result.stdout
            p.need(result.returncode == 2 and count < 2, "read-only blocker/exhausted UNKNOWN: " + name)
            time.sleep(60)
        raise p.Stop("unreachable read retry")

    def fleet(self):
        rows = {}
        for name in p.SERVICES:
            unit = name + ".service" if name in {"redis", "postgresql"} else "project-mai-tai-" + name + ".service"
            raw = self.command(["systemctl", "show", "--all", unit, *["--property=" + key for key in FIELDS]]).stdout
            pairs = [line.split("=", 1) for line in raw.decode().splitlines()]
            p.need(all(len(pair) == 2 for pair in pairs) and len(pairs) == len(dict(pairs)), "fleet state malformed")
            state = dict(pairs)
            # These system units can omit the empty EnvironmentFiles array.
            if name in {"redis", "postgresql"} and "EnvironmentFiles" not in state:
                reply = self.command(["busctl", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager", "GetUnit", "s", unit]).stdout.decode().strip()
                p.need(reply.startswith("o "), "D-Bus object missing")
                obj = json.loads(reply[2:])
                p.need(re.fullmatch(r"/org/freedesktop/systemd1/unit/[A-Za-z0-9_]+", obj), "D-Bus path unreadable")
                value = self.command(["busctl", "get-property", "org.freedesktop.systemd1", obj,
                                      "org.freedesktop.systemd1.Service", "EnvironmentFiles"]).stdout.decode().strip()
                p.need(value == "a(sb) 0", "omitted system EnvironmentFiles not empty")
                state["EnvironmentFiles"] = ""
            p.need(set(state) == set(FIELDS), "fleet fields incomplete: " + name)
            for key in ("MainPID", "NRestarts", "ExecMainCode", "ExecMainStatus", "ExecMainStartTimestampMonotonic"):
                state[key] = int(state[key])
            rows[name] = state
        return rows

    def source(self, sha):
        head = self.command(["runuser", "-u", "trader", "--", "git", "-C", REPO, "rev-parse", "HEAD"]).stdout.decode().strip()
        dirty = self.command(["runuser", "-u", "trader", "--", "git", "-C", REPO, "status", "--porcelain"]).stdout
        p.need(head == sha and not dirty, "checkout SHA/cleanliness differs")
        tree = self.command(["git", "-C", REPO, "rev-parse", p.APP + "^{tree}"]).stdout.decode().strip()
        p.need(tree == p.TREE, "application tree differs")
        for path, expected in self.release["application_blobs"].items():
            actual = self.command(["git", "-C", REPO, "show", p.APP + ":" + path]).stdout
            p.need(p.digest(actual) == expected, "application blob differs: " + path)
            if sha == p.APP:
                p.need(p.digest((REPO / path).read_bytes()) == expected, "working application blob differs")

    def baseline(self):
        for path, expected in self.release["baseline_hashes"].items():
            file = Path(path)
            p.need(not file.is_symlink() and file.stat().st_mode & 0o022 == 0
                   and p.digest(file.read_bytes()) == expected, "installed baseline drift: " + path)

    def flat(self, service=None):
        args = ["--service", service] if service else []
        raw = self.reader("strict_flat_readonly.py", *args)
        start = 0 if raw.startswith(b"{") else raw.find(b"\n{") + 1
        result = json.loads(raw[start:])
        p.need(result["rc"] == 0 and not result["blockers"] and not any(result[key] for key in
               ("broker_holdings", "working_orders", "managed_rows", "virtual_rows", "inflight_intents", "account_rows")),
               "requires fresh actual flat/zero orders/open rows/inflight; no holding waiver")
        return result

    def census(self):
        args = [] if self.since is None else [self.since.isoformat()]
        value = json.loads(self.reader("census_readonly.py", *args))
        if hasattr(self, "tickets"):
            stable_bindings(self.tickets, value["rows"])
        else:
            self.tickets = value["rows"]
        return value

    def redis(self):
        args = [] if not hasattr(self, "redis_before") else ["--baseline", self.redis_before]
        raw = self.reader("redis_checkpoint.py", *args)
        if not hasattr(self, "redis_before"):
            self.redis_before = self.attempt / "redis-before.json"
            exclusive(self.redis_before, raw)

    def proc(self, state, value):
        with Path(f"/proc/{state['MainPID']}/environ").open("rb") as stream:
            raw = stream.read(262145)
        found = p.process_values(raw, value)
        self.receipt("process-flags.json", p.canonical(dict(pid=state["MainPID"], sha256=p.digest(raw), values=found)))

    def v2_gate(self):
        self.flat("oms")
        self.flat("strategy")
        self.flat()
        self.reader("armed_readonly.py")
        result = self.command([REPO / "ops/preflight/preflight_v2_restart.sh"], check=False, timeout=60)
        if not self.claimed and result.returncode == 1 and not result.stderr.strip():
            # A literal clock-only refusal is pending, never an implicit override.
            blocks = [line for line in result.stdout.decode().splitlines() if "[BLOCK]" in line]
            if len(blocks) == 1 and "before 18:00 ET" in blocks[0]:
                raise p.Pending("unmodified native clock gate closed; no writes")
        p.need(result.returncode == 0 and not result.stderr.strip()
               and b"===> GO. Zero armed segments AND flat. Safe to restart v2." in result.stdout
               and b"[OVERRIDE]" not in result.stdout and b"[BLOCK]" not in result.stdout,
               "unmodified v2 restart gate not green")
        self.reader("armed_readonly.py")
        self.flat()
        self.reader("armed_readonly.py")

    def initial(self):
        p.first_stop_window(self.now())
        self.baseline()
        self.source(p.BOX)
        self.command(["git", "-C", REPO, "merge-base", "--is-ancestor", p.BOX, p.APP])
        self.command(["git", "-C", REPO, "merge-base", "--is-ancestor", p.APP, "origin/main"])
        changed = self.command(["git", "-C", REPO, "diff", "--name-only", p.BOX, p.APP, "--", "src", "ops"]).stdout.decode().splitlines()
        p.need(set(changed) == set(p.SOURCES), "application scope not only three v2 source files")
        info = ENV.stat()
        p.need(info.st_uid == 0 and info.st_mode & 0o777 == 0o600 and not ENV.is_symlink(), "env owner/mode")
        self.env_before = ENV.read_bytes()
        p.need(p.digest(self.env_before) == self.release["environment_sha256"], "approved env baseline differs")
        self.env_after = p.env_candidate(self.env_before)
        self.before = self.fleet()
        p.need(self.before == self.release["fleet_before"], "reviewed baseline identities drift")
        old = self.before[p.V2]
        p.need(old["MainPID"] == 917354 and old["ExecMainStartTimestamp"] == "Wed 2026-10-07 11:14:07 UTC"
               and old["NRestarts"] == 0 and old["ActiveState"] == "active" and old["SubState"] == "running"
               and old["Result"] == "success", "Oct7 OFF v2 identity differs")
        for key, value in p.ACK_STATE.items():
            p.need(str(self.before["orb-schwab"][key]) == value, "acknowledged ORB identity differs")
        self.proc(old, "false")
        self.catalog()
        self.flat()
        self.census()
        self.redis()
        self.v2_gate()

    def claim(self):
        exclusive(self.job / "write-started.json", p.canonical(dict(attempt=str(self.attempt), at_utc=self.now().isoformat())))
        self.claimed = True

    def prepare(self):
        self.gates(0)
        self.baseline()
        self.since = self.now()
        for label, file in [("env", ENV), ("preopen", GATE), *[("daily-" + name, DAILY / name)
                for name in ("runtime.json", "binding.json", "upgrade_ack.py", "upgrade-ack.json")]]:
            exclusive(self.attempt / (label + ".before"), file.read_bytes())
        archive = self.command(["git", "-C", REPO, "archive", p.BOX], limit=64_000_000).stdout
        exclusive(self.attempt / "source-before.tar", archive)
        self.receipt("backup-hashes.json", p.canonical({file.name: p.digest(file.read_bytes())
                         for file in self.attempt.glob("*.before")} | {"source-before.tar": p.digest(archive)}))
        self.command([PY, "-B", REPO / "ops/health/v2_restart_evidence.py", "snapshot",
                      "--output", self.attempt / "before-restart.json"], timeout=120)
        result = self.command(["runuser", "-u", "trader", "--", "git", "-C", REPO,
                               "switch", "--detach", p.APP], check=False)
        # Git's successful detached-checkout notice is stderr, not a read failure.
        # Exact clean HEAD/tree/working bytes below remain mandatory.
        p.need(result.returncode == 0, "clean application advance failed")
        self.advanced = True
        self.source(p.APP)
        self.flat()
        p.need(ENV.read_bytes() == self.env_before, "env changed during source advance")
        diff = "".join(unified_diff(self.env_before.decode().splitlines(True), self.env_after.decode().splitlines(True),
                                    fromfile="env.before", tofile="env.after", n=0))
        self.receipt("env.diff", diff.encode())
        self.replace(ENV, self.env_after)
        info = LOG.stat()
        self.log_base = dict(path=str(LOG), inode=info.st_ino, device=info.st_dev, offset=info.st_size)
        exclusive(self.attempt / "logs-before-stop.json", p.canonical(self.log_base))

    def replace(self, path, raw):
        info = path.stat()
        temporary = path.with_name(path.name + ".linesrc1-oct7-exclusive.tmp")
        exclusive(temporary, raw, info.st_mode & 0o777)
        os.chown(temporary, info.st_uid, info.st_gid)
        os.replace(temporary, path)
        p.need(p.digest(path.read_bytes()) == p.digest(raw), "installed bytes differ")

    def gates(self, phase):
        current = self.fleet()
        p.states(self.before, current, phase)
        if self.started is not None:
            p.need(current[p.V2] == self.started, "new v2 process moved")
        self.source(p.APP if getattr(self, "advanced", False) else p.BOX)
        self.census()
        self.redis()
        self.flat()

    def action(self, action):
        p.need(action in {"stop", "start"}, "foreign service action")
        if action == "stop":
            self.stop_started = self.now()
        result = self.command(["systemctl", action, "project-mai-tai-schwab-1m-v2.service"], check=False, timeout=120)
        if action == "start":
            self.start_returned = self.now()
            exclusive(self.attempt / "restart-interval.json", p.canonical(dict(
                stop_started_utc=self.stop_started.isoformat(), start_returned_utc=self.start_returned.isoformat())))
        p.need(result.returncode == 0 and not result.stderr.strip(), "v2 systemctl failed/unreadable")
        p.states(self.before, self.fleet(), 1 if action == "stop" else 2)

    def checkpoint(self, phase):
        current = self.fleet()
        p.states(self.before, current, phase)
        if phase == 2:
            self.started = current[p.V2]
        exclusive(self.attempt / f"phase-{phase}.json", p.canonical(current))

    def catalog(self):
        flags, numeric = (HELPERS / "expected_flags.json").read_bytes(), (HELPERS / "expected_numeric.json").read_bytes()
        p.need(p.digest(flags) == self.release["catalog_hashes"]["flags"]
               and p.digest(numeric) == self.release["catalog_hashes"]["numeric"], "installed catalog changed")
        p.need(p.catalog_counts(flags, numeric) == self.release["catalog_counts"], "catalog denominator differs")

    def flaggate(self):
        self.catalog()
        before = self.fleet()["momentum-paper"]
        result = self.command([PY, "-B", HELPERS / "expected_flags_check.py", "--catalog", HELPERS / "expected_flags.json",
                               "--numeric-catalog", HELPERS / "expected_numeric.json"], check=False)
        from flag_admission import flag_result
        rows = json.loads((HELPERS / "expected_flags.json").read_bytes())["flags"]
        rows += json.loads((HELPERS / "expected_numeric.json").read_bytes())["settings"]
        p.need(not result.stderr.strip(), "flaggate unreadable stderr")
        proof = flag_result(result.returncode, result.stdout.decode(), rows, paper_before=before,
                            paper_after=self.fleet()["momentum-paper"], now=self.now())
        self.receipt("flaggate.json", p.canonical(proof))
        return proof

    def prove(self):
        self.gates(2)
        self.proc(self.started, "true")
        # Imported byte-identical bounded log reader, not the old multi-owner collector.
        from log_ranges import logs
        deadline = time.monotonic() + 180
        while True:
            try:
                found = logs({p.V2: self.log_base})[p.V2]
                self.hold = p.held_logs(found["text"], self.started, self.now(), self.stop_started)
                break
            except p.Stop:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(5)
        self.receipt("new-v2-log-ranges.json", p.canonical(found))
        self.receipt("literal-held.json", p.canonical(self.hold))
        snapshot = json.loads((self.attempt / "before-restart.json").read_bytes())
        self.receipt("watched-population.json", p.canonical(snapshot["v2_watchlist"]))
        self.continuity = self.measure_continuity()
        self.record = self.attempt / "v2-only-install-record.json"
        journal = self.attempt / "sealed-actions.json"
        exclusive(journal, p.canonical(dict(actions=[["stop", p.V2], ["start", p.V2]],
                                            source_journal=str(self.attempt / "runner-journal.jsonl"),
                                            checkpoint_sha256=p.digest((self.attempt / "runner-journal.jsonl").read_bytes()))))
        exclusive(self.record, p.canonical(dict(schema_version=1, snapshot_captured_at_utc=snapshot["captured_at_utc"],
            source_journal=str(journal), classification="OCT7_V2_ONLY_NOT_OLD_GROUP_RESTART",
            service_actions={name: "restarted" if name == p.V2 else "deliberately_untouched"
                             for name in snapshot["services"]})))
        output = self.attempt / "official-restart-evidence.md"
        # The unchanged morning wrapper still has BOX-bound acknowledgement until
        # closeout. Tonight use the exact official collector with no error filter;
        # the morning wrapper is preserved and validated after its narrow repin.
        args = [PY, "-B", REPO / "ops/health/v2_restart_evidence.py", "report", "--snapshot", self.attempt / "before-restart.json",
                "--install-record", self.record, "--restarted", p.V2, "--no-schema-change",
                "--expected-alembic-head", "20261005_0022", "--output", output]
        for key, value in {**{key: "true" for key in p.LIVE_KEYS}, p.FLAG: "true"}.items():
            args += ["--expect-flag", p.V2 + ":" + key + "=" + value]
        result = self.command(args, check=False, timeout=240)
        raw = output.read_text() if output.exists() else result.stdout.decode()
        self.receipt("official-raw-report.md", raw.encode())
        self.receipt("install-disposition.md", p.report_disposition(result.returncode, raw, self.hold, self.continuity).encode())
        self.flaggate()
        self.gates(2)

    def measure_continuity(self):
        if not 4 <= self.start_returned.astimezone(p.ET).hour < 20:
            return dict(verdict="N/A_OFF_SESSION", pending_symbols=[], per_stock=[],
                        reason="actual restart outside04..20, not an inferred quiet tape")
        for count in range(3):
            result = self.command(["nice", "-n", "19", PY, "-B", self.job / "continuity_readonly.py",
                "--snapshot", self.attempt / "before-restart.json", "--interval", self.attempt / "restart-interval.json"],
                check=False, timeout=30)
            p.need(not result.stderr.strip() and result.returncode in {0, 2}, "continuity blocker/unreadable")
            value = json.loads(result.stdout)
            p.need((result.returncode == 0 and value["verdict"] == "MEASURED_RESTART_WINDOW")
                   or (result.returncode == 2 and value["verdict"] == "PENDING_FIRST_CLOSED_BAR"
                       and value["pending_symbols"]), "continuity UNKNOWN is not a pending future callback")
            if result.returncode == 0 or count == 2:
                self.receipt("continuity-mechanics.json", p.canonical(value))
                return value
            time.sleep(60)
        raise p.Stop("unreachable continuity")

    def closeout(self):
        self.baseline()
        state = self.fleet()[p.V2]
        p.need(state == self.started, "v2 pin changed before closeout")
        before = GATE.read_bytes()
        after = p.gate_candidate(before, state, self.attempt / "before-restart.json", self.record)
        candidate = self.attempt / "preopen.candidate.sh"
        exclusive(candidate, after, 0o700)
        self.command(["bash", "-n", candidate])
        helper, ack = p.repin_ack((DAILY / "upgrade_ack.py").read_bytes(), (DAILY / "upgrade-ack.json").read_bytes())
        binding = json.loads((DAILY / "binding.json").read_bytes())
        p.need(binding["approved_sha"] == p.BOX, "daily imported APP binding differs")
        binding.update(approved_sha=p.APP, tree=p.TREE)
        binding["linesrc1_oct7"] = dict(application=p.APP, tree=p.TREE, previous_application=p.BOX,
                                      merge_pr=1107, proof_scope="only-v2; historical Oct6 receipts retained")
        replacements = {"upgrade_ack.py": helper, "upgrade-ack.json": ack, "binding.json": p.canonical(binding)}
        runtime = json.loads((DAILY / "runtime.json").read_bytes())
        p.need(runtime["approved_sha"] == p.BOX, "runtime baseline application differs")
        runtime["approved_sha"] = p.APP
        runtime["gate_sha256"] = p.digest(after)
        runtime["artifacts"].update({name: p.digest(raw) for name, raw in replacements.items()})
        runtime["evidence_inputs"].update({str(file): p.digest(file.read_bytes()) for file in
            (self.record, self.attempt / "before-restart.json", self.attempt / "sealed-actions.json")})
        runtime["linesrc1_oct7"] = dict(release_sha256=p.digest((self.job / "release.json").read_bytes()),
            attempt=str(self.attempt), restoration="HELD/next07:00 UNMEASURED", v2=state)
        # Existing daily run lock excludes the checker during the multi-file repin.
        # A crash still leaves a hash mismatch fail-closed, never an auto-repair.
        for name, raw in replacements.items():
            self.replace(DAILY / name, raw)
        self.replace(GATE, after)
        self.replace(DAILY / "runtime.json", p.canonical(runtime))
        self.receipt("preopen.diff", "".join(unified_diff(before.decode().splitlines(True), after.decode().splitlines(True))).encode())
        self.command([PY, "-B", "-c", "import sys; sys.path.insert(0,'/home/trader/preopen-daily'); "
                      "from daily import verify_runtime; verify_runtime(); from upgrade_ack import current; current()"])
        self.receipt("morning-repin-hashes.json", p.canonical({str(path): p.digest(path.read_bytes()) for path in
            (GATE, DAILY / "runtime.json", *[DAILY / name for name in replacements])}))

    def complete(self):
        self.gates(2)
        self.proc(self.started, "true")
        self.flaggate()
        from log_ranges import logs
        found = logs({p.V2: self.log_base})[p.V2]
        self.hold = p.held_logs(found["text"], self.started, self.now(), self.stop_started)
        self.receipt("complete-new-v2-log-ranges.json", p.canonical(found))
        self.receipt("complete-literal-held.json", p.canonical(self.hold))
        raw = p.canonical(dict(verdict="COMPLETE_HELD_AFTER16", application=p.APP, tree=p.TREE,
            actual=self.fleet(), actions=[["stop", p.V2], ["start", p.V2]], next07="UNMEASURED",
            held=self.hold, continuity=self.continuity,
            raw_journal_sha256=p.digest((self.attempt / "runner-journal.jsonl").read_bytes())))
        exclusive(self.attempt / "COMPLETE.json", raw)
        self.deployment_note("COMPLETE_HELD_AFTER16", self.attempt / "COMPLETE.json")

    def deployment_note(self, verdict, receipt):
        path = Path("/home/trader/fleet_health/deployments-20261007.md")
        fd = os.open(path, os.O_APPEND | os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write("\n" + self.now().isoformat() + " codex LINESRC1 " + verdict + " APP=" + p.APP
                         + " receipt=" + str(receipt) + " sha256=" + p.digest(receipt.read_bytes())
                         + " next07/restoration=UNMEASURED; no recovery authorized\n")
            stream.flush()
            os.fsync(stream.fileno())

    def abort(self, phase, completed):
        if not (self.job / "write-started.json").exists():
            exclusive(self.job / "write-started.json", p.canonical(dict(verdict="ABORTED_BEFORE_APP_WRITE",
                       attempt=str(self.attempt), app_writes=False)))
        try:
            actual = self.fleet()
        except Exception as exc:
            actual = dict(unreadable=type(exc).__name__)
        raw = p.canonical(dict(verdict="STOP", phase=phase, completed=completed, actual=actual,
                               claimed=self.claimed, recovery_authorized=False, at_utc=self.now().isoformat()))
        target = self.attempt / "STOP.json"
        exclusive(target, raw)
        for name, notify in (("deployment-note", lambda: self.deployment_note("STOP", target)),
            ("alert", lambda: self.command([REPO / "ops/health/preopen_alert.sh", "ERROR",
                 "Oct7 LINESRC1 STOP " + raw.decode(), target], check=False, timeout=35))):
            try:
                result = notify()
                if result is not None:
                    self.receipt(name + "-delivery.json", p.canonical(dict(rc=result.returncode,
                                 delivered=result.returncode == 0, future_delivery="UNMEASURED")))
            except Exception as exc:
                self.receipt(name + "-unreadable.json", p.canonical(dict(error_type=type(exc).__name__,
                             delivered=False, recovery_authorized=False)))


def main():
    p.need(os.geteuid() == 0 and len(sys.argv) == 2 and re.fullmatch(r"[0-9a-f]{64}", sys.argv[1]),
           "root and exact published release SHA256 required")
    p.need("TZ" not in os.environ and not any(key.startswith("MAI_TAI_") or key == "POS_MAX_AGE_S" for key in os.environ),
           "native UTC/no inherited settings or gate overrides required")
    job = Path(__file__).resolve().parent
    release = verify(job, sys.argv[1])
    with Path("/run/lock/project-mai-tai-deploy.lock").open("a") as lock, (DAILY / "run.lock").open("a") as daily_lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(daily_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (job / "write-started.json").exists():
            print("STOP already claimed; no duplicate deployment/recovery")
            return 1
        attempt = job / ("attempt-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
        attempt.mkdir(mode=0o700)
        fx = Real(job, release, attempt)
        def interrupted(number, frame):
            raise p.Stop("signal " + str(number))
        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            Sequence(fx).run()
        except p.Pending as exc:
            if fx.claimed:
                return 1
            exclusive(attempt / "PENDING.json", p.canonical(dict(reason=str(exc), app_writes=False)))
            return 75
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("STOP LINESRC1 error_type=" + type(exc).__name__ + " reason="
              + (str(exc) if isinstance(exc, p.Stop) else "protected unreadable error"), file=sys.stderr)
        raise SystemExit(1)
