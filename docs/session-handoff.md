# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-28 (Mon) ~19:15 ET; UPDATED 2026-09-29 (Tue) 05:25 ET** (evening cleanup, polygon_30s off, Momentum Option A #1060, overnight state). **UPDATED AGAIN 2026-09-29 (Tue) ~18:05 ET: tonight's install scope, the day's rulings and reviews — see TONIGHT below.** It was written before the formal
close-out on purpose. codex-2 still has overnight jobs (B11 study, #1029 replay) that will write to its journal. So
freeze → manifest → promote runs **Tuesday morning**, and this PR gets the manifest then. Roles (operator, 09-28):
**codex-2 builds all code; claude-1 reviews and pins.** "Send it to Codex" means a relay block. claude-1 changes
env/config only when the operator asks. The author never reviews.

> ⛔⭐⭐⭐ **OPERATOR'S STANDING LINES (all live).**
> - ⛔⭐⭐⭐⭐ **(09-29) RULE #1 — both agents: before reporting ANY failure, or suggesting/recommending anything, READ THE CODE AND THE DESIGN and CALL IT: REAL FAILURE (the design was violated; show it), EXPECTED BY DESIGN (say so; the check is the defect), or UNKNOWN (name what's missing). Never relay a tool's FAIL raw; never "suggest" in place of a call.** Trigger: the 09-29 06:24 gate rc=1 bar-continuity row reported as a failure by both agents though the REST backfill covers restart gaps by design.
> - (09-23) A fix ships with its flag ENABLED so it can be validated.
> - (09-27) "No dark flag gets enabled without my ruling. No rule change unless I approve it."
> - (09-27) "Never ask me to trace or add logs; you have every source."
> - (09-27) Validate across everything before answering, and again before recommending.
> - (09-27) "No doc" means BUILD IT.
> - (09-27) The board is a table: `# | Item | Status | Evidence | Owner | Next action`.
> - (09-28) **Codex builds, claude reviews.**
> - (09-28) **Stability first: backtest/replay-engine work is parked TBD until the system is stable.**
> - ⛔⭐⭐⭐⭐ (09-29) **REQUIREMENT CARD:** every rule-changing PR gets 2–3 plain-English sentences ("when X, the bot does Y; it does NOT do Z"), checked against the operator's OWN words, and confirmed by him BEFORE the build and again BEFORE the pin. Trigger: RETRY-ONE was built "blocked for the whole day" (my spec #1039 said "per day") when he had said "skip the next flip". Register: claude-1 memory `project_mai_tai_live_rule_cards`.
> - ⛔⭐⭐⭐⭐ (09-29) **DEPLOY = FLAG ON:** a flag is only a rollback switch. It goes ON in the same deploy, verified in `/proc`; no pin without that deploy line. The ONLY exception: #1064 ORB Schwab live orders stay OFF by the operator's staged plan (observe first, then his decision).

---

# 🌙 TONIGHT 09-29 — INSTALL IN PROGRESS at `3389090a` (status 20:08 ET, claude-1 read of the box)

**What happened after 18:05 ET:**
1. The operator gave GO for `ba3ebf59`.
2. codex STOPPED at the #1060 whole-install hard stop: the inactive-morning Momentum baseline was UNKNOWN. That is EXPECTED BY DESIGN; nothing was changed.
3. The operator chose **option C**: revert #1060 out of main.
4. **#1068** (a mechanical revert) was pinned at fbe86e6a (tree == reviewed; files byte-identical to box 3141bbd5; full suite = the box set) and merged → **main `3389090a`**.
5. Operator: **"GO 3389090a, install tonight per the plan"** (no market-data/momentum-paper restart; no watch; no migrations).
6. The codex side thread could not deploy, so the GO was re-sent to codex's MAIN thread.

**Box at 20:08 ET:**

| check | reading | verdict |
|---|---|---|
| checkout | `3389090a` | ✅ |
| oms | **1663656** since 20:05:36 ET, NRestarts 0 | ✅ new |
| strategy | **1663666** since 20:05:36 ET | ✅ new (OMS helper companion) |
| schwab-1m-v2 | **1664453** since 20:08:16 ET | ✅ new (#1065 live) |
| /proc (oms, v2, strategy) | FLOOR_EXIT=false · target 5.0 · stop 8.0 · EOD_OCO_TRANSITION=true · OVERNIGHT_FLATTEN=true · RETRY_ONE=true · ORB_SCHWAB_OBSERVE=true · ORB_LIVE_SCHWAB_ORDERS=false · POLYGON_30S=false | ✅ as ruled |
| market-data / momentum-paper | 2202865 / 2704889 — unchanged | ✅ |
| tracebacks since restart | oms 0 · v2 0 | ✅ |
| watch.py | `45df60c6…` unchanged (#1059 held) | ✅ |
| exposure | 0 non-zero positions | ✅ |
| orb | **1665228** since 20:09:18 ET (20:14 read) · /proc ORB_ENABLED=true, PAPER_LIFECYCLE=true | ✅ new |
| orb-schwab | **1665845** since 20:11:17 ET · unit installed · log `[ORB-SCHWAB] mode=OBSERVE_ONLY live_sending=False` · /proc observe=true / live=false · 0 tracebacks | ✅ new (observe only) |
| preopen.sh | re-pinned 20:12 ET: DATE 2026-09-30 · SHA `3389090a` · v2 1664453 · strategy 1663666. ⚠ file mode is now 700 (was 755); fine if the 06:20 run is as `trader` | ✅ (mode noted) |
| codex journal + its own completion statement | **not received yet (20:14 ET)** — close-out waits for it | ⏳ |

**Original scope table (18:05 ET), kept for the record:**

**Operator scope rulings (09-29):**
- "yes include both, drop the watcher step" (#1065 + #1067);
- "yes add #1066 tonight".
- codex's plan: `docs/review-artifacts/orb-schwab-live/OBSERVE_INSTALL_PLAN.md`, refreshed to the final main SHA after #1066 merges.
- **Nothing is installed yet.** Production = the table below.

| # | Item | PR / head | Review | State 18:05 ET | Goes live as |
|---|---|---|---|---|---|
| T1 | ORB Schwab, 2 shares (MACD ≥0 on completed Schwab 1-min; body <45% judged at the break-bar CLOSE; ATR purple on a later completed Schwab bar; +5/−8 bracket; 10:00 cancel; 15:55 close) | #1064 | pinned 442b492b | merged `90106fb4` | **OBSERVE ON / LIVE ORDERS OFF** (new `orb-schwab` unit); 10:02 ET report on 09-30 |
| T2 | ORB 90 s bar-evidence grace (no false critical page per trade) | #1067 | re-pinned 1f4781af | merged `059b261a` | with T1 |
| T3 | RETRY-ONE per SELL cycle (never a whole-day block) | #1065 | pinned b6a0ddd6 (11/11 mutations; late-SELL audit 0/11) | merged `eeaa4a7d` | **LIVE** on the v2 restart (RETRY_ONE already true) |
| T4 | Card 10: same exit in every session (+5/−8/confirmation/ATR flip, NO floor); 16:00 cancel + broker-confirm; Webull software sells on the shared path; 19:55 always sells | #1066 | re-pinned 0ab525c2 | merged `ba3ebf59` → **LIVE at OMS 1663656** | **LIVE** on the OMS restart + `MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED=false` |
| T5 | Momentum Option A (one Massive connection) | #1060 | pinned 6cb5d747 | **REVERTED by #1068 (option C)** — re-lands after a complete inactive-morning baseline + threshold review | gateway + momentum-paper restart; preflight steps 3–4 are WHOLE-INSTALL HARD STOPS |
| T6 | Restart-evidence three-way gate | #1061 | pinned 2096c930 | merged | isolated install 18:00 ET (codex), then ONE preopen.sh re-pin for 09-30 |
| T7 | Pager 07:00–20:00 ET + INC1 auto-close | #1059 | pinned 83202e2c | merged | **INSTALL HELD until WBREAD1** — no watch reinstall tonight |

Restarts in the plan: market-data, momentum-paper, oms, strategy, orb, orb-schwab (new), schwab-1m-v2. No migrations. Both accounts must be freshly flat, with zero open rows / armed segments.

**Known gap (accepted by the operator):** the new 19:55 "flatten rejected" incident is written but NOT paged until WBREAD1 + the watch reinstall. The 16:00 unconfirmed path already pages via `oms_v2_exit_release_unresolved`.

**After the install:** claude-1 verifies codex's journal against the box (SHA, new PIDs, flags from /proc: floor=false, target 5 / stop 8, RETRY_ONE=true, ORB observe=true / live=false; untouched PIDs unchanged) BEFORE the 06:20 gate.

# ✅ PRODUCTION — services `8f69a08a` live since 18:39–18:42 ET 09-28; box checkout `3141bbd5`

| | |
|---|---|
| running services | **oms 1328348** (since 18:39:55 ET) · **v2 1329729** (since 18:42:25 ET) · **strategy 1359274** (restarted 20:47:24 ET for polygon_30s OFF, see below), all on `8f69a08a` (#1049 #1054 #1055). NRestarts 0 on all (05:21 ET). market-data 2202865 · control 2273848 · reconciler 1626620 · market-capture 2202817 · momentum-paper 2704889 · orb 2051823 untouched |
| box checkout | **`3141bbd5`**, clean. That is `8f69a08a` + PAGEGAP #1056 (ops-only; `ops/health/known_defect_regression_watch.py` + its test), fast-forwarded with no restart |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-29` · `EXPECTED_SHA=3141bbd5…` · `EXPECTED_PID=1329729` · **`EXPECTED_STRATEGY_PID=1359274` / start `Tue 2026-09-29 00:47:24 UTC` (re-pinned by claude-1 20:47 ET; file sha256 `3763d6a3…`; codex informed)**. codex runs the gate at **06:20 ET Tue** |
| restart evidence | v2 **BOOT-HOLD RELEASED 18:58:07 ET 09-28** (`restoration_complete=1 reconstructed_uncapped=0`) once a watchlist appeared. **Still a Tuesday check:** REST warmup (05:21 ET: watchlist 2, warmed 0, data_flow stalled_offhours_rest_dry) and live bar continuity, via the 06:20 gate |
| exposure | 05:21 ET 09-29: 0 non-zero account_positions (0 open managed rows at 18:54). INC1 `open=4` (2 stale BENF 09-23, EGG + NAMI policy pages 09-28; see board 22) |
| flags (from `/proc`) | v2: TICK_CAPTURE, CW_V2_EH_RESTING_ENTRY, GAP_HOLD, FLIP_OWNED_FIRST_ENTRY, RETRY_ONE = true · entry end 15:45 · ATR_MASSIVE_SEED=false · #1054 stream-cross flag defaults ON (no STREAM-INERT at boot). OMS: OMS_V2_EH_ENTRY, DUAL_BROKER_FANOUT, OMS_V2_EOD_OCO_TRANSITION = true · CW target/stop 5.0/8.0 |
| deploy record | `/home/trader/fleet_health/deployments-20260928.md` (codex-2) · claude-1 verified 18:43 ET |
| **state 17:58 ET 09-29 (claude-1, box)** | **UNCHANGED since 09-28:** oms 1328348 · v2 1329729 · strategy 1359274 · market-data 2202865 · orb 2051823 · momentum-paper 2704889 · orb-schwab not installed. Checkout `3141bbd5`; preopen.sh still pinned 09-29. 0 non-zero positions. INC1 `open=6 delivered=6` (adds DXST policy 11:02) |
| merges 09-28 | #1054 EH1 pre-market stream cross · #1055 PMCAP1 Webull pre-market leg on the Schwab 0.5% band · #1056 PAGEGAP per-exit-ID pages. Closed: #1053 (my misauthored EH1, superseded) · #1057 BAND1 report (band unchanged) |
| migration | `alembic_version = 20260916_0021`, unchanged |
| **box cleanup (09-28 evening, operator-approved)** | Box is **4 vCPU / 8 GB** (hostname still says 2vcpu-4gb), no swap. (1) Leftover **TradingView Chrome stopped** 20:41 ET (up 164 days, no owning service; profile `/var/lib/project-mai-tai/tradingview_user_data` 972 MB kept). (2) **polygon_30s paper bot OFF**: `MAI_TAI_STRATEGY_POLYGON_30S_ENABLED=false` (env backup `…env.claude-before-polygon30s-off-20260929T004722Z`); last fill 09-02, OMS blocks its intents; strategy-engine gateway symbols were ⊆ v2's over 09-25..09-28. Result by 05:21 ET: **strategy RSS 2,544 → 1,889 MB, CPU ~30% → ~12%; box available RAM 1,218 → 2,263 MB**. Remaining candidates are in Open decisions |

## What to READ Tuesday 09-29 (owner · first read) — report UNPROMPTED

| change | first evidence | owner |
|---|---|---|
| **Restart completion** | codex gate 06:20 ET 9/9 incl. REST warmup + BOOT-HOLD literal release before 07:00; bar continuity | codex gate / claude-1 read |
| **#1054 EH1** | next pre-market soft rest with a print ≥ trigger: `[V2-RESTING-EH-CROSS-STREAM]`, or `[V2-RESTING-EH-STREAM-SKIP] reason=ask_past_band / no_fresh_ask`; never after 09:30 | claude-1 |
| **#1055 PMCAP1** | a pre-market cross with the ask 0.5–1% over the trigger → BOTH legs `[OMS-ABANDON-INTENT] ASK_PAST_BAND`; in band → both legs the same limit. Watch whether Webull fills at the unbuffered ask | claude-1 |
| **#1049 SLOTCLEAR1** | first same-session re-add on the new process | claude-1 |
| **#1056 PAGEGAP** | first real dropped confirmation exit → one page per new `account:source_fill_id` (15-min throttle) | claude-1 |
| **Strategy settle + gate** | 06:20 gate passes with the re-pinned strategy PID; strategy RSS stays ≈1.9 GB through the session | codex / claude-1 |
| **Pass mark (Webull exits)** | 09-28 counts if clean → **2 of 5** (09-25, 09-28); recount at close | claude-1 |
| **Momentum Option A #1060** | operator answers codex's 2 questions (Open decision 7) → claude-1 review/pin → separate exact-SHA GO | operator / codex / claude-1 |

## What to READ Wednesday 09-30 (after tonight's install) — report UNPROMPTED
| change | first evidence | owner |
|---|---|---|
| **Install + gate** | codex journal vs box; 06:20 gate on the new PIDs; three-way call (REAL FAILURE / UNKNOWN / EXPECTED BY DESIGN) | codex / claude-1 |
| **#1065 per-cycle retry** | `[V2-FLIP-OWNER-RETRY] … action=reset_new_segment` on the first live SELL; a symbol with 2 closes can trade the NEXT cycle | claude-1 |
| **#1066 Card 10** | first software exit sells at +5% with NO `[OMS-V2-CW-FLOOR-ARMED]`; first held-past-16:00 cancel + confirm; 19:55 flatten | claude-1 |
| **#1064/#1067 ORB observe** | codex 10:02 ET report on 09:25–10:00 records; no candidates / no install = NOT VALIDATED | codex / claude-1 |
| **#1060 Momentum** | the first-session slowdown thresholds (live-trading slowdown = stop; load 3.5 = warning only) | codex / claude-1 |
| carried | #1054/#1055 pre-market rest (none 09-29) · #1049 fresh SELL after a re-add · #1056 first dropped exit | claude-1 |

## Open decisions (operator)
1. ~~Board 30 + 22~~ — **BUILT: #1059 merged 09-29 10:31 ET** (2-read auto-close). **Install HELD until WBREAD1**: a Webull read error writes a fresh false zero (5 events / 30 d), which could close a real alarm.
2. ~~B11 ruling~~ — **CLOSED 09-28 ~20:15 ET, no rule change**: report `f6d2cf8d` (branch `codex/b11-rebuilt-arm-trip-study`), 286 first-slot fills = 207 LIVE / 2 REBUILT (pre-#993) / 77 UNKNOWN (71 pre-#993); **0 rebuilt fills after #993**; rebuilt arms rarely reach fills. Observation only: post-#993 LIVE entries lose (Schwab 22/63, Webull 31/75, medians ≈ −1.5%).
3. **DRIFT1** (replay mirror 16:00 vs live 15:45) — parked TBD by the operator until stable.
4. **B4 Massive ATR seed — still UNRESOLVED** (carried from 09-27): two codex-2 PMAX runs disagree (1.35957 / SELL
   11:52 vs 1.7842 / 11:09); `replay-30.md` has no PMAX 09-24 replay; the 30-session parity failure (98.05% < 99.5%)
   keeps `ATR_MASSIVE_SEED_ENABLED=false`. codex-2 must publish the replay path/parameters before any ruling.
5. **Weekdays-only digest** (currently daily) — follow-up if wanted (carried from 09-27).
6. Refusal response table — **DROPPED by the operator 09-28** (with old-board C5/C6); recorded, no build.
7. ~~Momentum Option A #1060~~ — **answered 09-29** (allow the gateway warm-up; stop rule = live-trading slowdown, load 3.5 = warning only); pinned 6cb5d747, in main, ships in tonight's install behind hard-stop gates. (Old text:) codex asks the operator two things before it's ready: (a) whether Momentum-triggered symbols may cause gateway warm-ups; (b) an ACTIVE-session load check (the 16:05 census runs after Momentum stops). Then claude-1 reviews and pins; deploy needs its own exact-SHA GO.
8. **PREMKT-ATR (board 32) — PARKED 09-29** with two recorded options (Schwab-tick bars; Massive seed for pre-07:00 warm-up only). Un-park = operator; a fill-level replay comes first.
10. **ORB Schwab activation (#1064)** — evening of 09-30 at the earliest, and only after: the observe evidence; an attended Schwab place/reprice/cancel test; early-close handling; proven phone delivery of the new ORB INC1 sources (needs the watch reinstall).
11. **ORB: a big RED break bar** (body ≥45% but closing down) — sell or keep? The rule as written ignores direction.
12. **Live-rule card audit, batch 1 — cards with NO operator words on record:**
    - 2b: wait for 3 purple bars; reprice only when the line moves ≥0.5%;
    - 3: a rest placed before 15:45 can fill until 16:00;
    - 9: ATR sell-flip exit + floor wording;
    - 11: Webull 1 share; held back while >8% away; policy-blocked name keeps the Webull leg.
    Card 4 (10,000-share floor + 3 thin bars) was CONFIRMED 09-29; card 10 was ruled; card 6 was fixed (#1065).
9. **Remaining box-cleanup candidates** (operator: "validate each"): Redis `mai_tai:snapshot-batches` 180×~6 MB ≈ 1.1 GB (15 min of scanner history; cutting to ~5 min saves ~0.7 GB but weakens scanner recovery after a strategy restart); ORB observer (76 MB, 0 intents in 7 days; its routing flag must stay on); the dashboard/control CPU (top user, 66%; snapshot-write hot path → codex fix); clutter (35 failed one-off units, 2 dead harness timers, old checkouts 3.2 GB disk).

## Board (Mon 18:54 ET) — open rows only
| # | Item | Status | Evidence | Owner | Next action |
|---|---|---|---|---|---|
| 14 | #1049 seed-cap slot ownership | PARTLY EXERCISED 09-29 | MSGY re-added 09:44 → rebuilt arm capped (by design); no fresh SELL after a re-add yet. #1065 changes the same seam | claude-1 | first fresh SELL after a re-add |
| 19 | #1054 EH1 pre-market stream cross | UNEXERCISED 09-29 | no pre-market rest (the first rest was 10:19 ET) | claude-1 | next pre-market soft rest |
| 21 | #1055 PMCAP1 | UNEXERCISED 09-29 | no pre-market rest | claude-1 | 0.5–1% pre-market cross |
| 27 | #1056 PAGEGAP | UNEXERCISED (correctly silent) | 09-29: 4/4 confirmation-exit legs closed, nothing dropped | claude-1 | first dropped exit |
| 31 | Restart evidence | PARTLY EXERCISED | 09-29 06:24 gate rc=1 bar-continuity = EXPECTED BY DESIGN (REST backfill; RULE #1). #1061 three-way gate merged; isolated install 18:00 ET | codex / claude-1 | verify the install + the 09-30 06:20 gate |
| 3 | LXEH storm #1045 | TO BE EXERCISED | trigger never occurred | claude-1 | next removed name w/ pending recovery |
| 5 | 16:00 handover #1047 | TO BE EXERCISED | flat at 16:00 09-28 and 09-29. #1066 (tonight) changes it to cancel + broker-confirm before any software sell | claude-1 | first held-at-16:00 day |
| 6 | After-hours Webull ladder #1048 | TO BE EXERCISED | nothing held after 16:00 | claude-1 | first held Webull share |
| 7 | Entry cutoff 15:45 | TO BE EXERCISED | no rest live at 15:45 09-28 | claude-1 | first rest live at 15:45 |
| 9 | Pager delivery, new sources | PARTLY EXERCISED | policy-reject pages delivered ×2 09-28; oco_exit_fill_unrecorded / webull_eh_ladder_unsold not yet | codex | first of either |
| 10 | 09-18 naked-leg hard case | TO BE EXERCISED | none since 09-18 | claude-1 | wait |
| 11 | Pass mark 5 Webull-exit sessions | IN PROGRESS → **3 of 5** | 09-29: Webull exits 3/3 clean (BKYI ×2 confirmation exits, cancel-then-sell 2.6 s, 0.14 s unprotected; DXST OCO target +4.9%); 0 refused sells | claude-1 | next clean session |
| 15 | Momentum Option A #1060 | **PINNED 6cb5d747, in main — ships tonight (T5)**; history: IN PROGRESS (draft) | one Massive connection; 5 s snapshot detection + capped per-symbol trades; CI ×2 + 342 targeted pass; #1029 CLOSED, its replay CANCELLED before start (not graded) | codex → operator (2 questions) → claude-1 review | answer Open decision 7, then review |
| 22 | INC1 alarms never self-clear | BUILT #1059 (merged); INSTALL HELD | open=6 (BENF ×2, DAIC HDL1, EGG, NAMI, DXST). Dry run on the box 09-29: all verified closable | codex | install with WBREAD1 |
| 30 | INC1 pager runs 07:00–17:59 ET only | BUILT #1059 (merged); INSTALL HELD | the installed crontab is still `* 11-21 * * 1-5` UTC | codex | install with WBREAD1 |
| 33 | WBREAD1: the Webull adapter turns a failed position read into FLAT (fresh false zero) | IN BUILD (codex) | 5 events / 30 d (conn reset ×4, timeout ×1); harm none seen. Scope includes: unreadable body `break`→[]; the exit recheck returns 0 on a raise | codex → claude-1 | build + review; unblocks the #1059 install |
| 34 | ORB Schwab (#1064/#1067) | OBSERVE install tonight (T1/T2) | live orders OFF (operator's staged plan) | codex / claude-1 | 10:02 report; activation = Open decision 10 |
| 35 | RETRY-ONE per SELL cycle (#1065) | INSTALL tonight (T3) | BKYI 09-29: 12:52 flip missed by the whole-day block (my spec error) | claude-1 | first `reset_new_segment` |
| 36 | Card 10 same exit everywhere (#1066) | INSTALL tonight (T4) | floor mode ON today: 86 `[OMS-V2-CW-FLOOR-ARMED]` / ~22 d (e.g. DAIC 09-17 armed 4.27 → sold 4.21) | claude-1 | first software +5% exit; first 16:00 hold; 19:55 |
| 37 | Live-rule card audit | IN PROGRESS | batch 1: 11 cards; mismatches 6 (retry) + 10 (after 16:00) fixed; card 4 confirmed | operator | Open decision 12 |
| 38 | DXST Webull entry mirror refused by our OMS `NO_FRESH_QUOTE` ×2 (14:11, 14:15 ET 09-29) | UNCLASSIFIED | `[OMS-BROKER-REJECT] … no valid OMS market snapshot` (client abort, entry side) | claude-1 | classify per RULE #1 (design or defect) |
| 20 | DRIFT1 replay mirror 16:00 vs 15:45 | TBD (parked) | drift page RED every 6 h | operator | un-park when stable |
| 32 | PREMKT-ATR: v2 misses chart BUY flips on names SHORT before 07:00 (Schwab serves no pre-07:00 bars; v2's ATR starts LONG at its first bar ~07:08) | **PARKED (operator 09-29)** | BKYI 09-29: chart BUY 07:47 @2.45 missed (0 intents). 125 stock-days 09-09..09-29: chart BUYs 504, v2 matched 273; Massive seed +35 (≈2/day; +5-before-−8 score 21/13 ≈ break-even); Schwab-tick bars +14 (3 false); codex 30-session coverage (#1062): 172 pre-market stock-days, ticks retained 21 sessions → 112, only 42 with ≥10 printed minutes 06:00–06:59 | operator (un-park) → codex build / claude-1 review | **Option 1: Schwab-tick pre-market bars** (Schwab-only; ~40% of pre-market names; proven BKYI 07:47). **Option 2: Massive seed #994 for the pre-07:00 warm-up only** (built, OFF; covers all names; needs an exception to the no-Massive-bars rule; G5 parity 98.05%). Either needs a fill-level replay first |

**Closed 09-28:**
- #1043 GAPHOLD (skip once 07:00; real post-halt detect 09:42:46).
- #1044 policy rejects (NAMI 13:35; 7 retries dropped via the cache; Webull kept; EGG and NAMI paged).
- Pre-open gate 9/9.
- Alert split + digest.
- #1052 close-out.
- Row 23: the CLRO LULD pause (market event; no chase).
- Rows 28/29: watch-only.
- The cutoff analysis: dropped; 30% stays.
- B11 / PROV1x: no rule change (0 rebuilt fills after #993).
- Leftover TradingView Chrome stopped; polygon_30s paper bot OFF (strategy restarted, gate re-pinned).
- #1029 closed (design superseded by Option A; overnight replay cancelled before start).
- BAND1: 0 of 12 proven band-miss winners; 0.5% band unchanged.

**Closed 09-29:**
- Row 4 #1046 child fill before close (operator 09-29 ~13:10 ET). Exercised on DXST 09-29 (Webull-only; Schwab policy-refused):
  - bare fill 12:45:26 @2.85, pair attached 12:45:37 (target 2.9925 / stop 2.622);
  - target child filled 12:48:19 @2.99 and was recorded from the broker execution record (`[OMS-CHILD-EXIT-RECORDED]`) BEFORE `[OMS-V2-OCO-RESOLVED-FLAT]` closed the row;
  - fill row `…-ocoexit-15DKFHKB` is in `fills`; no `oco_exit_fill_unrecorded`; exposure 0.
  - Target leg only: a stop-side child fill is not yet seen, but #1046 applies the same path to both sides.
- Old board C1 and C4–C7.
- The deploy of 8f69a08a.
- **BKYI 12:52 flip missed:** root cause = RETRY-ONE blocked the whole day (a spec error, mine). Fixed by #1065 (T3).
- **MSGY 15:26 flip missed:** by design (card 4: Webull-only after the Schwab policy refusal; the rest was pulled on thin bars of 200–4,710 sh). The operator: "drop MSGY… our design, which is good".
- **DXST Schwab leg missing:** by design (a policy refusal; Webull traded alone, +4.9%).
- **ORB Schwab rule set** ruled by the operator: MACD-only entry, body <45% at the bar close, ATR on Schwab 1-min bars; ATR entry gates not now.
- **Card 10** ruled (same exit everywhere, no floor, 19:55 sell anyway) → #1066. **Card 4** confirmed.
- **#1059** reviewed + pinned + merged (install held). #1061, #1064, #1065, #1067 merged.

## Corrections I owe the record (09-29)
- **RETRY-ONE "per day":** my spec #1039 turned the operator's "skip the next flip" into "one retry per name per day". Codex built my words; it blocked BKYI for the day. Hence the REQUIREMENT CARD rule.
- **06:24 gate:** codex and I both relayed rc=1 as a failure without checking the REST-backfill design. Hence RULE #1.
- **BKYI (pre-market):** I gave "history/session" explanations before simulating; the operator rejected them. Simulate first, then answer.

## Corrections I owe the record (09-28)
- **CLRO 07:24:** I said the fixed code "would have entered". The first spike's ask of 5.59 was above the 5.5868 cap, so the OMS would have abandoned it and burned the latch, and the in-band second spike would have been ignored (codex's review of #1053; fixed in #1054).
- **#1053:** I authored it myself when the operator meant "codex builds". Closed; codex rebuilt it as #1054.
- **Row 23:** I read "no [V2-ATR-PROBE] lines" as "no Schwab bars". The bars were stored live; GAPHOLD suppresses the probe line while holding.
- **Timestamps and a watch script:** I labelled a table "15:35 ET" when it was ~14:40 ET. A zsh word-split bug made one deploy watch print a false "CHANGED".
