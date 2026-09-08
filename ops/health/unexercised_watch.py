#!/usr/bin/env python3
"""Watch the deployed-but-never-fired conditions and PAGE when one finally occurs.

⛔ WHY THIS EXISTS. Fifteen items were deployed and unexercised, several for three weeks, and the
board's own instruments could not tell "the condition never happened" from "nobody was looking".
Two proofs of that, both from 2026-09-08:
  - SIL1's exit-reject alarm had never once fired in its life; a forged 8-reject episode fired it
    immediately, so the alarm was always correct and simply never triggered.
  - The D6 grader's watchdog had been RED since Saturday and nobody read it.
⇒ A watcher whose output nobody reads is worth nothing. This one PAGES.

⭐ EVERY CONDITION CARRIES ITS OWN DENOMINATOR. A zero is only a result when the denominator is
non-zero. Three distinct outcomes, never collapsed into "clean":
    OCCURRED      fired >= 1                      -> page once, on the 0 -> 1 transition
    NEVER_OCCURRED denominator > 0 and fired == 0  -> a real measured zero
    NEVER_LOOKED  denominator == 0                 -> UNEXERCISED; the watcher saw nothing at all
⛔ COULD_NOT_TELL is returned whenever a query fails. A broken query must never read as a clean
zero -- that is the false-clean failure this whole board exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

NTFY_URL = "https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
STATE_PATH = Path("/home/trader/unexercised_watch/state.json")
STATUS_PATH = Path("/home/trader/unexercised_watch/STATUS.txt")
V2_LOG = Path("/var/log/project-mai-tai/schwab-1m-v2.log")
OMS_LOG = Path("/var/log/project-mai-tai/oms.log")

OCCURRED = "OCCURRED"
NEVER_OCCURRED = "NEVER_OCCURRED"
NEVER_LOOKED = "NEVER_LOOKED"
COULD_NOT_TELL = "COULD_NOT_TELL"

# A condition that has had a denominator for this many consecutive days without ever firing is
# still not a fault -- but one with NO denominator for this long means the watcher is blind, and
# that IS a fault worth paging about. [[feedback_a_watch_that_fails_to_a_false_clean]]
BLIND_DAYS_BEFORE_PAGE = 3


@dataclass
class Reading:
    name: str
    verdict: str
    fired: int
    denominator: int
    detail: str


def _dsn() -> str:
    out = subprocess.run(
        ["sudo", "grep", "-E", "^MAI_TAI_DATABASE_URL=", "/etc/project-mai-tai/project-mai-tai.env"],
        capture_output=True, text=True, timeout=20,
    )
    line = (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else ""
    return line.split("=", 1)[1] if "=" in line else ""


def _psql(sql: str) -> list[str]:
    """Run one SQL statement as the app user. Raises on any failure -- the caller turns that
    into COULD_NOT_TELL rather than a zero."""
    dsn = _dsn()
    if not dsn:
        raise RuntimeError("MAI_TAI_DATABASE_URL unreadable")
    pwd = dsn.split("://", 1)[1].split(":", 1)[1].split("@", 1)[0]
    env = dict(os.environ, PGPASSWORD=pwd)
    out = subprocess.run(
        ["psql", "-U", "mai_tai", "-h", "localhost", "-d", "project_mai_tai", "-tA", "-c", sql],
        capture_output=True, text=True, env=env, timeout=60,
    )
    if out.returncode != 0:
        raise RuntimeError(f"psql failed: {(out.stderr or '').strip()[:200]}")
    return [r for r in (out.stdout or "").splitlines() if r.strip()]


def _log_count(path: Path, marker: str) -> int:
    """Count a marker across the live log AND its rotations. ⛔ Logs are root:root 640, so this
    must run as root -- a permission failure returns a FALSE ZERO otherwise, which is exactly the
    trap that cost a day on 2026-09-06. We raise instead."""
    if not path.exists():
        raise RuntimeError(f"{path} missing")
    if not os.access(path, os.R_OK):
        raise RuntimeError(f"{path} unreadable -- run as root, a false zero is not a result")
    total = 0
    for candidate in sorted(path.parent.glob(path.name + "*")):
        tool = "zgrep" if candidate.suffix == ".gz" else "grep"
        # ⛔ -F IS LOAD-BEARING, AND SO IS THE RETURN CODE. Every marker here is bracketed, e.g.
        # "[V2-HALT-CONFIRMED]". As a basic regex that is a CHARACTER CLASS containing the range
        # T-C, which is invalid, so grep exits 2 with "Invalid range end" and an EMPTY stdout.
        # The first version parsed that empty output as 0 and reported NEVER_OCCURRED against a
        # denominator of 2,080. Three markers, three false zeros -- the precise false-clean this
        # watcher exists to prevent, inside the watcher.
        # grep exit codes: 0 = matched, 1 = no match, >=2 = ERROR. Only 0 and 1 are answers.
        out = subprocess.run([tool, "-cF", marker, str(candidate)], capture_output=True, text=True)
        if out.returncode >= 2:
            raise RuntimeError(
                f"{tool} failed on {candidate.name} rc={out.returncode}: "
                f"{(out.stderr or '').strip()[:120]}"
            )
        total += int((out.stdout or "0").strip() or 0)
    return total


# --------------------------------------------------------------------------------------------
# The four conditions nobody can force. Each returns (fired, denominator, detail).
# --------------------------------------------------------------------------------------------

def check_halt() -> tuple[int, int, str]:
    """HALT1/HALT2 -- a REAL Schwab halt seen by v2.
    Denominator: deduplicated quote observations with a usable prior print, which is exactly what
    halt_monitor publishes. Zero there means the detector never had anything to judge."""
    fired = _log_count(V2_LOG, "[V2-HALT-CONFIRMED]")
    # ⛔ The denominator lives ONLY in the v2 bot's published state. Two wrong sources were tried
    # first and both read as a clean zero: dashboard_snapshots carries no halt_monitor at all (0
    # rows), and taking the NEWEST strategy-state-isolated entry returns whichever bot published
    # last, which is usually not v2. A wrong source here reports NEVER_LOOKED while the real
    # denominator is in the thousands. Scan back and require a v2 payload, or say COULD_NOT_TELL.
    out = subprocess.run(
        ["redis-cli", "XREVRANGE", "mai_tai:strategy-state-isolated", "+", "-", "COUNT", "80"],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError("redis-cli XREVRANGE failed")
    denominator = None
    for chunk in (out.stdout or "").split("schwab_1m_v2")[1:]:
        marker = chunk.find('"denominator"')
        if marker != -1:
            digits = "".join(ch for ch in chunk[marker + 13 : marker + 40] if ch.isdigit())
            if digits:
                denominator = int(digits)
                break
    if denominator is None:
        raise RuntimeError("no schwab_1m_v2 halt_monitor payload in the last 80 state entries")
    return fired, denominator, "denominator = deduplicated quote observations (v2 published state)"


def check_two_broker_close() -> tuple[int, int, str]:
    """CONF3 -- a confirmation exit that closed BOTH broker legs.
    Denominator: confirmation-exit evaluations. Fired: evaluations whose fan-out actually closed
    the second account too."""
    rows = _psql("select count(*) from v2_confirmation_exit_evaluations")
    denominator = int(rows[0] or 0) if rows else 0
    fired = _log_count(OMS_LOG, "[OMS-V2-CONFIRMATION-EXIT-FANOUT-CLOSED]")
    return fired, denominator, "denominator = confirmation-exit evaluations"


def check_reject_storm() -> tuple[int, int, str]:
    """SIL1 -- the exit-reject alarm firing on a live storm.

    Denominator: Schwab MANAGED-EXIT SELL rejects only -- the single population the alarm can
    cover. ⛔ NOT "the two live accounts": live:orb's threshold is deliberately UNSET and can
    never fire, so counting it inflated the denominator from 264 to 2,080 and answered a
    different question confidently."""
    # ⛔ THE DENOMINATOR MUST BE THE POPULATION THE ALARM CAN SEE. The first version counted every
    # live reject on both accounts -- 2,080 -- while the alarm counts only Schwab MANAGED-EXIT
    # sells, which is 264. A zero against the wrong population is not a measured zero; it is a
    # different question answered confidently. live:orb is deliberately UNSET and can never fire,
    # so including it inflates the denominator with cases the alarm is not covering.
    rows = _psql(
        "select count(*) from broker_order_events e join broker_orders o on o.id=e.order_id "
        "join broker_accounts b on b.id=o.broker_account_id "
        "where e.event_type='rejected' and b.name='live:schwab_1m_v2' and o.side='sell' "
        "and (e.payload->'metadata'->>'oms_v2_managed_exit')='true' "
        "and e.event_at >= now() - interval '30 days'"
    )
    denominator = int(rows[0] or 0) if rows else 0
    fired = _log_count(OMS_LOG, "[OMS-V2-EXIT-REJECT-ALARM]")
    return fired, denominator, "denominator = Schwab managed-exit sell rejects, 30d (the only population the alarm covers)"


def check_resting_fill() -> tuple[int, int, str]:
    """PEX1 -- a resting entry that actually filled and was admitted by the paper harness.
    Denominator: fills the harness classified at all. Zero means it was never offered one."""
    # ⛔ NOT a LIKE on the payload. The first version matched '%resting%' anywhere and counted
    # LATE_MIRROR rows as resting fills. The condition is a real harness EXIT decision on an
    # admitted fill, which is exactly event_type='PAPER_EXIT'.
    rows = _psql("select count(*) from paper_exit_events")
    denominator = int(rows[0] or 0) if rows else 0
    rows2 = _psql("select count(*) from paper_exit_events where event_type='PAPER_EXIT'")
    fired = int(rows2[0] or 0) if rows2 else 0
    return fired, denominator, "denominator = paper_exit_events classified; fired = PAPER_EXIT decisions"


CONDITIONS = {
    "HALT_REAL": check_halt,
    "CONF3_TWO_BROKER_CLOSE": check_two_broker_close,
    "SIL1_REJECT_STORM": check_reject_storm,
    "PEX1_RESTING_FILL": check_resting_fill,
}


def classify(fired: int, denominator: int) -> str:
    if fired > 0:
        return OCCURRED
    if denominator > 0:
        return NEVER_OCCURRED
    return NEVER_LOOKED


def page(title: str, body: str) -> bool:
    """⛔ ASCII titles only -- ntfy rejects non-ASCII headers."""
    out = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "--max-time", "20",
         "-H", f"Title: {title.encode('ascii', 'ignore').decode('ascii')}",
         "-H", "Priority: high", "-H", "Tags: rotating_light",
         "-d", body, NTFY_URL],
        capture_output=True, text=True,
    )
    return out.returncode == 0


def _load_state(path: Path) -> tuple[dict, bool]:
    """Return (state, memory_lost). ⛔ A MISSING file is a first run; an UNPARSEABLE one is LOST
    MEMORY, and the two must not be collapsed. The first version caught both and reset to {}, so a
    truncated state file replayed every historical alert as a fresh first occurrence."""
    if not path.exists():
        return {}, False
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return (loaded, False) if isinstance(loaded, dict) else ({}, True)
    except (OSError, ValueError):
        return {}, True


def _write_state(path: Path, state: dict) -> None:
    """⛔ ATOMIC. write_text truncates in place, so a crash mid-write leaves half a JSON document
    and the next run loses every delivery record. Write beside the target and rename."""
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=str(STATE_PATH))
    ap.add_argument("--status", default=str(STATUS_PATH))
    ap.add_argument("--no-page", action="store_true", help="evaluate and write status, send nothing")
    args = ap.parse_args(argv)

    state_path, status_path = Path(args.state), Path(args.status)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state, memory_lost = _load_state(state_path)

    now = datetime.now(UTC)
    readings: list[Reading] = []
    pending: list[tuple[str, str, str, str]] = []   # (condition, kind, title, body)
    for name, fn in CONDITIONS.items():
        prior = dict(state.get(name, {}))
        try:
            fired, denominator, detail = fn()
            verdict = classify(fired, denominator)
        except Exception as exc:  # noqa: BLE001 - a failed query is COULD_NOT_TELL, never a zero
            fired, denominator, detail, verdict = 0, 0, f"{type(exc).__name__}: {exc}", COULD_NOT_TELL
        readings.append(Reading(name, verdict, fired, denominator, detail))

        if verdict == COULD_NOT_TELL:
            # ⛔ PRESERVE, NEVER OVERWRITE. The first version wrote fired=0 here, which reset the
            # transition memory: a condition that had already OCCURRED and paged would page AGAIN
            # the moment its query recovered. A failed query must not re-arm a delivered alarm.
            record = dict(prior)
            record["verdict"] = COULD_NOT_TELL
            record["last_run_at"] = now.isoformat()
            record["could_not_tell_since"] = prior.get("could_not_tell_since") or now.isoformat()
            if not prior.get("could_not_tell_paged") and not args.no_page:
                pending.append((
                    name, "could_not_tell",
                    f"CANNOT TELL {name} -- the watcher cannot read its own condition",
                    f"{name} could not be evaluated: {detail}\n"
                    f"This is NOT a clean zero. The condition may be occurring unseen.",
                ))
            state[name] = record
            continue

        # A real reading. Clear any could-not-tell episode.
        # ⛔ STICKY, NOT DERIVED FROM THE COUNT. The first version computed
        # `prior_fired > 0 and delivered`, so a 1 -> 0 -> 1 count sequence lost the announcement
        # memory at the dip and paged the SAME "first occurrence" twice. Log rotation and row
        # pruning both make a count fall. Once announced, always announced.
        announced = bool(prior.get("announced", False))
        delivered = bool(prior.get("delivered", False)) or announced
        if memory_lost and verdict == OCCURRED:
            # Memory is gone and we cannot tell whether this was already announced. Bias to
            # SILENCE: an alarm that repeats history is the wallpaper failure the operator refused.
            # The CORRUPT page below tells him to look.
            announced = True
        if verdict == OCCURRED and not announced and not args.no_page:
            # ⛔ QUEUED, NOT SENT. Every page is deferred until AFTER the state file is written.
            # Measured 2026-09-08: with the send here and the write at the end, a state-write
            # failure raised after paging and the SAME page went out every run -- 3 identical
            # pages in 3 crashed invocations. No page may be sent before its memory is durable.
            pending.append((
                name, "occurred",
                f"FIRED {name} -- first occurrence",
                f"{name} has occurred for the first time since deployment.\n"
                f"count={fired} denominator={denominator}\n{detail}\n"
                f"This condition could not be forced; it has now happened. Read it today.",
            ))
        blind_since = prior.get("blind_since") if verdict == NEVER_LOOKED else None
        if verdict == NEVER_LOOKED and not blind_since:
            blind_since = now.isoformat()
        blind_paged = bool(prior.get("blind_paged", False))
        if verdict == NEVER_LOOKED and blind_since and not args.no_page and not blind_paged:
            days = (now - datetime.fromisoformat(blind_since)).days
            if days >= BLIND_DAYS_BEFORE_PAGE:
                pending.append((
                    name, "blind",
                    f"BLIND {name} -- denominator 0 for {days}d",
                    f"{name} has had NO denominator for {days} days. This is not a clean zero -- "
                    f"the watcher has never had anything to judge. UNEXERCISED, never PASS.",
                ))
        state[name] = {
            "fired": fired, "denominator": denominator, "verdict": verdict,
            "last_run_at": now.isoformat(), "blind_since": blind_since,
            "blind_paged": blind_paged,
            # ⛔ DELIVERY IS NOT ASSUMED. `delivered` flips to True only after ntfy ACCEPTS the
            # send. The first version recorded the transition regardless, so a failed delivery was
            # suppressed FOREVER -- the alarm believed it had spoken when nothing was sent.
            "delivered": delivered, "announced": announced,
            "could_not_tell_since": None, "could_not_tell_paged": False,
        }

    if memory_lost and not args.no_page:
        pending.append((
            "__state__", "corrupt",
            "STATE LOST -- unexercised watch cannot remember what it announced",
            "The watcher's state file was unreadable. Delivery memory is gone, so occurrence "
            "alarms are SUPPRESSED this run rather than replayed. Read the conditions by hand.",
        ))

    # ⛔ ORDER IS THE GUARD. State durable first, pages second.
    _write_state(state_path, state)
    confirmed: list[tuple[str, str]] = []
    for name, kind, title, body in pending:
        if page(title, body):
            confirmed.append((name, kind))
    for name, kind in confirmed:
        if name not in state:
            continue
        if kind == "occurred":
            state[name]["delivered"] = True
            state[name]["announced"] = True
        elif kind == "blind":
            state[name]["blind_paged"] = True
        elif kind == "could_not_tell":
            state[name]["could_not_tell_paged"] = True
    if confirmed:
        _write_state(state_path, state)
    lines = [
        f"[UNEXERCISED-WATCH] run_at={now.isoformat()} conditions={len(readings)}",
        "⛔ A zero is a result only when the denominator is non-zero. NEVER_LOOKED is UNEXERCISED.",
    ]
    for r in readings:
        lines.append(f"  {r.name:24} {r.verdict:15} fired={r.fired:<6} denominator={r.denominator:<8} {r.detail}")
    status_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if all(r.verdict != COULD_NOT_TELL for r in readings) else 2


if __name__ == "__main__":
    sys.exit(main())
