# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-29 (Tue) ~21:00 ET, at the 09-29 close-out.** Freeze started 20:21 ET; both agents ACKed;
manifest `2026-09-29-rule1-requirement-card-retry-cycle-card10-orb-observe-3389090a` is BALANCED (93/93). This PR carries
all three files. codex-2 reviews and pins it, merges it, and runs `promote.sh` — that lifts the freeze.
Superseded snapshots from this page (the 09-28 production table, Tuesday's reads, the 18:05 install scope, the closed
decisions, Closed 09-28, the 09-28 corrections) were moved verbatim into `handoff-log.md` under 2026-09-29.
Roles (operator, 09-28): **codex-2 builds all code; claude-1 reviews and pins.** "Send it to Codex" means a relay block.
claude-1 changes env/config only when the operator asks. The author never reviews.

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
> - ⛔⭐⭐⭐⭐ (09-29) **DEPLOY = FLAG ON:** a flag is only a rollback switch. It goes ON in the same deploy, verified in `/proc`; no pin without that deploy line. The only STAGED exception: #1064 ORB Schwab live orders stay OFF (observe first, then the operator's decision). Flags that are OFF BY RULING (not staged fixes): `OMS_V2_CW_FLOOR_EXIT=false` (card 10: no floor ride) and `ATR_MASSIVE_SEED=false` (parked, board 32).

---

---

# ✅ PRODUCTION — `3389090a` installed 20:05–20:12 ET 09-29 (codex); claude-1 verified on the box 20:18 ET

Installed per the operator's GO ("GO 3389090a, install tonight per the plan"): #1064 + #1067 ORB Schwab (observe only),
#1065 RETRY-ONE per SELL cycle, #1066 Card 10, #1061 restart-evidence gate (isolated copy, 18:03 ET), #1059 code present
but its watch install HELD, #1068 = #1060 Momentum reverted (market-data and momentum-paper not restarted).

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
| codex journal + completion | **"Install complete"** — `/home/trader/fleet_health/deployments-20260929.md` sha256 `764d4bda…09b55d` ✅ matches · backup `backup-3389090a-20260930T0005Z/pre-deploy.tar.gz` `417babf7…a65d3` ✅ · fleet env `048f4e66…` → `d037d456…70e4839` ✅ · orb-schwab unit `7fe3449b…3579` ✅ · preopen.sh `81ba4ed6…225769a` ✅ | ✅ |
| health 20:18 ET | oms / strategy / v2 / orb / orb-schwab NRestarts 0; 0 tracebacks in oms, v2, strategy, orb-schwab | ✅ |
| 06:20 gate | one-shot codex gate on 09-30, run as `trader` (owns the mode-700 preopen.sh; sudo for read-only checks verified by codex) | scheduled |
| still UNEXERCISED (expected by design after hours) | v2 literal BOOT-HOLD release and current-session bar evidence → 06:20 gate + the first live reads | tomorrow |

**Known gap (operator-accepted):** the new 19:55 "flatten rejected" incident is written but NOT paged until WBREAD1 + the
watch reinstall. The 16:00 unconfirmed path already pages via `oms_v2_exit_release_unresolved`.

## What to READ Wednesday 09-30 (after the 09-29 20:05–20:12 ET install) — report UNPROMPTED
| change | first evidence | owner |
|---|---|---|
| **Install + gate** | codex journal vs box; 06:20 gate on the new PIDs; three-way call (REAL FAILURE / UNKNOWN / EXPECTED BY DESIGN) | codex / claude-1 |
| **#1065 per-cycle retry** | `[V2-FLIP-OWNER-RETRY] … action=reset_new_segment` on the first live SELL; a symbol with 2 closes can trade the NEXT cycle | claude-1 |
| **#1066 Card 10** | first software exit sells at +5% with NO `[OMS-V2-CW-FLOOR-ARMED]`; first held-past-16:00 cancel + confirm; 19:55 flatten | claude-1 |
| **#1064/#1067 ORB observe** | codex 10:02 ET report on 09:25–10:00 records; no candidates / no install = NOT VALIDATED | codex / claude-1 |
| **#1060 Momentum** | the first-session slowdown thresholds (live-trading slowdown = stop; load 3.5 = warning only) | codex / claude-1 |
| carried | #1054/#1055 pre-market rest (none 09-29) · #1049 fresh SELL after a re-add · #1056 first dropped exit | claude-1 |

## Open decisions (operator) — as of 21:00 ET 09-29
1. **DRIFT1** (replay mirror 16:00 vs live 15:45) — parked TBD by the operator until stable.
2. **B4 Massive ATR seed — still UNRESOLVED** (carried from 09-27): two codex-2 PMAX runs disagree (1.35957 / SELL
   11:52 vs 1.7842 / 11:09); `replay-30.md` has no PMAX 09-24 replay; the 30-session parity failure (98.05% < 99.5%)
   keeps `ATR_MASSIVE_SEED_ENABLED=false`. codex-2 must publish the replay path/parameters before any ruling.
3. **Weekdays-only digest** (currently daily) — follow-up if wanted (carried from 09-27).
4. **PREMKT-ATR (board 32) — PARKED 09-29** with two recorded options (Schwab-tick bars; Massive seed for pre-07:00 warm-up only). Un-park = operator; a fill-level replay comes first.
5. **Remaining box-cleanup candidates** (operator: "validate each"): Redis `mai_tai:snapshot-batches` 180×~6 MB ≈ 1.1 GB (15 min of scanner history; cutting to ~5 min saves ~0.7 GB but weakens scanner recovery after a strategy restart); ORB observer (76 MB, 0 intents in 7 days; its routing flag must stay on); the dashboard/control CPU (top user, 66%; snapshot-write hot path → codex fix); clutter (35 failed one-off units, 2 dead harness timers, old checkouts 3.2 GB disk).
6. **ORB Schwab activation (#1064)** — evening of 09-30 at the earliest, and only after: the observe evidence; an attended Schwab place/reprice/cancel test; early-close handling; proven phone delivery of the new ORB INC1 sources (needs the watch reinstall).
7. **ORB: a big RED break bar** (body ≥45% but closing down) — sell or keep? The rule as written ignores direction.
8. **Live-rule card audit, batch 1 — cards with NO operator words on record:**
    - 2b: wait for 3 purple bars; reprice only when the line moves ≥0.5%;
    - 3: a rest placed before 15:45 can fill until 16:00;
    - 9: ATR sell-flip exit + floor wording;
    - 11: Webull 1 share; held back while >8% away; policy-blocked name keeps the Webull leg.
    Card 4 (10,000-share floor + 3 thin bars) was CONFIRMED 09-29; card 10 was ruled; card 6 was fixed (#1065).
9. **FLAGGATE** — RULED 09-29 ("yes, have codex add the flag check to the gate"): the 06:20 gate fails if any fix flag in a running process differs from `ops/health/expected_flags.json`; CI refuses an unlisted new flag. Build = codex after the promote (board 39).

## Board (as of 21:00 ET 09-29) — open rows only
| # | Item | Status | Evidence | Owner | Next action |
|---|---|---|---|---|---|
| 14 | #1049 seed-cap slot ownership | PARTLY EXERCISED 09-29 | MSGY re-added 09:44 → rebuilt arm capped (by design); no fresh SELL after a re-add yet. #1065 changes the same seam | claude-1 | first fresh SELL after a re-add |
| 19 | #1054 EH1 pre-market stream cross | UNEXERCISED 09-29 | no pre-market rest (the first rest was 10:19 ET) | claude-1 | next pre-market soft rest |
| 21 | #1055 PMCAP1 | UNEXERCISED 09-29 | no pre-market rest | claude-1 | 0.5–1% pre-market cross |
| 27 | #1056 PAGEGAP | UNEXERCISED (correctly silent) | 09-29: 4/4 confirmation-exit legs closed, nothing dropped | claude-1 | first dropped exit |
| 31 | Restart evidence | PARTLY EXERCISED | 09-29 06:24 gate rc=1 bar-continuity = EXPECTED BY DESIGN (REST backfill; RULE #1). #1061 three-way gate installed 18:03 ET (isolated copy, blobs verified); preopen.sh re-pinned for 09-30 at 20:12 ET | codex / claude-1 | verify the install + the 09-30 06:20 gate |
| 3 | LXEH storm #1045 | TO BE EXERCISED | trigger never occurred | claude-1 | next removed name w/ pending recovery |
| 5 | 16:00 handover #1047 | TO BE EXERCISED | flat at 16:00 09-28 and 09-29. #1066 (installed 20:05 ET) changed it to cancel + broker-confirm before any software sell | claude-1 | first held-at-16:00 day |
| 6 | After-hours Webull ladder #1048 | TO BE EXERCISED | nothing held after 16:00 | claude-1 | first held Webull share |
| 7 | Entry cutoff 15:45 | TO BE EXERCISED | no rest live at 15:45 09-28 | claude-1 | first rest live at 15:45 |
| 9 | Pager delivery, new sources | PARTLY EXERCISED | policy-reject pages delivered ×2 09-28; oco_exit_fill_unrecorded / webull_eh_ladder_unsold not yet | codex | first of either |
| 10 | 09-18 naked-leg hard case | TO BE EXERCISED | none since 09-18 | claude-1 | wait |
| 11 | Pass mark 5 Webull-exit sessions | IN PROGRESS → **3 of 5** | 09-29: Webull exits 3/3 clean (BKYI ×2 confirmation exits, cancel-then-sell 2.6 s, 0.14 s unprotected; DXST OCO target +4.9%); 0 refused sells | claude-1 | next clean session |
| 15 | Momentum Option A #1060 | **REVERTED by #1068 (option C, 09-29)** — not installed; the install stopped at its own inactive-baseline hard stop (UNKNOWN); history: pinned 6cb5d747 | one Massive connection; 5 s snapshot detection + capped per-symbol trades; CI ×2 + 342 targeted pass; #1029 CLOSED, its replay CANCELLED before start (not graded) | codex → operator (2 questions) → claude-1 review | answer Open decision 7, then review |
| 22 | INC1 alarms never self-clear | BUILT #1059 (in running checkout 3389090a); WATCH INSTALL HELD | open=6 (BENF ×2, DAIC HDL1, EGG, NAMI, DXST). Dry run on the box 09-29: all verified closable | codex | install the watch with WBREAD1 |
| 30 | INC1 pager runs 07:00–17:59 ET only | BUILT #1059 (merged); INSTALL HELD | the installed crontab is still `* 11-21 * * 1-5` UTC | codex | install with WBREAD1 |
| 33 | WBREAD1: the Webull adapter turns a failed position read into FLAT (fresh false zero) | DRAFT PR #1063 @ ad865c84 (codex; on hold until the promote) | 5 events / 30 d (conn reset ×4, timeout ×1); harm none seen. Scope includes: unreadable body `break`→[]; the exit recheck returns 0 on a raise | codex → claude-1 | build + review; unblocks the #1059 install |
| 34 | ORB Schwab (#1064/#1067) | INSTALLED 20:11 ET, OBSERVE_ONLY (orb-schwab 1665845) | live orders OFF (operator's staged plan) | codex / claude-1 | 10:02 ET 09-30 report; activation = its Open decision |
| 35 | RETRY-ONE per SELL cycle (#1065) | INSTALLED 20:08 ET (v2 1664453), TO BE EXERCISED | BKYI 09-29: 12:52 flip missed by the whole-day block (my spec error) | claude-1 | first `reset_new_segment` |
| 36 | Card 10 same exit everywhere (#1066) | INSTALLED 20:05 ET (oms 1663656, floor=false), TO BE EXERCISED | floor mode ON today: 86 `[OMS-V2-CW-FLOOR-ARMED]` / ~22 d (e.g. DAIC 09-17 armed 4.27 → sold 4.21) | claude-1 | first software +5% exit; first 16:00 hold; 19:55 |
| 37 | Live-rule card audit | IN PROGRESS | batch 1: 11 cards; mismatches 6 (retry) + 10 (after 16:00) fixed; card 4 confirmed | operator | Open decision 12 |
| 38 | DXST Webull entry mirror refused by our OMS `NO_FRESH_QUOTE` ×2 (14:11, 14:15 ET 09-29) | UNCLASSIFIED | `[OMS-BROKER-REJECT] … no valid OMS market snapshot` (client abort, entry side) | claude-1 | classify per RULE #1 (design or defect) |
| 39 | FLAGGATE: 06:20 gate + CI fail on any fix flag not as ruled | RULED 09-29, NOT STARTED (freeze) | 09-29 20:20 ET /proc audit: all fix flags ON; OFF only ORB live (staged), CW floor (card 10), Massive seed (parked) | codex → claude-1 | build after the promote |
| 20 | DRIFT1 replay mirror 16:00 vs 15:45 | TBD (parked) | drift page RED every 6 h | operator | un-park when stable |
| 32 | PREMKT-ATR: v2 misses chart BUY flips on names SHORT before 07:00 (Schwab serves no pre-07:00 bars; v2's ATR starts LONG at its first bar ~07:08) | **PARKED (operator 09-29)** | BKYI 09-29: chart BUY 07:47 @2.45 missed (0 intents). 125 stock-days 09-09..09-29: chart BUYs 504, v2 matched 273; Massive seed +35 (≈2/day; +5-before-−8 score 21/13 ≈ break-even); Schwab-tick bars +14 (3 false); codex 30-session coverage (#1062): 172 pre-market stock-days, ticks retained 21 sessions → 112, only 42 with ≥10 printed minutes 06:00–06:59 | operator (un-park) → codex build / claude-1 review | **Option 1: Schwab-tick pre-market bars** (Schwab-only; ~40% of pre-market names; proven BKYI 07:47). **Option 2: Massive seed #994 for the pre-07:00 warm-up only** (built, OFF; covers all names; needs an exception to the no-Massive-bars rule; G5 parity 98.05%). Either needs a fill-level replay first |

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
