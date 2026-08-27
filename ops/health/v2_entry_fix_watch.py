#!/usr/bin/env python3
"""
V2 ENTRY-FIX WATCH -- validates the 2026-08-03 fixes (#644) against LIVE tape.

Four questions, one run (operator's list, 2026-08-04):
  A. Does a live cross read a LEGAL COMPOSITION (<=1 resting AND <=1 reclaim), never 3?
  B. Reactive-first was removed -- are we now DROPPING real trades on no-resting crosses?
  C. Has [OMS-V2-POLL-REENROLL] fired?  Presence AND frequency both matter.
  D. Is the tape lighter overall (expected, NOT a fault)?

SCOPE: real money only -- broker accounts live:schwab_1m_v2 (Schwab, the cap applies here)
and live:orb (the Webull FAN-OUT leg, a mirror -- reported, never counted toward the cap).
paper:polygon_30s is SIM and is excluded by construction; #645 shipped because SIM trades
reached a real-money pager once already.

WHAT GREEN MEANS: every armed cross in the window produced at most one FIRST-slot and at most
one RECLAIM-slot Schwab entry, and every counted fill carried explicit economic-slot evidence.
Order style is deliberately not used: a reclaim can itself be a resting STOP_LIMIT. GREEN does
not mean the entries were profitable, and it does not mean fix 2 is validated -- see section C.

Segment identity: an ARM..DISARM window from the v2 log. NOT cw_flip_level (repeats across
segments). NOT cw_arm_bar_ts from the DB either -- the RESTING path writes it as 0, so DB-only
grouping silently merges every resting entry of the day into one bucket (found 2026-08-04).
Attribution is by FILL TIME, never submitted_at -- a resting order is placed minutes before
it fills (the correction p0a_watch still owes).

HISTORICAL LIMIT (measured 2026-08-27): from the watch's first retained session, 2026-08-04,
through 2026-08-27, cw_entry_slot coverage is 0/239 Schwab BUY fills and 0/301 Webull BUY fills.
Every composition verdict in that interval is therefore COULD_NOT_TELL, including the ONFO and
VWAV alerts on 2026-08-14. Their tape has the same pre-arm/post-arm rested shape as CELU, but the
economic slot was never recorded, so neither CLEAN nor BREACH can be recovered honestly.
"""
import argparse
import glob
import gzip
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
V2_LOG_GLOB = "/var/log/project-mai-tai/schwab-1m-v2.log*"
OMS_LOG_GLOB = "/var/log/project-mai-tai/oms.log*"
ENV = "/etc/project-mai-tai/project-mai-tai.env"
REAL_ACCTS = ("live:schwab_1m_v2", "live:orb")
CAP_ACCT = "live:schwab_1m_v2"
VALID_ENTRY_SLOTS = ("first", "reclaim")
# Widest observed arm lag on 2026-08-03 was 706s (fill intrabar -> arm at bar close). 15 min
# covers it with margin; anything older is a genuinely unattached fill and stays an orphan.
PRE_ARM_ADOPT = timedelta(minutes=15)
ENTRY_OPEN_MIN = 7 * 60        # 07:00 ET -- the EH entry window opens
ENTRY_CLOSE_MIN = 18 * 60      # 18:00 ET -- entries capped (v2 trading window)
# KUST's ladder cancelled its exit every ~30s (09:26:09, 09:27:04, 09:27:32, 09:28:02 ...), so an
# exit that survives 30s has demonstrably sat through a refresh tick it would previously have died
# on. Below that the hold never had to engage and a fill proves nothing.
COUNTER_ORDER_WINDOW_SECS = 120.0   # a real entry writes its order within seconds of the increment
REST_THROUGH_SECS = 30.0
# An arm whose driving bar is older than this is a WARMUP REPLAY of a historical flip, not a live
# cross. Mirrors the bot's own `_reactive_max_bar_age_ms` guard on the EH reactive path.
# ⛔⭐⭐ LOAD-BEARING SIDE EFFECT — DO NOT TUNE OR REMOVE WITHOUT READING THIS (2026-08-05).
# Its ORIGINAL purpose: keep warmup-replay arms out of the CROSS DENOMINATOR (they inflated it
# ~14x). It has since acquired a SECOND, UNDOCUMENTED job that the composition verdict depends on:
#
#   `_apply_session_anchor_reset` (strategy schwab_1m_v2.py) clears `cw_armed` with NO
#   [V2-CW-DISARM] LINE. It fires whenever a bar's 04:00-ET anchor differs from the stored one —
#   i.e. on the FIRST LIVE BAR after a multi-day replay, not only at 04:00. So a replay-armed
#   segment leaves a DANGLING ARM in the log with no disarm ever following it.
#
# Anything pairing ARM->DISARM therefore reads a dangling ARM as "still armed" and extends the
# segment to the next ARM. This filter removes those arms BEFORE pairing, which is the only reason
# section A's segment boundaries are correct and the 08-04 "0 breaches" verdict holds.
# ⇒ Raising this threshold (or deleting it as "just a denominator fix") silently corrupts every
# entries-per-cross number in this file, with NO error and NO visible symptom.
# Proven live: `preflight_v2_restart.sh` pairs ARM/DISARM with NO age filter and over-reported
# CLRO/PAVS/ZCMD as armed on 2026-08-05 — dangling replay arms, every one.
LIVE_ARM_MAX_AGE_SECS = 300.0

ARM_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*\[V2-CW-ARM\] (?P<sym>\S+) armed "
    r"bar_ts=(?P<bar>\d+) trig=(?P<trig>[\d.]+) flip_level=(?P<flip>[\d.]+)")
DISARM_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*\[V2-CW-DISARM\] (?P<sym>\S+) reason=(?P<why>\S+)")


def dsn():
    out = subprocess.run(["grep", "-E", "^MAI_TAI_DATABASE_URL=", ENV],
                         capture_output=True, text=True, check=True).stdout.strip()
    url = out.split("=", 1)[1]
    m = re.match(r"^[^:]+://([^:]+):([^@]+)@", url)
    return "dbname=project_mai_tai user=%s host=localhost" % m.group(1), m.group(2)


def q(sql, params=()):
    import psycopg
    d, pw = dsn()
    with psycopg.connect(d, password=pw) as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def logs_in_window(pattern, start_utc):
    """Only files that could contain the window -- keeps the 80MB July gz out of a 5-min cron."""
    keep = []
    for p in glob.glob(pattern):
        try:
            mt = datetime.fromtimestamp(os.path.getmtime(p), timezone.utc)
        except OSError:
            continue
        if mt >= start_utc - timedelta(hours=6):
            keep.append(p)
    return sorted(keep)


def read_lines(path):
    op = gzip.open if path.endswith(".gz") else open
    with op(path, "rt", errors="replace") as fh:
        for line in fh:
            yield line


def parse_segments(start_utc, end_utc):
    arms, disarms = [], []
    replay_skipped = 0
    for p in logs_in_window(V2_LOG_GLOB, start_utc):
        for line in read_lines(p):
            if "[V2-CW-ARM]" in line:
                m = ARM_RE.match(line)
                if m:
                    t = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if start_utc <= t < end_utc:
                        # ⛔⭐ WARMUP-REPLAY ARMS ARE NOT CROSSES (found 2026-08-04).
                        # Warmup replays a historical bar series and emits one [V2-CW-ARM] per
                        # HISTORICAL flip, all within the same wall-clock second — AAOG fired 68
                        # arms at 10:23:00, driving bars up to 55 DAYS old. Counting those as live
                        # crosses inflated the denominator ~14x (98 arms -> 7 real) and poisoned the
                        # no-entry median with segments that were never live opportunities.
                        # `arm_bar_ts` age is NOT staleness in general (replay makes it legitimately
                        # old) -- but for "was this a live cross?" the driving bar MUST be recent.
                        bar_t = datetime.fromtimestamp(int(m.group("bar")) / 1000, timezone.utc)
                        if abs((t - bar_t).total_seconds()) > LIVE_ARM_MAX_AGE_SECS:
                            replay_skipped += 1
                            continue
                        arms.append((t, m.group("sym"), int(m.group("bar")),
                                     float(m.group("trig")), float(m.group("flip"))))
            elif "[V2-CW-DISARM]" in line:
                m = DISARM_RE.match(line)
                if m:
                    t = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if start_utc <= t < end_utc:
                        disarms.append((t, m.group("sym"), m.group("why")))
    arms.sort()
    disarms.sort()
    segs = []
    for i, (t, sym, bar, trig, flip) in enumerate(arms):
        nxt = [d[0] for d in disarms if d[1] == sym and d[0] > t]
        nxt += [x[0] for x in arms[i + 1:] if x[1] == sym]
        # Entries are capped 07:00-18:00 ET. A cross that arms outside that window was never
        # enterable, so counting it as a "dropped trade" would manufacture a scary number out
        # of overnight bars -- 84 of them before the open on 2026-08-04.
        et_min = t.astimezone(ET).hour * 60 + t.astimezone(ET).minute
        segs.append({"sym": sym, "start": t, "end": min(nxt) if nxt else end_utc,
                     "bar_ts": bar, "trig": trig, "flip": flip, "fills": [],
                     "enterable": ENTRY_OPEN_MIN <= et_min < ENTRY_CLOSE_MIN})
    parse_segments.replay_skipped = replay_skipped
    return segs


def entry_fills(start_utc, end_utc):
    rows = q("""
        SELECT f.symbol, f.filled_at, f.price, ba.name,
               COALESCE(ti.payload->'metadata'->>'resting_entry','false'),
               COALESCE(ti.payload->'metadata'->>'cw_entry_slot','')
        FROM fills f
        JOIN broker_orders bo ON bo.id = f.order_id
        JOIN broker_accounts ba ON ba.id = bo.broker_account_id
        JOIN strategies s ON s.id = bo.strategy_id
        LEFT JOIN trade_intents ti ON ti.id = bo.intent_id
        WHERE s.code = 'schwab_1m_v2' AND f.side = 'buy'
          AND ba.name = ANY(%s) AND f.filled_at >= %s AND f.filled_at < %s
        ORDER BY f.filled_at
    """, (list(REAL_ACCTS), start_utc, end_utc))
    return [{"sym": r[0], "at": r[1], "px": float(r[2]), "acct": r[3],
             "resting_style": str(r[4]).lower() == "true",
             "entry_slot": str(r[5]).strip().lower()} for r in rows]


def grade_composition(fills):
    """Grade one armed segment from explicit economic-slot evidence.

    Verdict precedence is deliberate: a known duplicate slot remains a BREACH even if another
    fill is unattributed; missing evidence can hide a breach but cannot erase one already proven.
    A segment with no Schwab fill is UNEXERCISED for the #644 cap.
    """
    cap = [f for f in fills if f["acct"] == CAP_ACCT]
    first = sum(1 for f in cap if f.get("entry_slot") == "first")
    reclaim = sum(1 for f in cap if f.get("entry_slot") == "reclaim")
    unknown = [f for f in fills if f.get("entry_slot") not in VALID_ENTRY_SLOTS]
    if first > 1 or reclaim > 1 or len(cap) >= 3:
        verdict = "BREACH"
    elif unknown:
        verdict = "COULD_NOT_TELL"
    elif not cap:
        verdict = "UNEXERCISED"
    else:
        verdict = "OK"
    return verdict, first, reclaim, len(unknown), len(cap)


def mfe_pct(sym, start_utc, end_utc, flip):
    if flip <= 0:
        return None
    r = q("""SELECT MAX(high_price) FROM strategy_bar_history
             WHERE strategy_code='schwab_1m_v2' AND symbol=%s
               AND bar_time >= %s AND bar_time < %s""", (sym, start_utc, end_utc))
    if not r or r[0][0] is None:
        return None
    return (float(r[0][0]) / flip - 1.0) * 100.0


def count_marker(pattern, marker, start_utc, end_utc):
    n, hits = 0, []
    for p in logs_in_window(pattern, start_utc):
        for line in read_lines(p):
            if marker in line:
                try:
                    t = datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if start_utc <= t < end_utc:
                    n += 1
                    if len(hits) < 5:
                        hits.append(line.rstrip()[:200])
    return n, hits


# ---------------------------------------------------------------- section F helpers
SEEDCAP_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*\[V2-CW-SEED-CAP\] (?P<sym>\S+) "
    r".*arm_bar_ts=(?P<bar>\d+), watch_start=(?P<ws>\d+), boot=(?P<boot>\d+)")
PROBE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*\[V2-CW-STATE-PROBE\] sym=(?P<sym>\S+) "
    r".*entries_this_flip=(?P<n>\d+)")


def scan_populations(start_utc, end_utc):
    """Every [V2-CW-ARM] split LIVE vs REPLAY, plus seed-caps and entry-counter samples.

    THREE POPULATIONS, NEVER POOLED. Section A deliberately drops replay arms so its denominator
    is real crosses -- correct for THAT job, and exactly why the #644 composition cap was
    UNVALIDATED on reconstructed segments rather than narrowly validated: this checker never
    contained the string 'reconstructed' at all. One pooled number hides the population the cap
    was never tested on.
    """
    live, replay, caps, probes = [], [], [], []
    for p in logs_in_window(V2_LOG_GLOB, start_utc):
        for line in read_lines(p):
            if "[V2-CW-ARM]" in line:
                m = ARM_RE.match(line)
                if not m:
                    continue
                t = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                if not (start_utc <= t < end_utc):
                    continue
                bar_t = datetime.fromtimestamp(int(m.group("bar")) / 1000, timezone.utc)
                age = abs((t - bar_t).total_seconds())
                (replay if age > LIVE_ARM_MAX_AGE_SECS else live).append(
                    (t, m.group("sym"), int(m.group("bar")), age))
            elif "[V2-CW-SEED-CAP]" in line:
                m = SEEDCAP_RE.match(line)
                if m:
                    t = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if start_utc <= t < end_utc:
                        caps.append((t, m.group("sym"), int(m.group("bar")), int(m.group("ws"))))
            elif "[V2-CW-STATE-PROBE]" in line:
                m = PROBE_RE.match(line)
                if m:
                    t = datetime.strptime(m.group("ts"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                    if start_utc <= t < end_utc:
                        probes.append((t, m.group("sym"), int(m.group("n"))))
    return live, replay, caps, probes


def counter_increment_events(probes):
    """Each entries_this_flip increment as (when, symbol, delta).

    Returns EVENTS, not per-symbol totals. A daily total is useless here: FUSE on 2026-08-03 took
    +3 from a DB-seed replay that emitted nothing, but it also had 6 genuine buys earlier that
    day, so a symbol-level total nets to "not inflated" and the canonical case goes undetected.
    A DECREASE is a reset (disarm / 04:00-ET session anchor) and is never counted."""
    last, events = {}, []
    for t, sym, n in sorted(probes):
        prev = last.get(sym)
        if prev is not None and n > prev:
            events.append((t, sym, n - prev))
        last[sym] = n
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(ET).strftime("%Y-%m-%d"), help="ET date, default today")
    a = ap.parse_args()
    d0 = datetime.strptime(a.day, "%Y-%m-%d").replace(tzinfo=ET)
    start_utc = d0.astimezone(timezone.utc)
    end_utc = (d0 + timedelta(days=1)).astimezone(timezone.utc)
    now_et = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    print("=== V2 ENTRY-FIX WATCH | day %s ET | run %s ===" % (a.day, now_et))
    print("scope: real money only (%s); cap counted on %s\n" % (", ".join(REAL_ACCTS), CAP_ACCT))
    verdicts = []

    try:
        segs = parse_segments(start_utc, end_utc)
        fills = entry_fills(start_utc, end_utc)
        for f in fills:
            placed = False
            for s in segs:
                if s["sym"] == f["sym"] and s["start"] <= f["at"] < s["end"]:
                    s["fills"].append(f)
                    placed = True
                    break
            if not placed:
                # A RESTING fill lands INTRABAR; its arm only confirms at the BAR CLOSE, measured
                # at 21s-706s later on 2026-08-03. That is defect 1 itself, so a naive
                # inside-the-window match drops the resting leg out of its own cross and a
                # 3-entry breach reads as 2. Adopt a pre-arm fill into the arm that follows it.
                cand = [s for s in segs
                        if s["sym"] == f["sym"] and f["at"] < s["start"]
                        and (s["start"] - f["at"]) <= PRE_ARM_ADOPT]
                if cand:
                    s = min(cand, key=lambda s: s["start"])
                    f["adopted"] = True
                    s["fills"].append(f)
                    placed = True
            if not placed:
                f["orphan"] = True

        # ---------- A. COMPOSITION ----------
        print("--- A. COMPOSITION PER CROSS (legal: <=1 first slot AND <=1 reclaim slot) ---")
        breaches = []
        uncertain = []
        live = [s for s in segs if s["fills"]]
        exercised = 0
        for s in live:
            status, first, reclaim, unknown, cap_total = grade_composition(s["fills"])
            exercised += int(cap_total > 0)
            fan = sum(1 for f in s["fills"] if f["acct"] == "live:orb")
            print("  [%s] %-6s arm %s ET  first=%d reclaim=%d unknown=%d total=%d "
                  "(webull leg %d)  flip=%.4f"
                  % (status, s["sym"], s["start"].astimezone(ET).strftime("%H:%M:%S"),
                     first, reclaim, unknown, cap_total, fan, s["flip"]))
            signature = "%s@%s f%d/r%d/u%d" % (
                s["sym"], s["start"].astimezone(ET).strftime("%H:%M"),
                first, reclaim, unknown,
            )
            if status == "BREACH":
                breaches.append(signature)
            elif status == "COULD_NOT_TELL":
                uncertain.append(signature)
        orph = [f for f in fills if f.get("orphan")]
        if orph:
            print("  NOTE %d real-money buy fill(s) matched NO armed cross (reported, not counted): %s"
                  % (len(orph), ", ".join("%s@%s" % (f["sym"], f["at"].astimezone(ET).strftime("%H:%M:%S"))
                                          for f in orph[:6])))
        if not live:
            print("  (no armed cross has produced a real-money entry yet today)")
        print("  crosses armed=%d (LIVE only; %d warmup-replay arms excluded)  crosses with entries=%d"
              % (len(segs), getattr(parse_segments, "replay_skipped", 0), len(live)))
        orphaned = [f for f in orph if f["acct"] == CAP_ACCT]
        if breaches:
            composition = "BREACH"
        elif uncertain or orphaned:
            composition = "COULD_NOT_TELL"
        elif exercised == 0:
            composition = "UNEXERCISED"
        else:
            composition = "OK"
        verdicts.append(
            "VERDICT composition=%s crosses=%d entered=%d exercised=%d breaches=%s unknown=%s"
            % (composition, len(segs), len(live), exercised, ";".join(breaches) or "-",
               ";".join(uncertain) or "-")
        )

        # ---------- B. DROPPED TRADES ----------
        print("\n--- B. ARMED CROSSES THAT PRODUCED NO ENTRY (reactive-first removal cost) ---")
        dropped = [s for s in segs if not s["fills"] and s["enterable"]]
        outside = sum(1 for s in segs if not s["fills"] and not s["enterable"])
        print("  (%d further armed crosses fell OUTSIDE the 07:00-18:00 ET entry window and were"
              " never enterable -- excluded)" % outside)
        judged = [(s, mfe_pct(s["sym"], s["start"], s["end"], s["flip"])) for s in dropped]
        judged.sort(key=lambda x: (x[1] is None, -(x[1] or 0)))
        for s, m in judged[:12]:
            mv = ("%+.2f%%" % m) if m is not None else "n/a"
            print("  %-6s arm %s-%s ET  max-high vs flip %s"
                  % (s["sym"], s["start"].astimezone(ET).strftime("%H:%M:%S"),
                     s["end"].astimezone(ET).strftime("%H:%M:%S"), mv))
        if not dropped:
            enterable = sum(1 for s in segs if s["enterable"])
            if enterable:
                print("  (none -- every enterable armed cross produced an entry)")
            else:
                # "none dropped" out of nothing enterable is NOT a result. Saying it the other
                # way round reads as a pass mark for a window that has not happened yet.
                print("  (nothing to judge yet -- no cross has armed inside the entry window today)")
        withmfe = sorted(m for _, m in judged if m is not None)
        med = withmfe[len(withmfe) // 2] if withmfe else None
        print("  no-entry crosses=%d  median max-high vs flip=%s"
              % (len(dropped), ("%+.2f%%" % med) if med is not None else "n/a"))
        print("  NOTE judgement call, NOT a fault: max-high is the BEST case, it ignores the exit.")
        verdicts.append("VERDICT dropped=%d median_mfe_pct=%s"
                        % (len(dropped), ("%.2f" % med) if med is not None else "na"))

        # ---------- C. POLL-REENROLL ----------
        print("\n--- C. [OMS-V2-POLL-REENROLL] (fix 2's real path) ---")
        n, hits = count_marker(OMS_LOG_GLOB, "[OMS-V2-POLL-REENROLL]", start_utc, end_utc)
        state = "NONE" if n == 0 else ("PROVEN" if n <= 2 else "REPEATED")
        print("  fired %dx today -> %s" % (n, state))
        for h in hits:
            print("    %s" % h)
        print("  0 = mechanism UNEXERCISED (not a fault, but not validated either).")
        print("  1-2 = the path works. 3+ = the underlying leak is LIVE and self-healing masks it.")
        verdicts.append("VERDICT reenroll=%s count=%d" % (state, n))

        # ---------- D. TAPE WEIGHT ----------
        print("\n--- D. TAPE WEIGHT vs prior sessions (lighter is EXPECTED, not a fault) ---")
        rows = q("""
            SELECT (f.filled_at AT TIME ZONE 'America/New_York')::date AS d, COUNT(*)
            FROM fills f
            JOIN broker_orders bo ON bo.id=f.order_id
            JOIN broker_accounts ba ON ba.id=bo.broker_account_id
            JOIN strategies s ON s.id=bo.strategy_id
            WHERE s.code='schwab_1m_v2' AND f.side='buy' AND ba.name=%s
              AND f.filled_at >= %s - INTERVAL '10 days' AND f.filled_at < %s
            GROUP BY 1 ORDER BY 1 DESC
        """, (CAP_ACCT, start_utc, end_utc))
        today_n = sum(1 for f in fills if f["acct"] == CAP_ACCT)
        prior = [int(c) for dd, c in rows if dd.strftime("%Y-%m-%d") != a.day][:5]
        pmed = sorted(prior)[len(prior) // 2] if prior else None
        print("  today Schwab entries=%d   prior sessions=%s   median=%s" % (today_n, prior, pmed))
        verdicts.append("VERDICT tape today=%d prior_median=%s"
                        % (today_n, pmed if pmed is not None else "na"))

        # ---------- E. P0a MARKETABLE-HOLD (EH software-ladder exits) ----------
        print("\n--- E. P0a MARKETABLE-HOLD on the EH software ladder ---")
        rows = q("""
            SELECT bo.symbol, bo.submitted_at, bo.updated_at, bo.status,
                   bo.payload->>'limit_price', bo.payload->>'session', bo.payload->>'reason'
            FROM broker_orders bo
            JOIN broker_accounts ba ON ba.id = bo.broker_account_id
            WHERE ba.name = %s AND bo.side = 'sell'
              AND bo.payload->>'oms_v2_managed_exit' = 'true'
              AND lower(bo.order_type) = 'limit'
              AND COALESCE(bo.payload->>'session','') IN ('AM','PM')
              AND bo.submitted_at >= %s AND bo.submitted_at < %s
            ORDER BY bo.symbol, bo.submitted_at
        """, (CAP_ACCT, start_utc, end_utc))
        by_sym = {}
        for sym, sub, upd, status, lim, sess, reason in rows:
            try:
                limf = float(lim) if lim else 0.0
            except (TypeError, ValueError):
                limf = 0.0
            by_sym.setdefault(sym, []).append(
                {"sub": sub, "upd": upd, "status": (status or "").lower(),
                 "lim": limf, "reason": (reason or "")})
        churn, validated, inconclusive = [], [], []
        for sym, os_ in by_sym.items():
            for i, o in enumerate(os_):
                life = (o["upd"] - o["sub"]).total_seconds() if o["upd"] and o["sub"] else 0.0
                if o["status"] == "filled":
                    # RESTED THROUGH A REFRESH THEN FILLED = the P0a hold did its job.
                    if life >= REST_THROUGH_SECS:
                        validated.append((sym, o, life))
                    else:
                        # A 41ms fill proves nothing -- the hold never had to engage.
                        inconclusive.append((sym, o, life, "fastfill"))
                    continue
                if o["status"] == "cancelled" and i + 1 < len(os_):
                    nxt = os_[i + 1]
                    gap = (nxt["sub"] - o["sub"]).total_seconds()
                    if gap <= 90 and o["lim"] > 0 and nxt["lim"] > 0:
                        if nxt["lim"] >= o["lim"]:
                            # Replaced at an EQUAL-OR-HIGHER limit: nothing had moved against
                            # us, so there was no reason to take it off the book. THE KUST
                            # SIGNATURE, and it needs no bid tape to identify.
                            churn.append((sym, o, nxt, gap))
                        else:
                            # Repriced DOWN = the bid fell through the limit and the hold
                            # correctly released. Working as designed, but not a P0a pass.
                            inconclusive.append((sym, o, life, "gap-through"))
                elif o["status"] == "cancelled" and "manual" in o["reason"].lower():
                    inconclusive.append((sym, o, life, "hand-cancel (not a failure)"))
        for sym, o, nxt, gap in churn[:8]:
            print("  [CHURN] %-6s cancelled %s (limit %.4f) -> replaced %.0fs later at %.4f "
                  "(equal-or-higher: nothing moved against us)"
                  % (sym, o["sub"].astimezone(ET).strftime("%H:%M:%S"), o["lim"], gap, nxt["lim"]))
        for sym, o, life in validated[:6]:
            print("  [P0a VALIDATED] %-6s EH exit rested %.0fs through a refresh, then FILLED at %s"
                  % (sym, life, o["sub"].astimezone(ET).strftime("%H:%M:%S")))
        for sym, o, life, why in inconclusive[:6]:
            print("  [inconclusive] %-6s %s (%.0fs) -- %s" % (sym, o["sub"].astimezone(ET).strftime("%H:%M:%S"), life, why))
        if not rows:
            print("  (no EH software-ladder managed exit today -- nothing to judge)")
        state = ("FAILURE" if churn else ("VALIDATED" if validated else
                 ("INCONCLUSIVE" if inconclusive else "NONE")))
        print("  EH managed exits=%d  churn=%d  rested-then-filled=%d  inconclusive=%d -> %s"
              % (len(rows), len(churn), len(validated), len(inconclusive), state))
        print("  VALIDATED needs a rest-through-a-refresh THEN a fill. A fast fill or a")
        print("  gap-through is INCONCLUSIVE, never a pass. A hand-cancel is not a failure.")
        verdicts.append("VERDICT p0a=%s eh_exits=%d churn=%d rested_filled=%d"
                        % (state, len(rows), len(churn), len(validated)))

        # ---------- F. SEGMENT POPULATIONS (never pooled) ----------
        print()
        print("--- F. SEGMENT POPULATIONS -- live arms / replay arms / reconstructed ---")
        f_live, f_replay, f_caps, f_probes = scan_populations(start_utc, end_utc)
        print("  LIVE arms          : %d   (bar age <= %.0fs -- real crosses; section A's set)"
              % (len(f_live), LIVE_ARM_MAX_AGE_SECS))
        print("  REPLAY arms        : %d   (warmup / DB-seed replay of historical flips)"
              % len(f_replay))
        print("  RECONSTRUCTED caps : %d   ([V2-CW-SEED-CAP]: the flip predates our watch)"
              % len(f_caps))
        for t, sym, bar, ws in f_caps[:8]:
            print("     %-6s capped %s ET  arm bar %.0f min BEFORE watch-start"
                  % (sym, t.astimezone(ET).strftime("%H:%M:%S"), (ws - bar) / 60000.0))
        if f_replay:
            byslot = {}
            for _t, sym, _b, _a in f_replay:
                byslot[sym] = byslot.get(sym, 0) + 1
            print("     replay arms by symbol: %s"
                  % ", ".join("%s=%d" % kv for kv in sorted(byslot.items(), key=lambda kv: -kv[1])[:5]))
        # THE FUSE CASE. cw_entries_this_flip is a LABEL, not the cap (#644 is composition).
        # On 2026-08-03 a DB-seed replay drove FUSE 0->2->3 while emitting NO ORDER, and because
        # capped/dangerous derive from that counter the segment read "capped=true dangerous=false"
        # -- safe BY ACCIDENT. Count increments against orders actually submitted.
        inc_events = counter_increment_events(f_probes)
        ords = q("""SELECT bo.symbol, bo.submitted_at FROM broker_orders bo
                    JOIN broker_accounts ba ON ba.id=bo.broker_account_id
                    WHERE bo.side='buy' AND ba.name = ANY(%s)
                      AND bo.submitted_at >= %s AND bo.submitted_at < %s""",
                 (list(REAL_ACCTS), start_utc - timedelta(minutes=10), end_utc))
        by_sym = {}
        for sym_, at_ in ords:
            by_sym.setdefault(sym_, []).append(at_ if at_.tzinfo else at_.replace(tzinfo=timezone.utc))
        phantom = [(t, sym, d) for (t, sym, d) in inc_events
                   if not any(abs((o - t).total_seconds()) <= COUNTER_ORDER_WINDOW_SECS
                              for o in by_sym.get(sym, []))]
        print("  ENTRY-COUNTER INFLATION (the FUSE case)")
        print("     increments=%d   of which NO buy order within +/-%.0fs = %d PHANTOM"
              % (len(inc_events), COUNTER_ORDER_WINDOW_SECS, len(phantom)))
        for t, sym, d in phantom[:8]:
            print("     %-6s %s ET  counter +%d  with NO order submitted nearby"
                  % (sym, t.astimezone(ET).strftime("%H:%M:%S"), d))
        print("  WHAT F CANNOT SEE:")
        print("     - whether a reconstructed segment ever ENTERED: F reports the cap EVENT, not")
        print("       an outcome. seed_caps>0 is not by itself a loss.")
        print("     - replay arms in a rotated-away log, and segments armed before this window.")
        print("     - an increment that rises AND resets between two STATE-PROBE samples =>")
        print("       PHANTOM COUNT IS A FLOOR, NEVER A CEILING.")
        print("     - an unrelated buy within +/-%.0fs masks a phantom increment (false negative);"
              % COUNTER_ORDER_WINDOW_SECS)
        print("       the window is deliberately generous, so 0 phantom is weak evidence of clean.")
        print("     - it does NOT prove the #644 composition cap held on reconstructed segments;")
        print("       it makes that population COUNTABLE. Section A still measures composition on")
        print("       LIVE crosses only.")
        verdicts.append("VERDICT populations live_arms=%d replay_arms=%d seed_caps=%d "
                        "phantom_increments=%d"
                        % (len(f_live), len(f_replay), len(f_caps), len(phantom)))

    except Exception as exc:                      # never a silent clean
        import traceback
        traceback.print_exc(file=sys.stderr)
        print("\nVERDICT error=SECTION-FAILED detail=%s:%s" % (type(exc).__name__, exc))
        print("\n".join(verdicts))
        return 2

    print()
    print("\n".join(verdicts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
