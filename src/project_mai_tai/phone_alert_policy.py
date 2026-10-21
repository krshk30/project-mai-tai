"""ALERTS1021-B phone-only routing. Unknown evidence keeps the phone path."""

from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat


SAFETY_STOP = Path("/var/lib/project-mai-tai/option-a/safety-stop.json")
STOP_PAGES = Path("/var/lib/project-mai-tai/option-a/phone-stop-pages.jsonl")
KEEP = (True, "keep_unclassified_or_risk")


def protected(title, body):
    text = title + "\n" + body
    return bool(re.search(r"LIVE[_ -]MONEY|reconcile[ _-]drift|BAR[ _-]HOLE|NOT[ _-]READY|"
                          r"service runtime.*inactive", text, re.I | re.S))


def phone_decision(title, body):
    """Only producer-bound exact informational classes, never generic RED/UNKNOWN."""
    if protected(title, body):
        return True, "keep_protected_risk"
    if title == "Intent refusal class - our defect" and re.fullmatch(
        r"(?:client_abort|broker_reject|skipped_before_submit|could_not_tell) "
        r"live:(?:orb|schwab_1m_v2) cancel n=\d+ \([^\n)]+\) - cancel_target_not_found"
        r"(?:\n\nFull report: [^\n]+)?", body,
    ):
        return False, "cancel_target_not_found_noop_only"
    if title == "AMBER mai-tai readiness warnings" and re.fullmatch(
        r"SEED-EXPOSURE\s+VERDICT\s*:\s*\d+ EXPOSED of \d+ swept "
        r"\(\d+ boundary, \d+ internal\)", body.strip(),
    ):
        return False, "seed_exposure_aggregate_info"
    if title in {"scanner capture CHECK UNKNOWN", "scanner capture CHECK ERROR"} and re.match(
        r"^SCANNER_CAPTURE COULD_NOT_TELL(?:\s|$)", body,
    ):
        return False, "scanner_capture_check_blind_only"
    if (title == "V2 poll-reenrol FIRED (good)" and "reenroll=PROVEN" in body
            and not re.search(r"REPEATED|BREACH|COULD_NOT_TELL|UNKNOWN", body)):
        return False, "poll_reenrol_good_news"
    if (re.fullmatch(r"V2 first live cross \d{4}-\d{2}-\d{2}", title)
            and body.startswith("[OK] ") and "composition=OK" in body
            and not re.search(r"BREACH|COULD_NOT_TELL|UNKNOWN", body)):
        return False, "first_live_cross_ok_info"
    # LIVE_LOCKED15:45 is fixed by approved99fc's mirror, not by hiding real drift.
    return KEEP


def manual_only(*, account, native_ids, requested_ids, bot_ids, complete):
    """A known-empty exact lookup is proof; missing/partial/failed lookup is not."""
    return bool(complete is True and isinstance(account, str) and re.fullmatch(r"live:[A-Za-z0-9_.:-]+", account)
                and native_ids and all(isinstance(oid, str) and oid for oid in native_ids)
                and set(native_ids) <= set(requested_ids) and not set(native_ids) & set(bot_ids))


def locked_window_notice_resolved(title, body, *, hour, minute):
    """Replay-only old-mirror counterfactual; never suppress a live drift sender."""
    keys = {"strategy_schwab_1m_v2_entry_window_end_hour_et": 15,
            "strategy_schwab_1m_v2_entry_window_end_minute_et": 45}
    found = re.findall(r"(\w+)\n\s+LIVE_LOCKED=\d+\s+live env='(\d+)'", body)
    return bool(title == "RED live config drift" and (hour, minute) == (15, 45)
                and len(found) == 2 and {key: int(value) for key, value in found} == keys
                and re.search(r"2 env-set drift\(s\), 0 unset-and-divergent, 0 unknown key\(s\)", body))


def stop_event_key(title, body, latch):
    if title != "Option A paper STOP" or protected(title, body):
        return None
    try:
        message = json.loads(body)
        if (message["action"] != "stop_paper" or type(message["systemctl_rc"]) is not int
                or message["systemctl_rc"] != 0
                or latch["clear_policy"] != "explicit human/claude-1 action only"):
            return None
        start = datetime.fromisoformat(latch["at_utc"].replace("Z", "+00:00"))
        notice = datetime.fromisoformat(message["at_utc"].replace("Z", "+00:00"))
        day = datetime.fromisoformat(latch["session_date"]).date()
        if start.tzinfo is None or notice.tzinfo is None or start > notice or not latch["reason"]:
            return None
        identity = {"at_utc": start.astimezone(UTC).isoformat(), "session_date": str(day),
                    "reason": latch["reason"], "clear_policy": latch["clear_policy"]}
        return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def read_stop_latch(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022
                or info.st_uid not in {0, os.geteuid()} or info.st_size > 16384):
            raise ValueError("stop identity file untrusted")
        raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError("stop identity file oversized")
        return json.loads(raw)


def send_stop_once(title, body, send, *, latch_path=SAFETY_STOP, journal=STOP_PAGES):
    """Do not delay STOP itself. Runs only at its existing notification boundary.

    Failed/unknown evidence and failed delivery never consume the event. The
    existing guard retry/timeout/audit contracts remain in their callers.
    """
    if title != "Option A paper STOP":
        return send()
    attempted = False
    delivered = False
    try:
        latch = read_stop_latch(latch_path)
        key = stop_event_key(title, body, latch)
        if key is None:
            return send()
        journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(journal, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "r+") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_uid not in {0, os.geteuid()}:
                raise ValueError("stop page journal untrusted")
            fcntl.flock(stream, fcntl.LOCK_EX)
            raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("stop page journal oversized; keep phone")
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            if any(not isinstance(row, dict) for row in rows):
                raise ValueError("stop page journal malformed")
            delivered_before = any(row.get("event_key") == key and row.get("delivered") is True for row in rows)
            if delivered_before:
                delivered = True
            else:
                attempted = True
                delivered = send()
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps({"event_key": key, "delivered": delivered,
                "phone_action": "same_event_already_delivered" if delivered_before else "send_attempt",
                "title": title, "body": body, "at_utc": datetime.now(UTC).isoformat()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
            return delivered
    except (OSError, ValueError, TypeError):
        # Evidence/storage failures may duplicate a page, never hide a new STOP.
        return delivered if attempted else send()
