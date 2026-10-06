"""Bounded local read-only control/token-owner and exact ORB display proof."""
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from release_policy import DAY, canonical, digest, moment, need
from retry_zero_readonly import FIELDS, identity

MAX = 524288
PATHS = ("/health", "/api/overview", "/bot/orb", "/api/bot/orb-schwab")


def page_proof(page, api):
    need(all(text in page for text in ("ORB Schwab Live", "live:schwab_1m_v2", "LIVE/SCHWAB",
         "<strong>Mode:</strong> LIVE", "<strong>Provider:</strong> SCHWAB", "JAGX")), "ORB page identity/routing missing")
    trades = re.findall(r"<span>Trades</span>\s*<strong>(\d+)</strong>", page)
    need(trades == ["1"], "ORB page trade count not exactly1")
    need("6.6700" in page and "6.6001" in page and "$-0.14" in page, "ORB page recorded JAGX result missing")
    rows = api["closed_today"]
    need(type(rows) is list and len(rows) == 1 and rows[0]["ticker"] == "JAGX", "ORB API not exactly one JAGX trade")
    row = rows[0]
    need(Decimal(str(row["quantity"])) == 2 and Decimal(str(row["entry_price"])) == Decimal("6.67")
         and Decimal(str(row["exit_price"])) == Decimal("6.6001"), "JAGX recorded quantity/prices changed")
    need(all(str(row[key]).startswith(DAY) for key in ("entry_time", "exit_time")), "JAGX trade day changed")
    need(abs(Decimal(str(api["daily_pnl"])) - Decimal("-0.1398")) < Decimal("0.000000001"), "ORB daily PNL changed")
    need(type(api["positions"]) is list and not api["positions"]
         and type(api["pending_open_symbols"]) is list and not api["pending_open_symbols"]
         and type(api["pending_close_symbols"]) is list and not api["pending_close_symbols"], "ORB displayed book not flat")
    need(type(api["recent_fills"]) is list and len(api["recent_fills"]) == 2
         and type(api["recent_orders"]) is list and len(api["recent_orders"]) == 2, "ORB order/fill population differs")
    need("SESSION COMPLETE" in page and 'http-equiv="refresh"' not in page,
         "after-close ORB session freeze not displayed")
    return dict(mode="LIVE", provider="SCHWAB", routing="LIVE/SCHWAB", account="live:schwab_1m_v2",
                symbol="JAGX", trades=1, quantity=2, entry="6.67", exit="6.6001", daily_pnl="-0.1398")


def token_proof(status, store, now):
    need(status.get("enabled") is True and status.get("health") == "healthy"
         and type(status.get("dead_token_retries")) is int and status["dead_token_retries"] == 0
         and status.get("last_error") == "", "control token refresher not healthy/enabled")
    need(all(type(store.get(key)) is str and bool(store[key].strip()) for key in ("access_token", "refresh_token")),
         "token store credentials absent")
    expires = moment(store["expires_at"])
    need(expires >= now + timedelta(seconds=180), "access token lacks180s restart/proof margin")
    return dict(enabled=True, health="healthy", dead_token_retries=0, expires_at=expires.isoformat(),
                forced_refresh=False, alternate_writer_enabled=False)


def state(service="control"):
    need(service in {"control", "oms"}, "unapproved control-owner unit")
    result = subprocess.run(["systemctl", "show", "project-mai-tai-" + service + ".service",
                             *["--property=" + key for key in FIELDS]], check=True, capture_output=True, timeout=5)
    need(not result.stderr.strip() and len(result.stdout) <= 4096, "control unit state unreadable")
    pairs = [line.split("=", 1) for line in result.stdout.decode().splitlines()]
    need(len(pairs) == len(FIELDS) and all(len(pair) == 2 for pair in pairs), "control unit state malformed")
    result = dict(pairs)
    identity(result, result)
    return result


def adapter_owner():
    before = state("oms")
    with Path("/proc/" + before["MainPID"] + "/environ").open("rb") as file:
        raw = file.read(262145)
    need(len(raw) <= 262144, "OMS environment exceeds bound")
    key = "MAI_TAI_SCHWAB_ADAPTER_TOKEN_REFRESH_ENABLED"
    pairs = [piece.decode().split("=", 1) for piece in raw.split(b"\0") if piece]
    need(all(len(pair) == 2 for pair in pairs), "OMS token-owner environment malformed")
    found = [pair for pair in pairs if pair[0].upper() == key]
    # The unchanged approved Settings default is false; no numeric/default-zero inference.
    need(found in ([], [[key, "false"]]), "OMS incidental token writer enabled/aliased/duplicate")
    need(state("oms") == before, "OMS token-owner identity moved")
    return dict(pid=int(before["MainPID"]), refresh_enabled=False,
                provenance="explicit-process-false" if found else "pinned-settings-default-false")


def owners(root=Path("/proc")):
    candidates = [path for path in root.iterdir() if path.name.isdigit()]
    need(len(candidates) <= 4096, "control process census exceeds4096 bound")
    found = []
    for path in candidates:
        try:
            with (path / "cmdline").open("rb") as file:
                raw = file.read(4097)
        except FileNotFoundError:
            continue  # Other short-lived processes may leave; main state is bracketed separately.
        need(len(raw) <= 4096, "process cmdline exceeds bound; cannot prove sole owner")
        args = [piece.decode() for piece in raw.split(b"\0") if piece]
        if any(Path(arg).name == "mai-tai-control" or arg == "project_mai_tai.services.control_plane:app" for arg in args):
            found.append(int(path.name))
    return sorted(found)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(path):
    need(path in PATHS, "unapproved control URL")
    url = "http://127.0.0.1:8100" + path
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(Request(url, method="GET", headers={"Cache-Control": "no-cache"}), timeout=10) as reply:
        need(reply.status == 200 and reply.url == url, "control HTTP status/redirect differs")
        need(reply.headers.get("Content-Encoding", "identity") == "identity", "encoded control response unsupported")
        expected = "text/html" if path == "/bot/orb" else "application/json"
        need(reply.headers.get("Content-Type", "").split(";")[0] == expected, "control response content-type differs")
        need("no-store" in reply.headers.get("Cache-Control", ""), "control response caching not disabled")
        raw = reply.read(MAX + 1)
    need(0 < len(raw) <= MAX, "control HTTP response missing/overflow")
    return raw


def collect(phase, pid, old_pid=None):
    from project_mai_tai.settings import Settings
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    need(settings.schwab_adapter_token_refresh_enabled is False, "alternate OMS token writer enabled")
    before = state()
    adapter = adapter_owner()
    need(int(before["MainPID"]) == pid and owners() == [pid], "control sole process owner unproven")
    if phase == "after":
        need(old_pid is not None and old_pid != pid and not Path(f"/proc/{old_pid}").exists(), "old control PID not retired")
    health_raw = get("/health")
    health = json.loads(health_raw)
    need(health.get("service") == "control-plane" and health.get("database_connected") is True
         and health.get("redis_connected") is True and health.get("errors") == [], "control health dependencies unreadable/degraded")
    overview_raw = get("/api/overview")
    overview = json.loads(overview_raw)
    path = Path(settings.schwab_token_store_path)
    need(path.is_absolute(), "token store path not absolute")
    with path.open("rb") as file:
        token_raw = file.read(65537)
    need(len(token_raw) <= 65536, "token store exceeds bound")
    token = token_proof(overview["schwab_token_refresher"], json.loads(token_raw), datetime.now(timezone.utc))
    proof = dict(verdict="PASS", phase=phase, pid=pid, old_pid=old_pid,
                 token=token, adapter=adapter, before=before, owners=[pid], old_pid_retired=phase == "after",
                 http_hashes={"/health": digest(health_raw), "/api/overview": digest(overview_raw)})
    if phase == "after":
        page_raw, api_raw = get("/bot/orb"), get("/api/bot/orb-schwab")
        proof["page"] = page_proof(page_raw.decode(), json.loads(api_raw))
        proof["raw_page"] = page_raw.decode()
        proof["raw_orb_api"] = json.loads(api_raw)
        proof["raw_orb_api_text"] = api_raw.decode()
        proof["http_hashes"].update({"/bot/orb": digest(page_raw), "/api/bot/orb-schwab": digest(api_raw)})
    after = state()
    need(before == after and owners() == [pid], "control identity/owner moved during proof")
    proof.update(after=after, measured_at_utc=datetime.now(timezone.utc).isoformat())
    return proof


def validate(proof, phase, pid, old_pid, since, until):
    need(proof["verdict"] == "PASS" and proof["phase"] == phase and proof["pid"] == pid
         and proof["old_pid"] == old_pid and proof["owners"] == [pid], "control proof identity/phase incomplete")
    identity(proof["before"], proof["after"])
    need(type(proof["adapter"]["pid"]) is int and proof["adapter"]["pid"] > 0
         and proof["adapter"]["refresh_enabled"] is False
         and proof["adapter"]["provenance"] in {"explicit-process-false", "pinned-settings-default-false"},
         "OMS token writer proof absent/enabled")
    need(int(proof["before"]["MainPID"]) == pid and since <= moment(proof["measured_at_utc"]) <= until,
         "control receipt PID/time unbound")
    token = proof["token"]
    need(token["enabled"] is True and token["health"] == "healthy"
         and type(token["dead_token_retries"]) is int and token["dead_token_retries"] == 0
         and token["forced_refresh"] is False and token["alternate_writer_enabled"] is False
         and moment(token["expires_at"]) >= until + timedelta(seconds=180), "token receipt not fresh/healthy")
    if phase == "after":
        need(proof["old_pid_retired"] is True and old_pid != pid, "old control not retired")
        need(proof["page"] == page_proof(proof["raw_page"], proof["raw_orb_api"]), "control page receipt inconsistent")
        need(proof["http_hashes"]["/bot/orb"] == digest(proof["raw_page"].encode()), "control page hash differs")
        need(json.loads(proof["raw_orb_api_text"]) == proof["raw_orb_api"]
             and proof["http_hashes"]["/api/bot/orb-schwab"] == digest(proof["raw_orb_api_text"].encode()),
             "control API raw evidence/hash differs")
    return proof


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--old-pid", type=int)
    args = parser.parse_args()
    try:
        since = datetime.now(timezone.utc)
        result = collect(args.phase, args.pid, args.old_pid)
        print(canonical(validate(result, args.phase, args.pid, args.old_pid, since, datetime.now(timezone.utc))).decode(), end="")
        return 0
    except Exception as exc:
        print("STOP control proof error_type=" + type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
