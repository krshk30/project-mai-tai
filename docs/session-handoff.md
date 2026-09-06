# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-04 17:57 ET.** Batch `2026-09-04-probe-answered-and-conf1-bound`.
Integrator for this rotation. Needs `codex-2`'s review before merge — the author never reviews.

> **⏩ UPDATED `claude-1`, 2026-09-06 ~12:40 ET — measurement Sunday, no build, no deploy, no merge.**
> Market closed 09-06 and 09-07 (Labor Day); next session **Tuesday 2026-09-08**.
> ✅ **THE ATR PROBE IS LIVE.** `/home/trader/atr_probe_enable.log`: fence **GO** (all four gates
> green), v2 stopped 20:05:03 / ready 20:05:34, pid 3135615, **step 4 proof present** —
> `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_PROBE_SYMBOLS=*` in the RUNNING process env. 0 tracebacks,
> warmup 850 bars for IMRN. Probe lines 0 as predicted (bars stop 20:00 ET). The transient timer has
> since expired and lists none — expected, it was one-shot.
> ✅ **CONF3 MEASURED, BUILT, PINNED AND MERGED — `0b9d16b7` (PR #902, pinned @ `eb44cb35`), plus
> its regression control `9a5a813c` (PR #904, pinned @ `72bc9db8`). ⛔ NOT DEPLOYED.** The
> confirmation exit closed Schwab only; 3 of 3 fires orphaned the fan-out leg. ⛔ Its supposed safe
> branch, `[OMS-EXIT-REPROTECT]`, was **0-for-8** — #904 supplies the control for the recovery-flat
> branch that was previously inert to mutation.
> ✅ **SIL1 DECIDED, BUILT, PINNED AND MERGED — `fd3e31dd` (PR #903, pinned @ `e9b10d34`). ⛔ NOT
> DEPLOYED.** Operator decision 09-06, final: Schwab **8** on its own counter; ceiling stays **20**
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
> **#904** pinned @ `72bc9db8` → merged **`9a5a813c`**. ⛔ **ALL THREE ARE MERGED AND NONE IS
> DEPLOYED.**

---

# ⛔ PRODUCTION — main is AHEAD of the box ON RUNTIME CODE. NOT IN SYNC.

| | |
|---|---|
| box (deployed) | **`c1e6357afa1ccf9b7327745c129b4b6510c1dd78`** — re-derived from the box 2026-09-06, unchanged since 09-04 |
| main | **`9a5a813cc2e72f08b07a39119f6c4b474dad055d`** |
| **split** | ⛔ **NOT docs-only.** `git diff --name-only c1e6357 origin/main` outside `docs/`: **`src/project_mai_tai/oms/service.py`** and **`src/project_mai_tai/services/control_plane.py`**, plus five test files. Runtime diffstat: **899 insertions / 43 deletions across 2 source files.** |
| merges 09-04 | **seven**: #892 `b5ca941` · #893 `073a331` · #894 `1d7ec05` · #895 `b1769e5` · #896 `660bafa` · #897 `184cd8e` · #898 `c1e6357` — **these ARE on the box** |
| merges 09-06 | **three, NONE DEPLOYED**: #903 SIL1 `fd3e31dd` · #902 CONF3 `0b9d16b7` · #904 CONF3 test-only control `9a5a813c` |
| ⇒ consequence | **Nothing merged on 09-06 is running.** CONF3's fan-out and SIL1's alarm exist in `main` only. Do not read Tuesday's live behaviour as containing either until a deploy happens under the operator's gate. |
| open PRs | **this handoff PR** (#901) |
| exposure | last verified **09-04 17:56 ET**: Schwab positions 0 · working orders 0 — FLAT. ⚠ Market has been closed since; **re-verify before any deploy**, do not quote this as current. |

| service | pid | NRestarts | | service | pid | NRestarts |
|---|---|---|---|---|---|---|
| oms | 3109734 | 0 | | schwab-1m-v2 | 2897273 | 0 |
| strategy | 3109745 | 0 | | market-data | 2202865 | 0 |
| control | 2928441 | 0 | | reconciler | 2202771 | 0 |
| **orb (NEW)** | 3110306 | 0 | | market-capture | 2202817 | 0 |

⚠️ **`schwab-1m-v2` was NOT restarted today** (pid unchanged since 09-03). That matters for the ATR
probe below.

# FLAGS

| flag | state | note |
|---|---|---|
| `..._CONFIRMATION_EXIT_ENABLED` (CONF1) | **`true`** | ON since 09-03. Its one live fire today was the defect below |
| `oms_v2_eod_cancel_reexit_enabled` (EOD1601) | **`False`** | still OFF, still **UNEXERCISED** |
| ORB paper observer | **LIVE** | broker-disconnected, `paper:orb`, provider=none |
| `..._ATR_FLIP_PROBE_SYMBOLS` | **set to `*` in the env file, NOT yet active** | needs a v2 restart — see IN FLIGHT |

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

# ▶ NEXT SESSION — Tuesday 2026-09-08 (Monday is Labor Day)

> ⏩ **09-06 update — item 1 is DONE.** The probe is confirmed live (see the header block); on
> Tuesday just verify it is *emitting* `state=` / `flip=` per bar.
> ⛔ **CONF3 AND SIL1 ARE BUILT, PINNED AND MERGED — THERE IS NOTHING TO BUILD.** #903 `fd3e31dd`,
> #902 `0b9d16b7`, #904 `9a5a813c`. **None is deployed**, so Tuesday's job is **deploy and observe
> under the operator's gate**, not build. ⛔ **The box is BEHIND main on runtime code** (see the
> PRODUCTION block) — until a deploy happens, live behaviour contains neither change.
> ⛔ **CONF3's live close stays UNEXERCISED even after deploying**: it is proven only when a real
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
