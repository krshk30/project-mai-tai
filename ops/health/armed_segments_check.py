#!/usr/bin/env python3
"""CW-v2 ARMED-SEGMENT check (P1.4's external pager) — the last leg of P1.3+P1.4 (#475).

WHY THIS EXISTS
---------------
P1.3 seed-caps reconstructed segments on boot; the boot-hold suppresses ALL CW-v2 entries until
the bot's self-verify sees zero reconstructed-uncapped ("dangerous") segments. The hold **never
releases on a timeout** — by design, because releasing on a timer is exactly the bug it prevents.
That safety has a corollary: if `dangerous` persists, v2 sits silently entry-less forever. The bot
CANNOT page about that itself (it is the thing that is stuck), so the paging is external — this.

`schwab_1m_v2_bot.py::_cw_boot_hold_check` names the three conditions verbatim:
    "dangerous present, entries_held too long, or snapshot stale"

Before P1.4 armed segments were UNOBSERVABLE — fleet-flat checks *positions*, and an armed segment
holds none. That is why the stricter rule ("don't restart v2 while a segment is armed") was not
merely unenforced but unrunnable, and why a restart-while-flat manufactured the CPHI loss.

READ-ONLY. Independent of the OMS. Fail-LOUD: a broken check pages rather than exits quiet.

STATE SOURCE: the bot's own published snapshot (Redis `mai_tai:strategy-state-isolated`,
IsolatedBotStateEvent.payload.cw_armed_segments / .entries_held). ORB publishes to the SAME stream,
so the source_service/strategy_code filter is load-bearing.

NOT-A-FAULT cases (deliberately GREEN, learned from the fleet-health false-positive history):
  * v2 service INACTIVE  -> armed segments are in-memory ONLY; they die with the process. A stopped
    v2 has no armed state to be wrong about. This is the OPPOSITE of the OMS case (where down =
    blind = page): here down = safe. Stopping v2 is the documented way to clear armed segments.
  * safety flag OFF      -> P1.3 does not seed-cap, so `dangerous` is expected and meaningless.
    Paging on it would train the operator to ignore the pager.
  * `reconstructed` but CAPPED -> P1.3 did its job. Only reconstructed AND uncapped is dangerous.
  * a live post-boot flip (arm_bar_ts >= boot) is NEVER dangerous — the bar-ts discriminator.

Exit: 0 green/skip, 2 red (paged).
Usage: armed_segments_check.py [--selftest] [--dry-run]
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime

NTFY_URL = "https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
STREAM = "mai_tai:strategy-state-isolated"
UNIT = "project-mai-tai-schwab-1m-v2"
SOURCE_SERVICE = "schwab-1m-v2"
STRATEGY_CODE = "schwab_1m_v2"

SNAPSHOT_STALE_SECS = 180        # matches the oms-liveness watchdog's staleness bar
BOOT_HOLD_GRACE_SECS = 600       # the 07-16 deploy released within ONE publish cycle; 10min is generous
SCAN_COUNT = 40                  # the stream interleaves ORB + v2; scan back far enough to find v2

DRY_RUN = "--dry-run" in sys.argv


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def page(title: str, body: str, priority: str = "urgent", tags: str = "rotating_light") -> None:
    print(f"PAGE [{priority}] {title}: {body}")
    if DRY_RUN:
        return
    subprocess.run(
        ["curl", "-s", "-H", f"Title: {title}", "-H", f"Priority: {priority}",
         "-H", f"Tags: {tags}", "-d", body, NTFY_URL],
        capture_output=True, timeout=20,
    )


def unit_active() -> bool:
    return sh(["systemctl", "show", UNIT, "-p", "ActiveState", "--value"]) == "active"


def uptime_secs() -> float | None:
    """Seconds since the unit went active. Used to age the boot-hold: entries_held is TRUE at boot
    BY DESIGN, so it is only a fault once it has outlived the grace."""
    raw = sh(["systemctl", "show", UNIT, "-p", "ActiveEnterTimestampMonotonic", "--value"])
    now = sh(["cat", "/proc/uptime"])
    try:
        return float(now.split()[0]) - (int(raw) / 1_000_000)
    except Exception:
        return None


def safety_flag_on() -> bool:
    pid = sh(["systemctl", "show", UNIT, "-p", "MainPID", "--value"])
    if not pid or pid == "0":
        return False
    env = sh(["sudo", "tr", "\\0", "\\n", f"/proc/{pid}/environ"])
    for line in env.splitlines():
        if line.startswith("MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_ARMED_SEGMENT_SAFETY_ENABLED="):
            return line.split("=", 1)[1].strip().lower() == "true"
    return True  # absent => settings.py default (True); fail toward checking, not toward silence


def latest_v2_snapshot() -> tuple[dict | None, float | None]:
    """Newest v2 snapshot + its age. ORB shares this stream — filter or you page on the wrong bot."""
    raw = sh(["redis-cli", "--raw", "XREVRANGE", STREAM, "+", "-", "COUNT", str(SCAN_COUNT)])
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = evt.get("payload") or {}
        if evt.get("source_service") != SOURCE_SERVICE and payload.get("strategy_code") != STRATEGY_CODE:
            continue
        produced = evt.get("produced_at")
        age = None
        if produced:
            try:
                ts = datetime.fromisoformat(produced.replace("Z", "+00:00"))
                age = (datetime.now(UTC) - ts).total_seconds()
            except ValueError:
                age = None
        return payload, age
    return None, None


def main() -> int:
    if "--selftest" in sys.argv:
        page("✅ armed-segments SELFTEST", "[SELFTEST] armed-segment pager path OK — benign, no action.",
             priority="default", tags="white_check_mark")
        return 0

    # v2 down => armed segments cannot exist (in-memory only, die with the process). NOT a fault.
    if not unit_active():
        print("SKIP: v2 inactive — armed segments die with the process, nothing to check")
        return 0

    if not safety_flag_on():
        print("SKIP: cw_armed_segment_safety flag OFF — P1.3 inactive, 'dangerous' is meaningless")
        return 0

    payload, age = latest_v2_snapshot()

    # Active but not publishing => we are BLIND to armed state. That IS the fault.
    if payload is None:
        page("🔴 armed-segments CHECK BLIND",
             f"[armed-segments] v2 unit is ACTIVE but no snapshot found in the last {SCAN_COUNT} "
             f"{STREAM} entries. Armed-segment state is UNOBSERVABLE — the P1.3 boot-hold cannot be "
             f"verified. Check the v2 bot and its state-publish loop BY HAND.")
        return 2
    if age is not None and age > SNAPSHOT_STALE_SECS:
        page("🔴 armed-segments SNAPSHOT STALE",
             f"[armed-segments] v2 snapshot is {age:.0f}s old (>{SNAPSHOT_STALE_SECS}s) while the unit "
             f"is ACTIVE. The bot may be wedged; armed-segment state is unreliable. Verify BY HAND.")
        return 2

    segments = payload.get("cw_armed_segments") or []
    entries_held = bool(payload.get("entries_held"))
    dangerous = [s for s in segments if s.get("dangerous")]

    # (1) dangerous: a reconstructed segment survived P1.3's seed-cap. This is the CPHI shape.
    if dangerous:
        detail = ", ".join(
            f"{s.get('symbol')}(entries={s.get('entries_this_flip')}/{s.get('max_entries')})"
            for s in dangerous
        )
        page("🔴 v2 DANGEROUS ARMED SEGMENT",
             f"[armed-segments] {len(dangerous)} reconstructed-UNCAPPED segment(s) survived P1.3: "
             f"{detail}. v2 entries are held (boot-hold will NOT release). This is the cap-reset "
             f"shape that manufactured the CPHI loss. Investigate before v2 can enter again.")
        return 2

    # (2) entries_held past the grace: held is normal AT boot; outliving the grace is not.
    up = uptime_secs()
    if entries_held:
        if up is None:
            page("🔴 v2 BOOT-HOLD (uptime unknown)",
                 "[armed-segments] v2 reports entries_held=true and its uptime could not be read, so "
                 "the hold cannot be aged. v2 may be silently entry-less. Verify BY HAND.")
            return 2
        if up > BOOT_HOLD_GRACE_SECS:
            page("🔴 v2 BOOT-HOLD NEVER RELEASED",
                 f"[armed-segments] entries_held=true {up/60:.1f}min after boot (grace "
                 f"{BOOT_HOLD_GRACE_SECS/60:.0f}min) with 0 dangerous segments. v2 is taking NO entries "
                 f"and cannot page about itself. Check [V2-BOOT-HOLD] in the v2 log.")
            return 2
        print(f"OK: entries_held=true but only {up:.0f}s since boot (within {BOOT_HOLD_GRACE_SECS}s grace)")
        return 0

    armed = len(segments)
    capped = sum(1 for s in segments if s.get("capped"))
    recon = sum(1 for s in segments if s.get("reconstructed"))
    print(f"GREEN: {armed} armed segment(s) ({capped} capped, {recon} reconstructed, 0 dangerous), "
          f"entries_held=false, snapshot age {age:.0f}s" if age is not None else
          f"GREEN: {armed} armed segment(s), entries_held=false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
