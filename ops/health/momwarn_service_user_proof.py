"""STAGED ONLY: main runs after F2 permissions, before the after-close gateway restart.

No daemon/provider imports, mutations or external connections. The only connection
is a bounded read of the configured loopback Redis strategy-state stream.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit

UNIT = "project-mai-tai-market-data.service"
MAX_ENV_BYTES = 262144


def process_identity(pid: int) -> str:
    with Path(f"/proc/{pid}/stat").open("rb") as source:
        raw = source.read(8193)
    if len(raw) > 8192:
        raise ValueError("process identity bound")
    fields = raw.decode().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        raise ValueError("gateway process dead")
    return f"{pid}:{int(fields[19])}"


def adopt_service_environment(repository: Path, expected_head: str) -> dict:
    if os.geteuid() == 0 or pwd.getpwuid(os.geteuid()).pw_name != "trader":
        raise ValueError("actual trader service user required; root proof forbidden")
    output = subprocess.check_output(
        ["systemctl", "show", UNIT, "-p", "MainPID", "-p", "User", "-p", "Group",
         "-p", "ActiveState", "-p", "WorkingDirectory", "-p", "ExecMainStartTimestamp"],
        text=True, timeout=5,
    )
    state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if state.get("User") != "trader" or state.get("ActiveState") != "active":
        raise ValueError("actual active trader gateway required")
    if state.get("WorkingDirectory") != str(repository):
        raise ValueError("gateway working directory mismatch")
    if state.get("Group") != grp.getgrgid(os.getegid()).gr_name:
        raise ValueError("gateway effective group mismatch")
    head = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True, timeout=5,
    ).strip()
    if len(expected_head) != 40 or head != expected_head:
        raise ValueError("reviewed source head mismatch")
    pid = int(state["MainPID"])
    if pid <= 0:
        raise ValueError("gateway PID missing")
    instance = process_identity(pid)
    with Path(f"/proc/{pid}/status").open("rb") as source:
        raw_status = source.read(16385)
    if len(raw_status) > 16384:
        raise ValueError("service identity bound")
    status = dict(line.split(":", 1) for line in raw_status.decode().splitlines() if ":" in line)
    if int(status["Uid"].split()[1]) != os.geteuid() or int(status["Gid"].split()[1]) != os.getegid():
        raise ValueError("actual service UID/GID mismatch")
    if sorted(map(int, status["Groups"].split())) != sorted(os.getgroups()):
        raise ValueError("actual service supplementary groups mismatch")
    with Path(f"/proc/{pid}/environ").open("rb") as source:
        raw = source.read(MAX_ENV_BYTES + 1)
    if len(raw) > MAX_ENV_BYTES:
        raise ValueError("gateway environment bound")
    environment = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        key, separator, value = item.partition(b"=")
        if not separator:
            raise ValueError("gateway environment malformed")
        environment[os.fsdecode(key)] = os.fsdecode(value)
    if process_identity(pid) != instance:
        raise ValueError("gateway changed during environment capture")
    # Values stay in memory, never in the receipt; no env-file fallback/restamping.
    os.environ.clear()
    os.environ.update(environment)
    os.chdir(repository)
    sys.path.insert(0, str(repository / "src"))
    return {"unit": UNIT, "gateway_pid": pid, "gateway_instance": instance,
            "exec_start": state["ExecMainStartTimestamp"], "source_head": head,
            "uid": os.geteuid(), "gid": os.getegid(), "groups": os.getgroups(),
            "environment_source": f"/proc/{pid}/environ (values not exported)"}


def path_receipt(path: Path) -> dict:
    result = {"path": str(path), "readable": False}
    try:
        info = path.stat()
        result.update(owner=pwd.getpwuid(info.st_uid).pw_name,
                      group=grp.getgrgid(info.st_gid).gr_name,
                      mode=format(stat.S_IMODE(info.st_mode), "04o"),
                      device=info.st_dev, inode=info.st_ino)
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("source is not regular")
            result["readable"] = True
        finally:
            os.close(fd)
    except (OSError, ValueError, KeyError) as exc:
        result["exception_class"] = type(exc).__name__
    return result


async def prove(reader, client, prefix: str, config: dict, timeout: float) -> dict:
    if any(type(config.get(key)) is not int or config[key] <= 0 for key in ("oms_pid", "v2_pid")):
        raise ValueError("positive source PIDs required before path inventory")
    paths = [reader.health_path, *(reader.log_directory / name for name in reader.LOG_NAMES),
             Path(f"/proc/{config['oms_pid']}/stat"),
             Path(f"/proc/{config['v2_pid']}/stat"), Path("/proc/meminfo")]
    permissions = [path_receipt(path) for path in dict.fromkeys(paths)]
    attempts = []
    for number in (1, 2):
        try:
            health = await asyncio.wait_for(reader.read_live(client, prefix), timeout=timeout)
            attempts.append({"attempt": number, "read_status": "OK", "health": asdict(health)})
        except Exception as exc:
            attempts.append({"attempt": number, "read_status": "UNKNOWN",
                             "exception_class": type(exc).__name__})
        if number == 1:
            await asyncio.sleep(1)
    passed = all(row["read_status"] == "OK" for row in attempts) and all(
        row["readable"] for row in permissions)
    return {"status": "READ_LIVE_2_OK" if passed else "UNKNOWN",
            "permissions": permissions, "attempts": attempts,
            "health_verdict": "NOT_EVALUATED_NO_GATEWAY_PUBLISH_LAG",
            "redis_scope": "XREVRANGE strategy-state-isolated count32 only; loopback"}


async def run(args) -> dict:
    identity = adopt_service_environment(args.repository, args.expected_head)
    from redis.asyncio import Redis
    from project_mai_tai.market_data.momentum_warning_watchdog import WarningHealthReader
    from project_mai_tai.settings import Settings

    settings = Settings(_env_file=None)
    endpoint = urlsplit(settings.redis_url)
    if endpoint.scheme not in {"redis", "rediss"} or endpoint.hostname not in {
        "localhost", "127.0.0.1", "::1",
    }:
        raise ValueError("only configured loopback Redis permitted")
    reader = WarningHealthReader(settings.market_data_momentum_warning_health_path,
                                 settings.market_data_momentum_warning_log_directory)
    config = reader._read_file()
    client = Redis.from_url(settings.redis_url, decode_responses=True, max_connections=1,
                            socket_timeout=2, socket_connect_timeout=2, retry_on_timeout=False)
    try:
        result = await prove(reader, client, settings.redis_stream_prefix, config,
                             settings.market_data_momentum_warning_monitor_timeout_seconds)
    finally:
        await client.aclose()
    if process_identity(identity["gateway_pid"]) != identity["gateway_instance"]:
        result["status"] = "UNKNOWN"
        result["identity_changed"] = True
    result.update(identity=identity, observed_ns=time.time_ns(),
                  caller_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--expected-head", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        result = {"status": "UNKNOWN", "exception_class": type(exc).__name__,
                  "actual_service_user_proof": "NOT_ESTABLISHED"}
    print(json.dumps(result, allow_nan=False, sort_keys=True))
    return 0 if result["status"] == "READ_LIVE_2_OK" else 1


if __name__ == "__main__":
    raise SystemExit(main())
