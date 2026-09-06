# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

> ⛔⭐⭐ **PAPER vs LIVE — READ THIS BEFORE QUOTING ANY EXIT NUMBERS. Corrected 2026-09-06.**
> **`+5%` target · `−8%` stop · reclaim OFF · one trade per segment are PAPER BOT settings ONLY.**
> Live `schwab_1m_v2` remains on its **current** settings and **no change to them is pending**.
> ⛔ **NOTHING IS OR WAS GATED ON CONF3.** `claude-1` wrote *"CONF3 blocks the operator's +5%/−8%
> settings change"* and carried it through the CONF3 build brief, the relay blocks to `codex-2`, the
> CONF3 board row and the #905 pin summary. **It was never true**, and it is corrected append-only at
> `corrections/pr-905-paper-not-live-settings.md` on `review-pins`.
> ⭐ **CONF3's case is unaffected and rests on measured live evidence at CURRENT settings:** 3 of 3
> confirmation fires orphaned the `live:orb` fan-out leg, median **44m06s** open after the Schwab
> counterpart closed, leg-vs-leg dispersion **+2.17 / +2.98 / −4.21 pp** on one decision.

**Originally written by `claude-1`, 2026-09-04 17:57 ET.** Batch
`2026-09-04-probe-answered-and-conf1-bound`; merged as PR #901.

**Updated by `codex-2`, 2026-09-06 16:49 ET.** Operator-authorized OMS + control deploy; values
below were read from the VPS after both post-restart gates passed. Reviewed and pinned by
`claude-1` @ `6606c7c9` (verified against the box, not the PR text) and **merged as `ed7e62f4`**.

**Closed out by `claude-1`, 2026-09-06 evening.** Batch `2026-09-06-conf3-sil1-deployed`; this
close-out needs `codex-2`'s review before merge — the author does not review it.

> **⏩ UPDATED `claude-1`, 2026-09-06 ~12:40 ET — measurement Sunday, no build, no deploy, no merge.**
> Market closed 09-06 and 09-07 (Labor Day); next session **Tuesday 2026-09-08**.
> ✅ **THE ATR PROBE IS LIVE.** `/home/trader/atr_probe_enable.log`: fence **GO** (all four gates
> green), v2 stopped 20:05:03 / ready 20:05:34, pid 3135615, **step 4 proof present** —
> `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_PROBE_SYMBOLS=*` in the RUNNING process env. 0 tracebacks,
> warmup 850 bars for IMRN. Probe lines 0 as predicted (bars stop 20:00 ET). The transient timer has
> since expired and lists none — expected, it was one-shot.
> ✅ **CONF3 MEASURED, BUILT, PINNED AND MERGED — `0b9d16b7` (PR #902, pinned @ `eb44cb35`), plus
> its regression control `9a5a813c` (PR #904, pinned @ `72bc9db8`). ✅ DEPLOYED CLEAN 2026-09-06
> 16:48 ET. ⛔ LIVE CLOSE STILL UNEXERCISED.** The
> confirmation exit closed Schwab only; 3 of 3 fires orphaned the fan-out leg. ⛔ Its supposed safe
> branch, `[OMS-EXIT-REPROTECT]`, was **0-for-8** — #904 supplies the control for the recovery-flat
> branch that was previously inert to mutation.
> ✅ **SIL1 DECIDED, BUILT, PINNED AND MERGED — `fd3e31dd` (PR #903, pinned @ `e9b10d34`). ✅
> DEPLOYED CLEAN 2026-09-06 16:48 ET. ⛔ UNEXERCISED.** Operator decision 09-06, final: Schwab
> **8** on its own counter; ceiling stays **20**
> on both accounts; ⛔ **`live:orb` alarm UNSET — EXPLICITLY UNCOVERED**, because its benign band
> genuinely reaches 19 across 80 episodes and no threshold under 20 is defensible. **live:orb reject
> storms are not alarmed, deliberately.** ✅ **WRAP1 answered** (two
> triggers, one shared behaviour) and ✅ **CONF2 closed** (#897 is the whole answer). All four rows,
> with the numbers, are in [`handoff-open-items.md`](handoff-open-items.md); the narrative is in
> [`handoff-log.md`](handoff-log.md).
> ⛔ **CONF1 POST-FIX HAS NOT FIRED ONCE.** All three fires ran pre-#897 and the OMS restarted 09-04
> 17:50 ET, after the last one. The window is clean with no straddle — and that is not a pass.
> ✅ **The main checkout is CLEAN again** — it was parked on `codex/atr-bracket-grid` with a dirty
> tree earlier today; `codex-2` preserved the tracked and untracked work in
> `stash@{Sun Sep 6 12:32:15 2026}: On codex/atr-bracket-grid: codex preserve dirty atr-bracket-grid
> before CONF3 2026-09-06`. **That deploy blocker is RESOLVED — do not carry it forward as current.**
> Everything in this session was still read from `origin/main` and written in separate worktrees.
> ⏩ **Later the same day, all three landed — see the PRODUCTION block for the deploy gap:**
> SIL1 **#903** pinned @ `e9b10d34` → merged **`fd3e31dd`**; CONF3 **#902** (first head `fc377ea7`
> conflicted with #903 in `oms/service.py`, so it was **rebased, never Update-branch**, re-reviewed
> from scratch and re-pinned @ `eb44cb35`) → merged **`0b9d16b7`**; CONF3's regression control
> **#904** pinned @ `72bc9db8` → merged **`9a5a813c`**. ✅ **ALL THREE ARE MERGED AND ON THE BOX;
> #902/#903 RUNTIME CODE WAS DEPLOYED CLEAN, WHILE #904 IS TEST-ONLY. NONE IS PROVEN LIVE.**

---

# ✅ PRODUCTION — runtime code IN SYNC at `8b05ed42`

| | |
|---|---|
| box (deployed) | **`8b05ed42520db71bd0eae78934bfe84f547c955b`** — re-derived from the box 2026-09-06 16:49 ET; checkout clean |
| GitHub main at deploy | **`8b05ed42520db71bd0eae78934bfe84f547c955b`** — local `origin/main`, GitHub and VPS `origin/main` agreed before this docs-only follow-up |
| **runtime split** | **none** after the deploy; OMS and control restarted only after the exact checkout and runtime refresh. Both post-restart gates proved fresh healthy process identities; the heartbeat schema does **not** independently attest the SHA. |
| merges 09-04 | **seven**: #892 `b5ca941` · #893 `073a331` · #894 `1d7ec05` · #895 `b1769e5` · #896 `660bafa` · #897 `184cd8e` · #898 `c1e6357` — **these ARE on the box** |
| merges 09-06 | #903 SIL1 `fd3e31dd` · #902 CONF3 `0b9d16b7` · #904 CONF3 test-only control `9a5a813c` · #901 docs `8b05ed42`; **all on the box** |
| deploy 09-06 | **operator-authorized, after close; OMS + control only; migrations OFF** |
| ⇒ consequence | CONF3 fan-out and SIL1 alarm are running, but both remain **UNEXERCISED**. Released-leg recovery is also **UNEXERCISED**. Deployed clean is not proven live. |
| open PRs | **#906 only** — the close-out handoff PR. #905 merged as `ed7e62f4`; #901/#902/#903/#904 all merged. Exposure row below is the 16:49 ET post-deploy read |
| exposure | **16:49 ET post-restart:** zero open managed rows; `live:schwab_1m_v2` and `live:orb` both flat with broker truth 6 seconds old; overview also reports zero pending intents and zero open virtual/account positions |

| service | pid | NRestarts | | service | pid | NRestarts |
|---|---|---|---|---|---|---|
| oms | **3508410** | 0 | | schwab-1m-v2 | **3135615** | 0 |
| strategy | 3109745 | 0 | | market-data | 2202865 | 0 |
| control | **3508437** | 0 | | reconciler | 2202771 | 0 |
| **orb (NEW)** | 3110306 | 0 | | market-capture | 2202817 | 0 |

✅ **`schwab-1m-v2` was NOT restarted by this deploy.** PID stayed `3135615`, and its running
environment still contains `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_PROBE_SYMBOLS=*`.

> 🔑 **Schwab token, as of 2026-09-06 23:42 UTC** — `codex-2` re-authenticated during the deploy.
> `refresh_token_expires_at` = **2026-09-13T20:47:13Z = 16:47 ET Sun 09-13**; `expires_at` is the
> access token and rotates itself (a near-term value there is NOT an alarm — read BOTH fields).

# FLAGS

| flag | state | note |
|---|---|---|
| `..._CONFIRMATION_EXIT_ENABLED` (CONF1) | **`true`** | ON since 09-03; post-#897 live fire denominator remains zero |
| `oms_v2_eod_cancel_reexit_enabled` (EOD1601) | **`False`** | still OFF, still **UNEXERCISED** |
| ORB paper observer | **LIVE** | broker-disconnected, `paper:orb`, provider=none |
| `..._ATR_FLIP_PROBE_SYMBOLS` | **`*`, active** | confirmed in running v2 PID `3135615`; this deploy did not restart it |

---

# 🎯 THE PROBE RAN, AND IT FOUND A LIVE DEFECT

Attended, operator-run, ~$2.18. Entry `2.1657` → PM exit `2.1611` filled `16:05:00`, account flat.

**Answer: one DELETE cancels BOTH OCO children.** Schwab accepts a DELETE against an OCO child, and
the pair cancels as a unit.

⛔ **But the second DELETE returns `400 "Order in state CANCELED cannot be canceled"` — not 404.**
`release_native_oco_for_close` tolerated **404 only**, so it returned `unanswerable` on the very tick
both legs were gone, and it skipped the authoritative order-tree reread that would have said
`released`. Because the OCO always cancels as a unit, that path reported failure **every time it
succeeded**.

⇒ **EOD1601 could never have worked as written.** The 16:01 sequence would have cancelled the legs
correctly and then stalled, at exactly the moment it exists for. Fixed by **#898**: DELETE responses
are no longer release evidence; the reread decides, and stays fail-closed.

⭐ This is what the $2.18 bought. The question was never "will Schwab accept the DELETE" — it was
"what happens on the second one".

# ⛔ CONF1 SOLD A POSITION IT WAS NEVER DECIDED FOR — fixed by #897

IMRN 09-04, all ET:

- **11:34:11** position A fills · **11:35 bar** reads `short` · **11:36:09** OMS exits A. Correct.
- ⛔ the pending confirmation is **never popped after emitting**
- A closes; **position B opens 12:18:03**
- **12:18:04 → 12:18:37** the same stale decision fires **~20×** against B, into protective legs
  placed seconds earlier → **20 refusals** → reject ceiling → **36 minutes of suppressed exits**
- **12:54:01** the ATR flip is detected on time and **cannot be acted on**
- **12:58:33** the broker's own OCO leg closes it at 1.63

⭐ **The reject ceiling WORKED.** It stopped this at **20** where NCRA hit 145 and CHPT 205, and it
left the row and broker protection in place — which is why the OCO leg was still there to close it.

**#897** binds a confirmation to the episode it was decided for (`oms_managed_positions.id`, fresh
per episode), makes it one-shot, and drops it **before** any OCO protection reconcile — reaching
that reconcile would have stripped B's protection, and on `resolved_by_fill` closed B's row outright.

⚠️ **CONF1's behaviour is still barely observed**: 6 evaluations, 2 fires, 2 symbols, 2 days — and
one of the two fires *is* this defect.

---

# 🔴 OPEN — SEGMENT SLOT FLAGS NOT RELEASED ON AN ATR SHORT (deliberately unfixed)

IMRN: the 15:00 segment traded at 15:12 and exited 15:14 on a `short` read. At **15:40** a genuine
new arm fired and the **resting slot was still held by the 15:00 segment** — the placement went out
as `slot=reclaim`, not `slot=first`, and the Webull fan-out leg was suppressed.

⚠️ **With reclaim being turned off, there would have been no trade at 15:39 at all.**

⛔ **NOT FIXED, and that is the right call.** A release path already exists on the processed SELL
flip (`schwab_1m_v2.py:2455-2476`). It did not run. I could not distinguish *"the SELL flip was
never emitted"* from *"emitted and skipped"* because `[V2-ATR-PROBE]` is **off** — 0 lines for IMRN.
Fixing the symptom by adding a second release site would have been a guess.

**Frequency: 9 of 2,599 arms across 14 retained sessions inherited a previous segment's flags.**
⚠️ Treat 9 as an **upper bound** — the detector counts a missing `[V2-CW-DISARM]`, but that line is
gated on `cw_armed`, so a correct silent release is counted as an inheritance. **Rare, not routine.**

⇒ Operator ruling: enable the ATR probe, **do not build the fix**, let the next occurrence answer it.

# ⚠️ WRITE AMPLIFICATION — boarded, deliberately not fixed

#898 removed an early return, so a non-2xx now continues to the next child instead of stopping.
`_reconcile_confirmation_exit_protection` is **re-entrant across ticks** (`confirmation_inflight`
blocks overlap only). Measured today: **2,290 invocations, peak 4/sec**.

On a stuck path: **1 DELETE/tick before #898 → N after** (one per working SELL child; no cap — an
existing test uses three). ⛔ My #898 pin asserted "an OCO carries two" as fact; that was **false and
load-bearing**, corrected by `codex-2` and recorded append-only on `review-pins`
(`corrections/pr-898-two-child-bound.md`).

⚠️ Today this path issued **zero** DELETEs — **UNEXERCISED, not benign.** Operator ruling: board and
watch `[SCHWAB-NATIVE-OCO-DELETE-NON2XX]`, **no speculative throttle**. Repeated firing on one symbol
is the trigger.

---

# 🏗 ORB IS NOW A BROKER-DISCONNECTED PAPER OBSERVER (#896)

Deployed and running (pid 3110306). Two independent refusals: `OrbService._require_paper_decision`
and `OmsRiskService.process_trade_intent` (first statement, before persistence or dispatch).
Migration `20260904_0019` applied, additive.

⛔ **`orb_paper_events` is EMPTY — 0 paper-tape lines.** Deployed ≠ working. The first real evidence
is Tuesday's session.

⭐ **A check worth keeping:** `live:orb` is the account the **v2 Webull fan-out** routes to. An ORB
refusal keyed on *account* would have silently killed a real-money leg. It keys on
`strategy_code == "orb"`, which is correct — and correct for a non-obvious reason.

# ▶ IN FLIGHT — ATR PROBE ENABLE, HALF DONE

`MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_PROBE_SYMBOLS=*` is written to the env file (line 211,
backup `.bak-20260904-atrprobe`). **The running v2 process does not have it** — it is read once at
`__init__`.

A fence-gated restart is scheduled **on the box**, not from any agent session, so it runs whether or
not anyone is connected. It re-runs `preflight_v2_restart.sh` and **aborts without restarting**
unless that is GO, then writes a full checklist and pushes one ntfy either way.

| | |
|---|---|
| unit | `mai-tai-atr-probe-enable.timer` → `.service` (transient) |
| fires | 2026-09-05 **00:05 UTC** = Fri **20:05 ET** |
| script | `/home/trader/atr_probe_enable.sh` |
| **report** | **`/home/trader/atr_probe_enable.log`** |
| did it fire? | `systemctl list-timers mai-tai-atr-probe-enable --all` |

⛔ **VERIFY THIS FIRST NEXT SESSION — read that log.** If the fence refused, the probe is still off
and the segment-flag defect stays undiagnosable. ⚠️ **Expect ZERO `[V2-ATR-PROBE]` lines in tonight's
report**: bars stop at 20:00 ET, so there is nothing to sample. The proof it worked is step 4 of the
log — the variable present in the RUNNING process environment. The first real `state=` / `flip=`
lines arrive with Tuesday's session.

⚠️ The timer is transient and would not survive a reboot — but a reboot restarts v2 and picks up the
env var anyway, so either path ends with the probe on.
- 20:05 rather than 18:00 deliberately: **bars stop at 20:00 ET**, so the restart leaves no hole in
  `strategy_bar_history`.
- The fence blocked at 16:53 on its **clock proxy only** (all three substantive gates green). Its
  `--clock-override` was **not** used — the change gains nothing from the hour.

# ⭐⭐ STATUS SPLIT — ANSWERED vs UNEXERCISED. Do not collapse these.

⛔ **An unexercised fix reading as an answered item is how #684 sat live-and-never-exercised and how
`[WEBULL-PROTECT-ATTACHED]` ran 0-for-ever for seven days.** Keep the two columns apart.

| ✅ ANSWERED **and CLOSED** | ⛔ DEPLOYED and **UNEXERCISED** |
|---|---|
| **WRAP1** — two triggers, one shared behaviour. 07-13 matches 07-31/08-04; 09-03 is the outlier. ROOT1 = 1 pinned + 2 consistent + 2 unexplained, **supported not proven** | **CONF3** — fan-out is running; the **live close has never fired** post-#897 |
| **CONF2** — no evaluation row at 12:18; it was never a decision. #897 is the whole answer | **SIL1** — alarm is running; **no episode has reached 8**. `live:orb` is deliberately **UNSET/uncovered** |
| **CONF3's root cause** — the reprotect was **0-for-8** because the recovery only fails when the broker is erroring | **Released-leg recovery** — replaces that 0-for-8 path and is itself **UNEXERCISED** |

⇒ The left column is finished. **The right column is three open acceptances, not three wins.**

# ▶ ACCEPTANCE FOR TUESDAY — PRE-REGISTERED 2026-09-06, BEFORE THE SESSION

⛔ **Written down now so a quiet day cannot be read as a pass.** Each has a denominator and a stated
non-result. [[feedback_pre_registration_stopped_me]]

**1 · CONF3 close.** `confirmation exits fired` / of those, `evaluations with a fan-out leg` / of
those, `legs closed`.
⛔ **Non-result:** the fan-out marker now emits on **every evaluation**, so coverage is readable
without a fire — **but coverage is NOT the close.** Zero fires ⇒ report **UNEXERCISED**, never
"clean". Reporting coverage as if it were the close is the failure mode this line exists to stop.

**2 · Released-leg recovery.** `failed closes after a release` / of those, `re-protected` /
`proved flat` / `UNCOVERED`.
⛔ **Non-result:** it only exercises on a failed close **during broker trouble**. A day with no
Webull errors proves **nothing** about it. ⇒ **Report the session's
`webull.core.client ServerException` density ALONGSIDE the result, always**, so "it did not fire"
reads as *no opportunity* rather than *it works*.

**3 · SIL1.** `episodes ≥8 on live:schwab_1m_v2` / of those, `alarms raised` / of those,
`symbol visible on the operator's screen`.
⛔ **All three, not just the page count** — on-screen visibility during the stand-down IS the
requirement. ⛔ And `live:orb` stays **UNCOVERED**; its silence is not evidence of anything.

**One question to answer Tuesday, not to chase:** the `ServerException` density on `live:orb` per
session. One number, from data already being pulled, and it is the input that makes item 2 readable.
⛔ **An observation, not a workstream.**

# ▶ NEXT SESSION — Tuesday 2026-09-08 (Monday is Labor Day)

> ⏩ **09-06 update — item 1 is DONE.** The probe is confirmed live (see the header block); on
> Tuesday just verify it is *emitting* `state=` / `flip=` per bar.
> ⛔ **CONF3 AND SIL1 ARE BUILT, PINNED AND MERGED — THERE IS NOTHING TO BUILD.** #903 `fd3e31dd`,
> #902 `0b9d16b7`, #904 `9a5a813c`. **The runtime changes are deployed clean and the box is in sync.**
> Tuesday's job is **observe, not build or deploy**.
> ⛔ **CONF3's live close stays UNEXERCISED**: it is proven only when a real
> confirmation fire closes both legs. Merged is not proven, and deployed is not proven either.

1. **Confirm the ATR probe is live** and emitting `state=` / `flip=` per bar. Expect ~1.6 MB/session
   at the 9-symbol maximum; `maxsize 200M` gives ~59× headroom, so retention is unaffected.
2. **Watch `[SCHWAB-NATIVE-OCO-DELETE-NON2XX]`** — first firing sizes the write amplification.
3. **The segment-flag defect** — diagnose from the probe on the next occurrence. Do not guess.
4. **EOD1601 stays OFF.** #898 removed its blocker; it is not thereby proven. The 16:01 path has
   still never run against a real position.
5. **Reclaim is being turned off** (operator, 09-04). That makes item 3 materially more expensive —
   with reclaim off, an inherited slot means **no trade at all**.

# ⚠️ TWO VERIFICATION FAILURES OF MINE, RECORDED SO THEY ARE NOT REPEATED

1. **My first #893 mutation run was invalid.** `git checkout -- <file>` restores to HEAD and the fix
   was **uncommitted**, so three of four mutations silently ran against unfixed `main`. I caught it
   from the landing probe, not the result. ⇒ **Mutate against a COMMITTED baseline, and make the
   "did it land?" probe part of the harness.**
2. **I co-signed a number I had not checked** — "an OCO carries two" in the #898 pin, which was the
   entire reason I accepted the change. ⇒ **A reviewed number is a claim I co-sign; re-derive it or
   mark it as theirs.**

⚠️ Also: a one-shot test of mine **passed with the fix removed**. An accepted sell became a working
order, which flipped `dedup_active` and incidentally popped the pending — hiding the bug. Only a
*rejecting* adapter reproduced the live condition. Mutation caught it; review did not.

# ⚠️ Watch items live here, not in [`handoff-open-items.md`](handoff-open-items.md)

- **A deduped marker's silence is not an absence.** `[V2-RESTING-SLOT-CONSUMED]` is deduped by
  segment key; its absence at 15:42 was **not** evidence the guard passed — it had logged the same
  key at 15:15 and stayed silent. ⛔ Dedupe keyed on the thing under investigation is
  self-concealing. Never quote a count of that marker as a count of suppressions.
- **`bar_gap_watch_cron.sh` exits 0 by ET-GUARD SKIP after 16:00.** A clean evening exit is not a
  pass.
- **`broker_order_events` stores our own aborts as rejects** — every reject count is contaminated
  unless keyed on the broker's verbatim reason string.
- **`cancel_exit_leg_ids` still carries the 404-only assumption** that #898 fixed in its sibling.
  Check it before anything relies on it.
