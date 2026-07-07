#!/usr/bin/env python3
"""Pre-open readiness check for the project-mai-tai fleet.

Run ~09:00-09:15 ET (attended). Surfaces EVERYTHING checkable before the 09:30
open so the open holds only genuinely load-dependent unknowns (#387 burst lag,
#388 entry behaviour). Single green/red verdict.

Sections: (1) Schwab token SPOF  (2) services  (3) heartbeats/zombie
(4) bar flow (v2 + ORB, time-aware)  (5) watchlists + protected config
(6) data_health.  Run as `trader` (calls sudo internally for root logs/env).
"""
import subprocess, json, time, sys
from datetime import datetime, timezone, timedelta

ET = timezone(timedelta(hours=-4))          # EDT (summer). July 2026 = EDT.
UTC = timezone.utc
now = datetime.now(UTC)
now_et = now.astimezone(ET)

FAIL = 0
WARN = 0

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

def ok(msg):   print(f"  [ OK ] {msg}")
def warn(msg):
    global WARN; WARN += 1; print(f"  [WARN] {msg}")
def bad(msg):
    global FAIL; FAIL += 1; print(f"  [FAIL] {msg}")
def info(msg): print(f"  [info] {msg}")

def iso_age(s):
    """Age in seconds of a UTC ISO string like 2026-07-01T11:16:57.569591Z."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return (now - dt).total_seconds()

def parse_et(s):
    """'2026-07-01 07:17:17 AM ET' -> aware datetime."""
    s = s.replace(" ET", "").strip()
    return datetime.strptime(s, "%Y-%m-%d %I:%M:%S %p").replace(tzinfo=ET)

def parse_log_ts(line):
    """Leading '2026-07-01 08:25:27,396 ...' -> aware UTC-naive dt (logs are UTC)."""
    try:
        stamp = line[:23].replace(",", ".")
        return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)
    except Exception:
        return None

# --- shared redis pulls ---
def redis(cmd):
    return sh(f"redis-cli {cmd}")

def latest_by(stream, key_path, count=60):
    """Return dict of {key: payload_dict} newest-first, first seen wins."""
    raw = redis(f"XREVRANGE {stream} + - COUNT {count}")
    out = {}
    for ln in raw.splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            d = json.loads(ln)
        except Exception:
            continue
        cur = d
        for k in key_path:
            cur = cur.get(k, {}) if isinstance(cur, dict) else {}
        if cur and cur not in out:
            out[cur] = d
    return out

print("=" * 66)
print(f"PRE-OPEN READINESS CHECK   {now_et:%Y-%m-%d %H:%M:%S} ET  ({now:%H:%M} UTC)")
open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
mto = (open_et - now_et).total_seconds() / 60
anchor_passed = now_et.hour * 60 + now_et.minute >= 9 * 60 + 25   # 09:25 ET
print(f"Minutes to 09:30 open: {mto:.0f}    ORB 09:25 anchor passed: {anchor_passed}")
print("=" * 66)

# ============================================================ (1) SCHWAB TOKEN
print("\n(1) SCHWAB TOKEN  — recurring weekly SPOF (last death 2026-06-25)")
mt = sh("sudo stat -c %Y /var/lib/macd-webhook-server/data/schwab_tokens.json").strip()
if mt.isdigit():
    age = time.time() - int(mt)
    m = f"token-store mtime age {age/60:.1f} min"
    (ok if age < 2000 else warn if age < 2700 else bad)(
        m if age < 2000 else m + "  (refresh lagging?)" if age < 2700 else m + "  (STALE)")
else:
    bad("cannot stat token store")
last = sh("sudo grep 'SCHWAB-TOKEN-REFRESHED' /var/log/project-mai-tai/control.log | tail -1").strip()
if last:
    lt = parse_log_ts(last)
    exp = ""
    if "expires_at=" in last:
        exp = last.split("expires_at=")[1].split()[0]
    if lt:
        info(f"last refresh {lt.astimezone(ET):%H:%M:%S ET} ({(now-lt).total_seconds()/60:.1f} min ago)")
    if exp:
        try:
            ea = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            info(f"next token expiry / refresh due ~{ea.astimezone(ET):%H:%M:%S ET} "
                 f"(in {(ea-now).total_seconds()/60:.0f} min)")
            if ea < now:
                bad("token already EXPIRED — refresher not keeping up")
        except Exception:
            pass
else:
    warn("no SCHWAB-TOKEN-REFRESHED line found")
# degraded / invalid_grant in the recent window (last ~35 min = actionable)
deg = sh("sudo grep -hE 'SCHWAB-TOKEN-REFRESHER-DEGRADED-PERSISTENT|invalid_grant|refresh_token' "
         "/var/log/project-mai-tai/control.log /var/log/project-mai-tai/schwab-1m-v2.log 2>/dev/null | tail -1").strip()
recent_deg = False
if deg:
    dt = parse_log_ts(deg)
    if dt and (now - dt).total_seconds() < 2100:
        recent_deg = True
if recent_deg:
    bad(f"token DEGRADED/invalid_grant in last 35 min: {deg[:90]}")
else:
    ok("no token degradation/invalid_grant in the recent window")

# ============================================================ (2) SERVICES
print("\n(2) SERVICES  — systemd active state")
core = ["strategy", "oms", "market-data", "control", "reconciler", "schwab-1m-v2", "orb"]
aux = ["market-capture", "trade-coach"]
for svc in core:
    st = sh(f"systemctl is-active project-mai-tai-{svc}.service").strip()
    (ok if st == "active" else bad)(f"{svc:14} {st}")
for svc in aux:
    st = sh(f"systemctl is-active project-mai-tai-{svc}.service").strip()
    (info if st == "active" else warn)(f"{svc:14} {st}  (auxiliary)")

# ============================================================ (3) HEARTBEATS
print("\n(3) HEARTBEATS  — zombie check (active service must still beat)")
hbs = latest_by("mai_tai:heartbeats", ["source_service"])
expect_hb = ["strategy-engine", "oms-risk", "market-data-gateway", "reconciler", "schwab-1m-v2"]
for svc in expect_hb:
    d = hbs.get(svc)
    if not d:
        bad(f"{svc:22} NO heartbeat (zombie signature)")
        continue
    age = iso_age(d["produced_at"])
    status = d.get("payload", {}).get("status", "?")
    m = f"{svc:22} {age:.0f}s ago  status={status}"
    if age > 240:
        bad(m + "  (STALE — zombie?)")
    elif age > 90:
        warn(m)
    elif status not in ("healthy", "degraded"):
        warn(m)
    else:
        ok(m + ("  (degraded=benign CYN/CANF)" if status == "degraded" else ""))
# control: liveness via API
code = sh("curl -s -o /dev/null -w %{http_code} http://localhost:8100/api/positions").strip()
(ok if code == "200" else bad)(f"{'control (API)':22} HTTP {code}")
# orb: liveness via isolated-state freshness
orb_states = latest_by("mai_tai:strategy-state-isolated", ["payload", "strategy_code"], count=20)
orb = orb_states.get("orb")
if orb:
    age = iso_age(orb["produced_at"])
    disp = "just now" if age < 5 else f"{age:.0f}s ago"
    (ok if age < 90 else warn if age < 240 else bad)(f"{'orb (iso-state)':22} {disp}")
else:
    bad(f"{'orb (iso-state)':22} NO recent isolated-state")
v2 = orb_states.get("schwab_1m_v2")

# ============================================================ (4) BAR FLOW
print("\n(4) BAR FLOW")
# --- v2 ---
if v2:
    p = v2["payload"]
    wl = p.get("watchlist", [])
    lta = p.get("last_tick_at", {})
    ages = {}
    for sym in wl:                       # only symbols on the CURRENT watchlist
        s = lta.get(sym)
        try:
            ages[sym] = (now - parse_et(s).astimezone(UTC)).total_seconds() if s else None
        except Exception:
            ages[sym] = None
    bc = p.get("bar_counts", {})
    dh_ok = p.get("data_health", {}).get("status") == "healthy"
    print(f"  v2 watchlist={wl}  bar_counts={ {k: bc.get(k) for k in wl} }")
    fresh = [s for s, a in ages.items() if a is not None and a < 120]
    stale = {s: a for s, a in ages.items() if a is None or a >= 120}
    if not wl:
        warn("v2 watchlist empty")
    elif not stale:
        ok(f"v2 all {len(fresh)} watchlist symbols fresh (<120s): {fresh}")
    elif len(stale) == len(wl) and not dh_ok:
        bad(f"v2 ALL watchlist symbols stale + data_health not healthy: {list(stale)}")
    else:
        shown = {s: (f"{a:.0f}s" if a is not None else "none") for s, a in stale.items()}
        warn(f"v2 {len(stale)}/{len(wl)} watchlist symbol(s) quiet >120s: {shown} "
             f"(benign if pre-market illiquid; data_health={p.get('data_health',{}).get('status')})")
else:
    bad("v2 no isolated-state")
# --- ORB (time-aware) ---
if orb:
    p = orb["payload"]
    md_id = redis("XREVRANGE mai_tai:market-data + - COUNT 1").split("\n")[0].strip()
    md_age = None
    if "-" in md_id and md_id.split("-")[0].isdigit():
        md_age = (time.time() * 1000 - int(md_id.split("-")[0])) / 1000
    print(f"  ORB watchlist={p.get('watchlist')}  universe_size={p.get('data_health',{}).get('universe_size')}")
    if md_age is not None:
        (ok if md_age < 15 else warn if md_age < 60 else bad)(
            f"gateway stream mai_tai:market-data newest tick {md_age:.0f}s old (ORB's feed)")
    else:
        warn("cannot read market-data stream age")
    sub = sh("sudo grep 'ORB-GATEWAY-SUBSCRIBE' /var/log/project-mai-tai/orb.log | tail -1").strip()
    subn = sub.split("symbols=")[-1] if "symbols=" in sub else "?"
    if not anchor_passed:
        info(f"ORB bar-build starts at 09:25 anchor — no bars yet is EXPECTED "
             f"(subscribed symbols={subn}). Confirm bars in the 09:25-09:29 window.")
        ok("ORB alive + subscribed + gateway feeding it (pre-anchor readiness OK)")
    else:
        lta = p.get("last_tick_at", {})
        if lta:
            ok(f"ORB post-anchor last_tick_at present: {lta}")
        else:
            warn("ORB post-09:25 but no last_tick_at yet — watch bar-build now")
else:
    bad("ORB no isolated-state")

# ============================================================ (5) WATCHLISTS + CONFIG
print("\n(5) WATCHLISTS + PROTECTED CONFIG")
# canonical protected set = the on-disk env (source of truth), NOT a hardcoded list,
# so this survives protect/unprotect changes and validates running-vs-on-disk config.
env_prot = sh("sudo grep -E '^MAI_TAI_PROTECTED_SYMBOLS=' "
              "/etc/project-mai-tai/project-mai-tai.env | head -1 | cut -d= -f2").strip()
PROT = set(x for x in env_prot.split(",") if x)
print(f"  canonical protected set (env): {sorted(PROT)}")
if v2:
    wl = set(v2["payload"].get("watchlist", []))
    leak = wl & PROT
    if leak:
        bad(f"v2 watchlist LEAKS protected {sorted(leak)}")
    elif not wl:
        warn("v2 watchlist empty")
    else:
        ok(f"v2 watchlist excludes all protected {sorted(PROT)} ({len(wl)} syms)")
# running /proc protected set must MATCH on-disk env (mismatch = env changed w/o restart)
for svc in ["schwab-1m-v2", "oms"]:
    pid = sh(f"systemctl show project-mai-tai-{svc}.service -p MainPID --value").strip()
    val = sh(f"sudo tr '\\0' '\\n' < /proc/{pid}/environ 2>/dev/null | "
             "grep '^MAI_TAI_PROTECTED_SYMBOLS=' | cut -d= -f2").strip()
    got = set(x for x in val.split(",") if x)
    (ok if got == PROT else bad)(
        f"{svc} /proc protected_set={sorted(got)}"
        + ("" if got == PROT else f"  != on-disk env {sorted(PROT)} (restart needed?)"))

# ============================================================ (6) DATA_HEALTH
print("\n(6) DATA_HEALTH")
for name, st in [("v2", v2), ("orb", orb)]:
    if not st:
        continue
    dh = st["payload"].get("data_health", {})
    status = dh.get("status")
    halted = dh.get("halted_symbols", [])
    warns = dh.get("warning_symbols", [])
    m = f"{name} status={status} halted={halted} warning={warns}"
    if status == "healthy" and not halted:
        ok(m)
    elif halted:
        bad(m)
    else:
        warn(m)

# ============================================================ VERDICT
print("\n" + "=" * 66)
if FAIL:
    verdict = f"RED  — {FAIL} FAIL, {WARN} WARN  — DO NOT trust the open; investigate."
elif WARN:
    verdict = f"AMBER — 0 FAIL, {WARN} WARN — review warnings, likely OK."
else:
    verdict = "GREEN — fleet READY. Open holds only load-dependent unknowns (#387/#388)."
print(f"VERDICT: {verdict}")
print("=" * 66)

# Exit code drives the cron alert branch: 0=green, 1=amber, 2=red.
sys.exit(2 if FAIL else 1 if WARN else 0)
