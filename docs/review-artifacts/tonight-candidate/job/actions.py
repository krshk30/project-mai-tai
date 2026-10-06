"""Reviewed deployment writes only; no trading, ledger repair or recovery."""
import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

APP = "7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf"
BOX = "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89"
FLAT_HASH = "658ca1c0ed85c18b9f898643dc84fc49ac4826ffa06f38c8a471f9e0f8565670"
REPO = Path("/home/trader/project-mai-tai")
ENV = Path("/etc/project-mai-tai/project-mai-tai.env")
JOURNAL = Path("/home/trader/fleet_health/deployments-20261005.md")
KEYS = ["MAI_TAI_STRATEGY_SCHWAB_1M_V2_" + name for name in (
    "PM_PRINT_ASK_CONFIRM_ENABLED", "PM_FLIP_WAIT_ENABLED",
    "PM_REST_REPRICE_ENABLED", "ATR_REPRICE_HANDOFF_ENABLED")]


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exclusive(path, value, mode=0o600):
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "w") as f:
        f.write(value)
        f.flush()
        os.fsync(f.fileno())


def journal(message):
    if not JOURNAL.is_file():
        raise RuntimeError("deployment journal missing; refusing implicit creation")
    with JOURNAL.open("a") as f:
        f.write("\n" + now() + " codex-2 " + message + "\n")
        f.flush()
        os.fsync(f.fileno())


def verify(job, release_sha):
    if digest(job / "release.json") != release_sha:
        raise RuntimeError("release hash drift")
    release = json.loads((job / "release.json").read_text())
    approval = json.loads((job / "approval.json").read_text())
    if (release["approved_sha"] != APP or release["box_sha"] != BOX
            or release["helper_sha256"] != FLAT_HASH
            or approval["reviewer"] != "claude-1" or approval["decision"] != "APPROVED"
            or approval["plan_commit"] != release["plan_commit"]
            or approval["approved_sha"] != APP
            or approval["release_sha256"] != release_sha
            or approval["date_et"] != "2026-10-05"
            or approval["scope"] != "v2-strategy-oms-0022-all-four-on-no-recovery"):
        raise RuntimeError("exact runner approval absent or mismatched")
    if datetime.now(ZoneInfo("America/New_York")).date().isoformat() != "2026-10-05":
        raise RuntimeError("GO expired")
    for name, expected in release["artifacts"].items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise RuntimeError("manifest path outside job")
        if digest(job / name) != expected:
            raise RuntimeError("artifact drift: " + name)
    if digest(job / "strict_flat_readonly.py") != FLAT_HASH:
        raise RuntimeError("allowance helper drift")
    print("APPROVAL PASS application=" + APP + " plan=" + release["plan_commit"])


def env_flags(attempt):
    if ENV.stat().st_uid != 0 or ENV.stat().st_mode & 0o777 != 0o600:
        raise RuntimeError("env ownership/mode drift")
    raw = ENV.read_bytes()
    text = raw.decode()
    lines = text.splitlines(keepends=True)
    indexes = {}
    for key in KEYS:
        matches = [i for i, line in enumerate(lines) if line.startswith(key + "=")]
        if len(matches) > 1:
            raise RuntimeError("duplicate env key: " + key)
        indexes[key] = matches
    handoff = indexes[KEYS[-1]]
    if len(handoff) != 1 or lines[handoff[0]].strip() != KEYS[-1] + "=true":
        raise RuntimeError("handoff must already be literally true; not an extra edit")
    backup = attempt / "project-mai-tai.env.before"
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    for key in KEYS[:-1]:
        if indexes[key]:
            i = indexes[key][0]
            print("ENV_CHANGE " + lines[i].strip() + " -> " + key + "=true")
            lines[i] = key + "=true\n"
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            print("ENV_CHANGE absent -> " + key + "=true")
            lines.append(key + "=true\n")
    changed = "".join(lines)
    for old, new in zip(text.splitlines(), changed.splitlines()[:len(text.splitlines())]):
        if old != new and not any(old.startswith(k + "=") for k in KEYS[:-1]):
            raise RuntimeError("unrelated env change")
    temp = ENV.with_name(ENV.name + ".oct5-all-on.tmp")
    exclusive(temp, changed)
    os.chown(temp, ENV.stat().st_uid, ENV.stat().st_gid)
    os.replace(temp, ENV)
    print("ENV_BACKUP " + str(backup) + " sha256=" + digest(backup))
    print("ENV_AFTER sha256=" + digest(ENV))
    journal("env three PM enables; handoff retained true; backup=" + str(backup)
            + " before=" + digest(backup) + " after=" + digest(ENV))


def migration(attempt):
    from dotenv import dotenv_values
    from sqlalchemy import create_engine, text
    from project_mai_tai.settings import Settings
    config = Settings(_env_file=ENV)
    engine = create_engine(config.database_url)
    with engine.connect() as c:
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        versions = c.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    engine.dispose()
    if versions != ["20260916_0021"]:
        raise RuntimeError("migration base not exactly 0021: " + repr(versions))
    manifest = json.loads((attempt.parent / "release.json").read_text())
    path = REPO / "sql/migrations/versions/20261005_0022_managed_entry_binding.py"
    if digest(path) != manifest["migration_sha256"]:
        raise RuntimeError("migration blob drift")
    environment = dict(os.environ)
    values = dotenv_values(ENV)
    environment.update({k: v for k, v in values.items() if v is not None})
    if environment.get("MAI_TAI_DATABASE_URL") != config.database_url:
        raise RuntimeError("migration DB binding differs from reviewed Settings")
    subprocess.run(["runuser", "-u", "trader", "--", str(REPO / ".venv/bin/alembic"),
                    "-c", str(REPO / "alembic.ini"), "upgrade", "20261005_0022"],
                   cwd=REPO, env=environment, check=True, timeout=120)
    journal("migration 20260916_0021 -> 20261005_0022; two nullable binding columns; no backfill")


def record(attempt):
    before = json.loads((attempt / "before-restart.json").read_text())
    payload = {"schema_version": 1, "snapshot_captured_at_utc": before["captured_at_utc"],
               "source_journal": str(JOURNAL), "service_actions": {
                   k: "restarted" if k in {"oms", "strategy", "schwab-1m-v2"}
                   else "deliberately_untouched" for k in before["services"]}}
    exclusive(attempt / "install-record.json", json.dumps(payload, indent=2) + "\n")
    print("INSTALL_RECORD " + str(attempt / "install-record.json"))


def source_proof(job):
    release = json.loads((job / "release.json").read_text())
    actual = subprocess.check_output(["git", "-C", str(REPO), "rev-parse", APP + "^{tree}"], text=True).strip()
    if actual != "ee6f058c248eeebf475fd392845eadfef7af59eb":
        raise RuntimeError("approved whole tree not pinned tree")
    for path, expected in release["application_blobs"].items():
        raw = subprocess.check_output(["git", "-C", str(REPO), "show", APP + ":" + path])
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError("approved blob hash drift: " + path)
    if digest(Path("/home/trader/preopen.sh")) != "2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3":
        raise RuntimeError("preopen baseline hash drift")
    print("SOURCE_BLOBS PASS exact tree and release application hashes")


def unchanged(before):
    import proof
    baseline = json.loads(before.read_text())
    for name, expected in baseline["services"].items():
        if proof.service(name) != expected:
            raise RuntimeError("pre-first-stop identity drift: " + name)
    print("ALL_IDENTITIES unchanged before first stop")


def catalogs(attempt):
    release = json.loads((attempt.parent / "release.json").read_text())
    destination = Path("/home/trader/restart_evidence")
    for name in ("expected_flags_check.py", "expected_flags.json", "expected_numeric.json"):
        source = REPO / "ops/health" / name
        target = destination / name
        expected = release["application_blobs"]["ops/health/" + name]
        if digest(source) != expected:
            raise RuntimeError("catalog/checker source drift: " + name)
        backup = attempt / (name + ".before")
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
            f.write(target.read_bytes())
            f.flush()
            os.fsync(f.fileno())
        temp = target.with_name(target.name + ".oct5-all-on.tmp")
        exclusive(temp, source.read_text(), 0o644)
        os.chown(temp, target.stat().st_uid, target.stat().st_gid)
        os.replace(temp, target)
        if digest(target) != expected:
            raise RuntimeError("installed checker/catalog hash drift")
        print("ISOLATED_GATE " + str(target) + " sha256=" + expected + " backup=" + str(backup))


def repin(attempt):
    result = json.loads((attempt / "preopen-candidate.json").read_text())
    expected_question = ["inactive-paper check_identity currently demands active/running; candidate PID0 stays REAL FAILURE, no routing bypass"]
    if result["review_required"] != expected_question:
        raise RuntimeError("unresolved preopen routing disposition; no script write")
    path = Path("/home/trader/preopen.sh")
    original = path.read_bytes()
    info = path.stat()
    if digest(path) != result["old_sha256"] or info.st_mode & 0o777 != 0o700:
        raise RuntimeError("preopen identity/hash/mode drift")
    backup = attempt / "preopen.sh.before"
    with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700), "wb") as f:
        f.write(original)
        f.flush()
        os.fsync(f.fileno())
    os.chown(backup, info.st_uid, info.st_gid)
    candidate = attempt / "preopen.sh.candidate"
    exclusive(candidate, result["candidate"], 0o700)
    if digest(candidate) != result["candidate_sha256"]:
        raise RuntimeError("candidate bytes/hash disagree")
    subprocess.run(["bash", "-n", str(candidate)], check=True)
    exclusive(attempt / "preopen.diff", result["diff"])
    temp = path.with_name(path.name + ".oct5-all-on.tmp")
    exclusive(temp, result["candidate"], 0o700)
    os.chown(temp, info.st_uid, info.st_gid)
    if digest(path) != result["old_sha256"]:
        raise RuntimeError("preopen changed during repin")
    os.replace(temp, path)
    subprocess.run(["bash", "-n", str(path)], check=True)
    if digest(path) != result["candidate_sha256"] or path.stat().st_mode & 0o777 != 0o700:
        raise RuntimeError("installed preopen bytes/mode drift")
    message = ("PREOPEN_REPIN date=2026-10-06 sha=" + APP + " sha256=" + digest(path)
               + " backup=" + str(backup) + "; paper PID0 is real, existing active check retained; "
               "expected identity FAILURE distinct from FLAGGATE paper UNKNOWN2, explicitly reviewed")
    print(message)
    journal(message)


def page(message):
    request = Request("https://ntfy.sh/mai-tai-routine-112964cc8f26787132a29538",
                      data=message.encode(), headers={"Title": "Oct5 install STOP", "Priority": "high"},
                      method="POST")
    with urlopen(request, timeout=15) as response:
        print("PAGE_HTTP " + str(response.status))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("verify", "env", "migration", "record", "journal", "page",
                                       "source", "unchanged", "catalogs", "repin"))
    p.add_argument("path")
    p.add_argument("value", nargs="?")
    a = p.parse_args()
    if a.command == "verify":
        verify(Path(a.path), a.value)
    elif a.command == "env":
        env_flags(Path(a.path))
    elif a.command == "migration":
        migration(Path(a.path))
    elif a.command == "record":
        record(Path(a.path))
    elif a.command == "source":
        source_proof(Path(a.path))
    elif a.command == "unchanged":
        unchanged(Path(a.path))
    elif a.command == "catalogs":
        catalogs(Path(a.path))
    elif a.command == "repin":
        repin(Path(a.path))
    elif a.command == "journal":
        journal(a.path)
    else:
        page(a.path)


if __name__ == "__main__":
    main()
