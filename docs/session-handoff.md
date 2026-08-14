# Session Handoff — CURRENT STATE (read this first)

> ## ⛔ HOW TO MAINTAIN THIS FILE — two verbs, never merge them
> 1. **OVERWRITE this file.** It answers one question: *what is true RIGHT NOW.* If a line here is
>    no longer true, **delete or rewrite it** — never append.
> 2. **APPEND to [`handoff-log.md`](handoff-log.md).** That is where *what changed today* goes.
>
> **Target: ~150 lines.** To onboard an agent: *"Read `docs/session-handoff.md`."*

> **⛔⭐ OUTPUT CONSTRAINT (every study): report per-trade %, MEDIAN-FIRST, with a drop-one.
> NEVER a bare dollar total.**

---

## ⚡ FIRST SCREEN — act on this alone

**As of 2026-08-14 EOD.** Fleet active. **Deployed HEAD `69d4b5a`** (pulled + verified by content
16:36 ET). Tree **0 local changes**. Account **FLAT on everything we trade**.
⚠️ **`live:schwab_1m_v2` holds XPON −1000 @ 4.4152 (mkt −$4,730) — the OPERATOR'S OWN SHORT, not
ours.** Proven: zero XPON orders we ever placed on any account at any time, 0 fills / 0 intents /
0 managed rows, 0 `oms.log` mentions. The OMS scoping invariant means we never act on it.
⛔ It is also **INVISIBLE to the reconciler** — that query filters `quantity > 0`, so **no short
position is ever compared.** New open item 16.

### The 08-13 deploy, final read (validator 15:33 ET, day complete)
| § | PR | verdict | number |
|---|---|---|---|
| 3 | #688 mirror | ✅ **PASS** | **172 mirrors / 172 RTH rests** — yesterday was 215 rests / **0** |
| 4 | protection | 🔴 FAIL | 18 fills · **259 `[V2-OCO-EMIT]` brackets** · **10 re-protect failures** |
| 5 | #691 release | 🔴 FAIL | reservation rejects **58 → 27**; `-close-` 5 filled / 27 rejected |
| 6 | #692 reprotect | 🔴 FAIL | 13 releases, 4 re-protects, **10 failed via `[WEBULL-PROTECT-FAILED]`** |
| 7 | #687 claim expiry | ✅ PASS | expired **35×** instead of latching the flip shut |
| 8 | #693 cron bit | ✅ PASS | tree clean |
| 9 | money | — | `live:orb` 8 trades **median +1.79%** (worst −6.75) · `v2` 2 trades **median +1.63%** |

⭐ **#688 is the day's win and it is unambiguous: 172/172.**
⛔ **§4/§6 were BOTH lying this morning and are fixed (#703)** — §4 counted only the re-protect
marker and called bracketed fills "unprotected"; §6 printed PASS over the same 10 failures. **A
broker screenshot caught them, not the script.** Today's numbers above are from the fixed version.

## 📦 DEPLOYED TONIGHT (16:36 ET) — `3ac4721` → `69d4b5a`
**Only ONE file under `src/` changed: `reconciliation/service.py`.** Everything else is docs,
`ops/health`, standalone scripts, tests. ⇒ **reconciler restarted alone; strategy / oms /
schwab-1m-v2 were NOT touched, so there is NO BAR HOLE.** Verified after: heartbeat healthy,
cycles completing ~20s, **0 errors**, both cron scripts parse, exec bits `-rwxrwxr-x`.

⛔⛔ **THE RECONCILER CHANGE IS DEPLOYED BUT UNEXERCISED — that is a RESULT, not a pass.**
The account is flat (`account_positions qty>0` = **0**, open managed rows = **0**), so **no finding
has been generated on the new code**. Whether the payload carries `managed_quantity`/`our_quantity`
and whether the WETO-class false page is actually gone is **UNKNOWN until a position is open during
a reconciler cycle — Monday at the earliest.** Unit tests pin it (8 cases, 4 mutants killed); tests
are not the live path.

**Also live now:** #697/#698/#699 (validator: intraday rotation warning, blind-zero verdicts,
**rotated logs are read ⇒ there is NO 20:00 ET deadline**, §0b population controls) · #701 bar-watch
I2/I3 · #703 the §4/§6 fix.
⛔ **#701 changes a live pager**: after a bar-gap repair the all-clear now HOLDS 30 min instead of
firing immediately. Intended — it stops the watch verifying its own INSERT — but the first time it
happens it will look like a missing green. **Not yet exercised: the watch window is 07:00-16:00 ET
and the deploy landed at 16:36.**

## 🕐 OPEN PRs
**NONE.** Nine merged 08-14: #697 #698 #699 #700 #701 #702 #703 #704 + the five parked
(#682 #673 #646 #642 #640). All of it is deployed.

## 🔴 OPEN THREADS — tiered, detail in [`handoff-open-items.md`](handoff-open-items.md)

> ⛔ Numbers below are **tiers, not priorities within a tier**. The open-items file keeps stable
> item numbers; this list is the scannable index. **Tier A is what can lose money today.**

### 🔴 TIER A — can cost money now
1. **⭐ MONDAY'S FIRST MOVE — THE RELEASE → CLOSE → REPROTECT CHAIN** *(item 15)* — #691 cancels a
   working bracket to close, the close is refused, #692 cannot put it back. **10× on 08-14.**
   Direction: re-price the re-attach off a **fresh quote at attach time** (it currently uses the
   ENTRY price and fires 37s–10min later, so the stop is stale), **refuse to attach if we no longer
   hold**, and **serialise** the retry loop (two interleaved sequences per fill today).
   ⛔ Control it: a re-attach that has only ever failed proves nothing until one succeeds.
   ✅ The measuring instrument is fixed (#703) — Monday's numbers are trustworthy.
2. **THE RETRY BOUND IS STILL UNREACHABLE** — `_v2_exit_close_failures` resets on every positively-
   HELD read, so a reject on a position we truly hold retries forever. **The fix underneath #691**,
   whose rejects went 58 → 27 but did not clear.
3. **PRE-MARKET IS 0% BRACKETED** *(item 11)* — 14d: RTH 172/172 orb, 131/132 schwab; PRE 0/34, 0/13.
   Both brokers are limit-only in EH; the RTH-edge arm (#647 Gate 2, built, dark) is the only answer.
4. **`virtual_positions` FALSELY READS ZERO ON A POSITION WE HOLD** *(item 12)* — `[VIRTUAL-CLEAR]`
   fires ~0.7s after a fill, inside the ~15s broker settle window, and never restores.
   ✅ **The reconciler half is FIXED + DEPLOYED (#704)** — it now reads `oms_managed_positions` too.
   ⛔ **UNEXERCISED** (flat account since the deploy) **and the root cause is untouched**: the clear
   itself still zeroes a live row, and five consumers still believe it.

### 🟡 TIER B — wrong numbers / wrong reasons, no immediate loss
5. **⛔ THE RECONCILER CANNOT SEE A SHORT POSITION AT ALL** *(item 16, new 08-14)* — the query
   filters `quantity > 0`, so a negative quantity never enters the comparison. The gap is not "we
   ignore the operator's shorts", it is **"a short drift of OURS would be undetectable."**
6. **`broker_order_events` CONFLATES OUR ABORTS WITH BROKER REFUSALS** — needs a `source` column.
   08-14: the **largest** reject class was **83× our own `RuntimeError('Webull combo MASTER…')`**.
7. **THE WEBULL LEG BUYS UNDER A LOOSER CAP THAN THE STRATEGY CHOSE** *(item 13)* — fan-out intents
   carry no `eh_resting`/`resting_band_pct` ⇒ 1.0% instead of 0.5%. ⛔ Schwab genuinely refuses most
   of those names (10 of 12 have ZERO Schwab fills ever). Measured fills sit at median **+0.32%**,
   so the loose cap rarely binds. **Deferred 08-14.**
8. **THE FLOAT CEILING SILENTLY DROPS LARGE-FLOAT MOVERS** *(item 14)* — CAPR 08-14: +98%, 17.1M vol,
   never confirmed, `shares_outstanding 57.9M > 50M`. ⛔ Path C "extreme mover" is nested INSIDE that
   filter so it can never override it. ⛔ The reject reason is `logger.debug` only ⇒ no record of what
   the scanner dropped. Threshold = selection = DISCUSS FIRST; the INFO-logging half is cheap.
9. **ENTRY LEVELS QUANTISED BY ~70 bps** — +2%/−5% tick-rounded off the decided price; the 0.5% band
   collapses to ZERO under ~$2. **Fix the unit, not the number.**
10. **THE ORPHAN WATCH READS SCHWAB ONLY** — it can never clear a Webull question.

### 📋 TIER C — known, parked, not decaying
11. **CHURN IS THE BIGGEST NUMBER** — median 5 entries/symbol-day, max 16; ~200% of one position's
    notional crossed over 14d. Selection is **DISCUSS BEFORE BUILDING**.
12. **The 16:00 bracket death** *(item 11)* · **P0 boot-hold freshness gate** · **Redis evicts the
    heartbeat stream** *(item 9)* · **per-lot attribution gap** — unchanged.
13. **⚠ Flaky test** `test_scanner_cycle_history_retention_and_dedup` — passes alone and in-file,
    failed once in a full run. Cross-file ordering.

## 🔔 ALERTING
`orphan_order_cron.sh` (Schwab-only) · `bar_gap_watch_cron.sh` · `reconcile_alert_cron.sh` ·
`entry_fix_watch/watch_cron.sh` ⛔ **silence is NOT green — read `STATUS.txt`** · OMS liveness ·
pre-open readiness · token expiry · OCO capture.
⛔ All ROOT crontab, ET-guarded **inside** the script.
✅ **FIXED TODAY (#693):** `bar_gap_watch_cron.sh` + `reconcile_alert_cron.sh` were 100644 in git and
chmod'd by hand on the box, leaving the VPS tree **permanently dirty** — which **BLOCKS EVERY
DEPLOY** (`refusing deploy because repo has local changes`). ⛔ Do NOT `git checkout` them to unblock:
root's crontab invokes both **directly by path**, so reverting to 100644 silently kills the bar-hole
watch and the drift alarm. The exec bit is now committed.
⛔ `schwab_token_expiry_cron.sh` is still 100644 but runs from a **separate `/home/trader/` copy** —
its repo mode is not load-bearing. Do not "fix" it.

## 🔑 SCHWAB TOKEN
**Read from the store 2026-08-13 17:40 ET:** `refresh_token_expires_at = 2026-08-19T09:21:35Z`
⇒ **Wed 2026-08-19 05:21 ET.** Store mtime fresh ⇒ the refresher is alive.
⚠️ 05:21 is **before** the 07:00 EH open — re-auth Tue evening or Wed after the close.
⛔ **Never quote this date from memory; read `refresh_token_expires_at`.** A memory file carried a
wrong date (`Mon 08-17`) into this session — that is exactly why this rule exists.

## 🧠 MEMORY POINTERS (auto-load each session)
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_restart_bar_gap_checklist]] **(READ BEFORE ANY RESTART)** ·
[[project_mai_tai_exit_reservation_conflict]] **(today's finding)** ·
[[project_mai_tai_cancel_is_fire_and_forget]] · [[project_mai_tai_probe_w_webull_stoplimit_master]] ·
[[project_mai_tai_two_exit_routes_close_unattributable]] ·
[[project_mai_tai_premarket_exit_protection]] · [[feedback_the_brokers_book_is_shared]] ·
[[feedback_commit_before_you_mutate]] · [[feedback_a_wrong_reason_is_worse_than_a_missing_one]]
