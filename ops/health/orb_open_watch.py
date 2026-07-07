#!/usr/bin/env python3
"""ORB 09:30 open watcher — the LOAD-DEPENDENT unknowns only.

Everything checkable pre-open is in preopen_readiness_check.py. This watches the
two things that can only be known under the real open burst:

  #387 (consume-loop lag): does ORB keep up with the open-burst tick rate? We
        measure (a) the burst rate off mai_tai:market-data `entries-added`, and
        (b) ORB's last finalized bar (last_bar_at) vs wall-clock. If #387 holds,
        last_bar_at tracks the current minute (lag < ~90s = <=1 bar behind). The
        OLD bug left the 09:30 bar surfacing ~1:47 late.
  #388 (entry behaviour): [ORB-ENTRY-FILLED]/[ORB-ENTRY-RESET], the 2-attempt
        cap, no phantom suppression — grepped from orb.log/oms.log post-window.

Usage: python3 orb_open_watch.py [end_hhmm_et]   (default 0933)
Run it at ~09:29:50 ET. Prints live samples, then a marker trace + summary.
"""
import subprocess, json, time, sys
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))
UTC = timezone.utc
END_HHMM = sys.argv[1] if len(sys.argv) > 1 else "0933"

def sh(c):
    return subprocess.run(c, shell=True, capture_output=True, text=True).stdout

def now():
    return datetime.now(UTC)

def et(dt):
    return dt.astimezone(ET)

def orb_state():
    raw = sh("redis-cli XREVRANGE mai_tai:strategy-state-isolated + - COUNT 20")
    for ln in raw.splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if d.get("payload", {}).get("strategy_code") == "orb":
                return d
    return None

def md_entries_added():
    raw = sh("redis-cli XINFO STREAM mai_tai:market-data")
    toks = raw.split()
    for i, t in enumerate(toks):
        if t == "entries-added" and i + 1 < len(toks):
            try:
                return int(toks[i + 1])
            except Exception:
                return None
    # fallback: XLEN
    try:
        return int(sh("redis-cli XLEN mai_tai:market-data").strip())
    except Exception:
        return None

def parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

end_dt = None
def compute_end():
    n = et(now())
    e = n.replace(hour=int(END_HHMM[:2]), minute=int(END_HHMM[2:]), second=0, microsecond=0)
    return e.astimezone(UTC)

end_dt = compute_end()
start = now()
print("=" * 72)
print(f"ORB OPEN WATCHER — start {et(start):%H:%M:%S ET}  → until {et(end_dt):%H:%M:%S ET}")
print("Watching: #387 burst-rate + ORB bar-finalize lag  |  #388 entry markers")
print("=" * 72)
print(f"{'wall(ET)':10} {'burst/s':>8} {'orbHB':>6} {'lastBar(ET)':>12} {'barLag':>7} {'bar_counts'}")

samples = []
prev_added = md_entries_added()
prev_t = time.time()
peak_rate = 0.0
max_barlag = 0.0
first_bar_seen_at = None   # wall-clock when last_bar_at first became non-empty
first_bar_ts = None

while now() < end_dt:
    time.sleep(2)
    t = time.time()
    added = md_entries_added()
    rate = None
    if added is not None and prev_added is not None and t > prev_t:
        rate = (added - prev_added) / (t - prev_t)
        peak_rate = max(peak_rate, rate)
    prev_added, prev_t = added, t

    st = orb_state()
    wall = now()
    hb_age = None
    barlag = None
    lastbar_et = "-"
    bc = {}
    if st:
        pa = parse_iso(st["produced_at"])
        if pa:
            hb_age = (wall - pa).total_seconds()
        p = st["payload"]
        bc = p.get("bar_counts", {})
        lta = p.get("last_tick_at", {})   # = last_bar_at per symbol
        lags = []
        latest_bar = None
        for sym, s in lta.items():
            bt = parse_iso(s)
            if bt:
                lags.append((wall - bt).total_seconds())
                if latest_bar is None or bt > latest_bar:
                    latest_bar = bt
        if lags:
            barlag = min(lags)   # freshest bar's age = how current ORB is
            max_barlag = max(max_barlag, barlag)
            if latest_bar:
                lastbar_et = f"{et(latest_bar):%H:%M:%S}"
            if first_bar_seen_at is None:
                first_bar_seen_at = wall
                first_bar_ts = latest_bar
    line = (f"{et(wall):%H:%M:%S} {('%.0f'%rate) if rate is not None else '-':>8} "
            f"{('%.0f'%hb_age) if hb_age is not None else '-':>6} "
            f"{lastbar_et:>12} {('%.0fs'%barlag) if barlag is not None else '-':>7} {bc}")
    print(line, flush=True)
    samples.append((wall, rate, barlag))

# ---- post-window marker trace (#388 + entry-dependent #387) ----
print("\n" + "-" * 72)
print("MARKER TRACE (orb.log + oms.log, this session's open window):")
markers = "ORB-RH-ENTRY|ORB-BREAKOUT|ORB-OPEN|ORB-ENTRY-FILLED|ORB-ENTRY-RESET|ORB-RECLAIM|OMS-ORB-QUOTE-PRICED|OMS-ABANDON-INTENT|HARD-STOP"
raw = sh(f"sudo grep -hE '{markers}' /var/log/project-mai-tai/orb.log /var/log/project-mai-tai/oms.log 2>/dev/null "
         f"| grep -viE 'polygon_30s|schwab_1m_v2|macd_30s'")   # ORB-scoped only
lo, hi = start - timedelta(minutes=2), end_dt + timedelta(minutes=2)
kept = []
for ln in raw.splitlines():
    try:
        ts = datetime.strptime(ln[:23].replace(",", "."), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    except Exception:
        continue
    if lo <= ts <= hi:
        kept.append(ln)
print("\n".join(kept[-40:]) if kept else
      "  (no ORB entry/breakout/abandon markers in window — no qualifying breakout; #388 carries to next entry day)")

# ---- summary ----
print("\n" + "=" * 72)
print("SUMMARY")
print(f"  peak burst rate      : {peak_rate:.0f} ticks/s  (old bug fell behind above ~200/s)")
print(f"  ORB max bar lag      : {max_barlag:.0f}s  (<=~90s = keeping up / <=1 bar behind)")
if first_bar_seen_at and first_bar_ts:
    print(f"  first bar surfaced   : bar {et(first_bar_ts):%H:%M:%S} seen at wall {et(first_bar_seen_at):%H:%M:%S ET}")
if first_bar_seen_at is None:
    v387 = "INCONCLUSIVE — ORB built NO bars in window (pre-09:25 anchor, or genuinely light open)"
elif max_barlag > 125:
    v387 = f"REVIEW — freshest-bar age hit {max_barlag:.0f}s (>~2 intervals behind → possible #387 lag)"
else:
    v387 = f"GREEN — ORB tracked the burst (freshest-bar age stayed <=125s, peak {peak_rate:.0f}/s)"
print("  #387 verdict         : " + v387)
print("  #387 eyeball         : lastBar(ET) should advance each minute, staying within ~1 min of wall(ET)")
print("  #388 verdict         : see marker trace above (FILLED/RESET, 2-attempt cap, no phantom)")
print("=" * 72)
