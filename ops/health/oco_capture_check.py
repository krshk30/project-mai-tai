"""OCO CAPTURE CHECK — watches the two flags enabled 2026-07-27 evening.

Both ship OFF by default and were switched on together, each with one unproven assumption. This is
the pager that answers "did they work?" without a human staring at logs.

  1. webull_bracket_realign_on_fill_enabled  (PR #562)
     The combo is placed atomically, so both exit legs are priced off the pre-trade REFERENCE
     before the master fills. The Webull leg enters at MARKET on the ATR cross -- exactly where
     slippage lives -- so the realised bracket drifts. Measured 07-27 across 12 combos:
     ALIGNED 8 / DRIFTED 4, with the "-5%" stop actually running -3.85%..-5.83%.
     ⚠ UNPROVEN: whether v3 replace_order accepts a PARTIAL combo (the two exit legs, master
     omitted because filled). A rejection is SAFE -- the original bracket stays and the position
     stays protected -- but it means the fix is not working, so it pages AMBER, not RED.

  2. oms_record_native_oco_exit_fills_enabled  (PR #565 + #566)
     Since the native OCO went live (07-22) NO exit fill has been recorded -- the exit executes on
     a broker child leg the OMS never placed. The bot page's completed trades and P&L have been
     BLANK for five days (Schwab sell fills 07-21: 5 -> 07-23: 0 -> 07-27: 0).
     ⚠ UNPROVEN IN PROD: no exit fill has ever actually been WRITTEN; that path has only run
     against tests.

RED   = a live-money or data-integrity fault (a $0 exit booked; entries closing with no exit
        recorded well into the session).
AMBER = the fix is not taking effect (brackets still drifting, realign rejected by the broker).
GREEN = brackets aligned and exits pairing.

READ-ONLY: queries the DB and reads logs. Places, cancels and modifies nothing.
Exit 0 green/skip, 1 amber, 2 red.
"""
from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
TOPIC = "mai-tai-preopen-28806a5a97b7"
LOG = "/var/log/project-mai-tai/oms.log"
# Bracket alignment is judged from the OMS LOG, not by re-reading the broker: a Webull call every
# 15 min would court the same 429s that broke the exit-fill probe. Use verify_realign.py by hand
# for the price-level audit.
# how long after an entry we still consider "the exit may legitimately not have happened yet"
EXIT_GRACE_MIN = 45


def push(title: str, body: str, priority: str, tags: str) -> None:
    # HTTP headers are latin-1: a non-ASCII Title raises UnicodeEncodeError and the page is LOST.
    # Caught on the first dry run. Body is fine (sent as encoded data), headers are not.
    title = title.encode("ascii", "replace").decode("ascii")
    try:
        requests.post(
            f"https://ntfy.sh/{TOPIC}",
            data=body.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 - a pager that crashes is worse than a quiet one
        print(f"ntfy push failed: {type(exc).__name__}")


def log_count(pattern: str) -> int:
    """Count TIMESTAMPED matches today. ⛔ Anchored with ^2026-: `awk '$0 >= date'` compares
    LEXICALLY, so untimestamped traceback/JSON lines pass any date filter and drag in history
    (that produced two false alarms on 07-27 — reported 1630 and 414, actual 0 and 6)."""
    today_utc = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        out = subprocess.run(
            ["sudo", "grep", "-hcE", f"^{today_utc}.*{pattern}", LOG],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return int(out or 0)
    except Exception:  # noqa: BLE001
        return 0


def main() -> int:
    selftest = "--selftest" in sys.argv
    s = get_settings()
    realign_on = bool(getattr(s, "webull_bracket_realign_on_fill_enabled", False))
    capture_on = bool(getattr(s, "oms_record_native_oco_exit_fills_enabled", False))
    if not selftest and not (realign_on or capture_on):
        print("both flags OFF — nothing to watch")
        return 0

    reds: list[str] = []
    ambers: list[str] = []
    notes: list[str] = []

    sf = build_session_factory(s)
    with sf() as sess:
        buys, sells, zero_priced, unpaired = sess.execute(text("""
            SELECT
              count(*) FILTER (WHERE f.side = 'buy'),
              count(*) FILTER (WHERE f.side = 'sell'),
              count(*) FILTER (WHERE f.side = 'sell' AND f.price <= 0),
              count(*) FILTER (WHERE f.side = 'buy'
                               AND f.filled_at < now() - make_interval(mins => :grace)
                               AND NOT EXISTS (
                                     SELECT 1 FROM fills x
                                     WHERE x.symbol = f.symbol AND x.side = 'sell'
                                       AND x.broker_account_id = f.broker_account_id
                                       AND x.filled_at >= f.filled_at)
                               -- a STILL-HELD position has no exit yet, and that is correct;
                               -- only a CLOSED-OUT entry with no recorded exit is a fault
                               AND NOT EXISTS (
                                     SELECT 1 FROM oms_managed_positions m
                                     WHERE m.symbol = f.symbol
                                       AND m.current_quantity <> 0))
            FROM fills f
            JOIN strategies st ON st.id = f.strategy_id
            WHERE st.code = 'schwab_1m_v2' AND f.filled_at::date = CURRENT_DATE
        """), {"grace": EXIT_GRACE_MIN}).one()

        still_open = sess.execute(text("""
            SELECT count(*) FROM oms_managed_positions WHERE current_quantity <> 0
        """)).scalar_one()

    notes.append(f"fills today buy={buys} sell={sells} · open managed rows={still_open}")

    # --- data integrity: the $0 cancelled-sibling artefact must NEVER be booked
    if zero_priced:
        reds.append(f"{zero_priced} SELL fill(s) booked at price<=0 — the $0 cancelled-leg trap; "
                    f"these read as -100% trades. Disable oms_record_native_oco_exit_fills_enabled.")

    # --- capture working? entries that closed long ago with no exit recorded
    if capture_on and unpaired:
        reds.append(
            f"{unpaired} entry fill(s) older than {EXIT_GRACE_MIN}min closed with NO exit fill "
            f"recorded - the OCO exit capture is not working; P&L stays blank. "
            f"KNOWN BENIGN CAUSE: a position closed BY HAND on Webull leaves no OCO child leg to "
            f"read, so operator manual closes show up here too - check before rolling back."
        )
    if capture_on and buys and sells:
        notes.append(f"exit capture LIVE: {sells} sell fill(s) recorded (was 0 all week)")

    # --- realign working?
    realign_ok = log_count(r"bracket realigned to fill")
    realign_bad = log_count(r"bracket realign failed")
    if realign_on and realign_bad:
        ambers.append(f"{realign_bad}x 'bracket realign failed' — the broker likely rejects a "
                      f"PARTIAL combo replace (the CONFIRM-AT-TEST case). Positions are STILL "
                      f"PROTECTED by the original bracket; turn the flag off and rework the shape.")
    if realign_on and realign_ok:
        notes.append(f"realign fired {realign_ok}x")

    if selftest:
        push("OCO capture - SELFTEST", "pager path OK; no fault implied.", "low", "white_check_mark")
        print("selftest push sent")
        return 0

    stamp = datetime.now(ET).strftime("%H:%M ET")
    if reds:
        push(f"OCO capture RED - {stamp}", "\n".join(reds + notes), "urgent", "rotating_light")
        print("RED:", " | ".join(reds))
        return 2
    if ambers:
        push(f"OCO capture AMBER - {stamp}", "\n".join(ambers + notes), "high", "warning")
        print("AMBER:", " | ".join(ambers))
        return 1
    print("GREEN:", " | ".join(notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
