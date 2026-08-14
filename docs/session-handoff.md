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

**As of 2026-08-14 midday (session in progress).** Fleet active. Box still on **HEAD `3ac4721`** —
08-14's merges are on `main` but **NOT deployed**; the operator's call is **deploy after the close**,
which keeps today's validation uncontaminated.

✅ **BOTH 08-13 FLAGS ARE NOW EXERCISED** (they were UNEXERCISED at yesterday's close):
```
MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_RESTING_MIRROR_ENABLED=true   -> #688 PASS, 83 mirrors / 83 RTH rests
MAI_TAI_OMS_V2_EXIT_RELEASE_RESERVATION_ENABLED=true               -> #691 rejects 58 -> 24, NOT cleared
```
⛔ Rollback = delete those two lines + restart. Neither key existed before 08-13; **no env backup
exists** — do not assume one does.

### 08-14 validator, 12:06 ET (intraday ⇒ partial; re-run after the close for the quotable set)
| § | PR | verdict |
|---|---|---|
| 3 | #688 mirror | ✅ **PASS — 83/83.** The headline win; yesterday it was 215 rests / 0 mirrors |
| 4 | #689 attach | 🔴 FAIL **— but MIS-ATTRIBUTED, see below** |
| 5 | #691 release | 🔴 FAIL — 24 reservation rejects (was 58); close route 5 filled / 24 rejected |
| 6 | #692 reprotect | ⚠️ **PASS is FALSE** — wrong marker, see below |
| 7 | #687 claim expiry | ✅ PASS — expired 21× instead of latching the flip shut |
| 8 | #693 cron bit | ✅ PASS |
| 9 | money | `live:orb` 5 trades, **median +1.49%**, best +3.03%, worst −6.75% |

### ⛔⛔ TWO VALIDATOR DEFECTS FOUND BY READING A BROKER SCREEN — fix before trusting §4/§6
**Brackets are FINE: 148 `[V2-OCO-EMIT]` today.** §4 counts only `[WEBULL-PROTECT-ATTACHED]` (=0) and
concludes "held with no broker-side stop" — **wrong**. §6 counts `[OMS-EXIT-REPROTECT-FAILED]` (=0)
and prints PASS while the re-attach failed **9×** under `[WEBULL-PROTECT-FAILED]` — **a false clean.**
⇒ **Both sections must read BOTH markers.** The real defect is Tier A item 1 below.
⭐ The operator's own Webull screen (WETO `Target@8.17 / Stop@7.61`) is what falsified my report.
**The broker screen outranks our logs — use it first.**

### ▶ RUN THE VALIDATOR
```bash
ssh mai-tai-vps 'bash -s' < ops/health/validate_0813_deploy.sh        # or  -- YYYY-MM-DD
```
Read `➤ VERDICT:` only. `PASS` = exercised AND behaved · `FAIL` = act now · `UNEXERCISED` = the path
never ran (**a RESULT, not a pass**) · `VOID` = could not see (**never a pass**).
⛔ An intraday run is **not** a result — the day has not happened yet.
✅ **There is NO 20:00 ET deadline any more** (#699): rotated log siblings are read, so a post-close
run sees the whole day. A `VOID` now means the window aged out of retention (~6 days), not that you
ran late.
⭐ §0 self-tests against 08-13's 58 known rejects; §0b controls the **population** (rest / fill /
bracket visible?) against 08-12. Either failing ⇒ the run VOIDs its own zero-based verdicts.

## 📦 WHAT IS DEPLOYED ON THE BOX (`3ac4721`, 08-13 17:35 ET)

#687 fan-out claim release · #688 both legs rest at their own broker · #689 attach a stop+target
after a bare fill · #690 the 40-char coid cap · #691 the close cancels resting exit legs first ·
#692 re-attach if that close will not go through · #693 the cron exec bit.
Validation at merge: 2062 pass · ruff clean · 18 mutations all killed.

⛔ **08-13's "the close was fighting our own exit legs" narrative has moved out of this file** — it
is history now, not state. It lives in [`handoff-log.md`](handoff-log.md) and in
[[project_mai_tai_exit_reservation_conflict]]. The part that is still TRUE and still open is Tier A
items 1 and 2 above.

## 🚚 NOT YET DEPLOYED — merged to `main` on 08-14, goes out after the close
#697 #698 #699 (validator: rotation warning, blind-zero verdicts, rotated-log reading + population
controls) · #701 (bar-watch I2/I3) · #700 #702 (design + open items, docs only) · #682 #673 #646
#642 #640 (scripts/design, previously parked).
⛔ **#701 changes a live pager path**: after the pull, the bar-gap all-clear HOLDS for 30 min instead
of firing immediately. That is intended — it will look like a missing green the first time.
⛔ Pre-flight verified 08-14: VPS tree **0 local changes**, `bar_gap_watch_cron.sh` is **100755** on
`main`. Follow [[project_mai_tai_restart_bar_gap_checklist]] across the restart.

## 🕐 OPEN PRs
**#702** — the four 08-14 open-item notes (docs only). Everything else that was open on 08-14 morning
(#682 #673 #646 #642 #640, plus #697-#701) is **MERGED**; see the deploy queue above.

## 🔴 OPEN THREADS — tiered, detail in [`handoff-open-items.md`](handoff-open-items.md)

> ⛔ Numbers below are **tiers, not priorities within a tier**. The open-items file keeps stable
> item numbers; this list is the scannable index. **Tier A is what can lose money today.**

### 🔴 TIER A — can cost money now
1. **THE RELEASE → CLOSE → REPROTECT CHAIN LEAVES AN UNCOVERED WINDOW** *(item 15)* — #691 cancels a
   working bracket to close, the close is refused, #692 cannot put the bracket back (9× on 08-14).
   ⛔ Brackets themselves are FINE: **148 `[V2-OCO-EMIT]` today**; the failures are re-protect, not
   bare fills. ⛔ Validator §4/§6 read the wrong marker and print a FALSE CLEAN — fix before trusting.
2. **THE RETRY BOUND IS STILL UNREACHABLE** *(thread 3 detail)* — `_v2_exit_close_failures` resets on
   every HELD read, so a reject on a position we truly hold retries forever. **The fix underneath #691.**
3. **PRE-MARKET IS 0% BRACKETED** *(item 11)* — 14d: RTH 172/172 orb, 131/132 schwab; PRE 0/34, 0/13.
   Both brokers are limit-only in EH; the RTH-edge arm (#647 Gate 2, built, dark) is the only answer.
4. **`virtual_positions` FALSELY READS ZERO ON A POSITION WE HOLD** *(item 12)* — `[VIRTUAL-CLEAR]`
   fires inside the settle window (~0.7s after the fill) and never restores when the broker catches
   up ~15s later. ⛔ **This is what pages "reconcile drift" while you are in the trade** —
   `oms_managed_positions` is the correct book and reads open/qty=1 throughout. Alarm, not a defect.

### 🟡 TIER B — wrong numbers / wrong reasons, no immediate loss
5. **`broker_order_events` CONFLATES OUR ABORTS WITH BROKER REFUSALS** — needs a `source` column.
   08-14: the **largest** reject class was **83× our own `RuntimeError('Webull combo MASTER…')`**.
6. **THE WEBULL LEG BUYS UNDER A LOOSER CAP THAN THE STRATEGY CHOSE** *(item 13)* — fan-out intents
   carry no `eh_resting`/`resting_band_pct` ⇒ 1.0% instead of 0.5%. ⛔ Schwab genuinely refuses most
   of those names (10 of 12 have ZERO Schwab fills ever). Measured fills sit at median **+0.32%**,
   so the loose cap rarely binds. **Deferred 08-14.**
7. **THE FLOAT CEILING SILENTLY DROPS LARGE-FLOAT MOVERS** *(item 14)* — CAPR 08-14: +98%, 17.1M vol,
   never confirmed, `shares_outstanding 57.9M > 50M`. ⛔ Path C "extreme mover" is nested INSIDE that
   filter so it can never override it. ⛔ The reject reason is `logger.debug` only ⇒ no record of what
   the scanner dropped. Threshold = selection = DISCUSS FIRST; the INFO-logging half is cheap.
8. **ENTRY LEVELS QUANTISED BY ~70 bps** — +2%/−5% tick-rounded off the decided price; the 0.5% band
   collapses to ZERO under ~$2. **Fix the unit, not the number.**
9. **THE ORPHAN WATCH READS SCHWAB ONLY** — it can never clear a Webull question.

### 📋 TIER C — known, parked, not decaying
10. **CHURN IS THE BIGGEST NUMBER** — median 5 entries/symbol-day, max 16; ~200% of one position's
    notional crossed over 14d. Selection is **DISCUSS BEFORE BUILDING**.
11. **The 16:00 bracket death** *(item 11)* · **P0 boot-hold freshness gate** · **Redis evicts the
    heartbeat stream** *(item 9)* · **per-lot attribution gap** — unchanged.
12. **⚠ Flaky test** `test_scanner_cycle_history_retention_and_dedup` — passes alone and in-file,
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
