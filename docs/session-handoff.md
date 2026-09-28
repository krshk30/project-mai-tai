# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-28 (Mon) ~19:15 ET**, the evening of the Monday deploy. It was written before the formal
close-out on purpose. codex-2 still has overnight jobs (B11 study, #1029 replay) that will write to its journal. So
freeze → manifest → promote runs **Tuesday morning**, and this PR gets the manifest then. Roles (operator, 09-28):
**codex-2 builds all code; claude-1 reviews and pins.** "Send it to Codex" means a relay block. claude-1 changes
env/config only when the operator asks. The author never reviews.

> ⛔⭐⭐⭐ **OPERATOR'S STANDING LINES (all live).**
> - (09-23) A fix ships with its flag ENABLED so it can be validated.
> - (09-27) "No dark flag gets enabled without my ruling. No rule change unless I approve it."
> - (09-27) "Never ask me to trace or add logs; you have every source."
> - (09-27) Validate across everything before answering, and again before recommending.
> - (09-27) "No doc" means BUILD IT.
> - (09-27) The board is a table: `# | Item | Status | Evidence | Owner | Next action`.
> - (09-28) **Codex builds, claude reviews.**
> - (09-28) **Stability first: backtest/replay-engine work is parked TBD until the system is stable.**

---

# ✅ PRODUCTION — services `8f69a08a` live since 18:39–18:42 ET 09-28; box checkout `3141bbd5`

| | |
|---|---|
| running services | **oms 1328348 · strategy 1328360** (since 18:39:55 ET) · **v2 1329729** (since 18:42:25 ET), all on `8f69a08a` (#1049 #1054 #1055). NRestarts 0. market-data 2202865 · control 2273848 · reconciler 1626620 untouched |
| box checkout | **`3141bbd5`**, clean. That is `8f69a08a` + PAGEGAP #1056 (ops-only; `ops/health/known_defect_regression_watch.py` + its test), fast-forwarded with no restart |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-29` · `EXPECTED_SHA=3141bbd5…` · `EXPECTED_PID=1329729`. codex runs the gate at **06:20 ET Tue** |
| **not yet exercised** | v2 **BOOT-HOLD HELD** after the restart (`restoration_complete=0`, expected off-session). **REST warmup, BOOT-HOLD release and live bar continuity are TUESDAY pre-market checks, not passes** |
| exposure | 18:54 ET: 0 non-zero account_positions, 0 open managed rows. INC1 `open=4` (2 stale BENF 09-23, EGG + NAMI policy pages 09-28; see board 22) |
| flags (from `/proc`) | v2: TICK_CAPTURE, CW_V2_EH_RESTING_ENTRY, GAP_HOLD, FLIP_OWNED_FIRST_ENTRY, RETRY_ONE = true · entry end 15:45 · ATR_MASSIVE_SEED=false · #1054 stream-cross flag defaults ON (no STREAM-INERT at boot). OMS: OMS_V2_EH_ENTRY, DUAL_BROKER_FANOUT, OMS_V2_EOD_OCO_TRANSITION = true · CW target/stop 5.0/8.0 |
| deploy record | `/home/trader/fleet_health/deployments-20260928.md` (codex-2) · claude-1 verified 18:43 ET |
| merges 09-28 | #1054 EH1 pre-market stream cross · #1055 PMCAP1 Webull pre-market leg on the Schwab 0.5% band · #1056 PAGEGAP per-exit-ID pages. Closed: #1053 (my misauthored EH1, superseded) · #1057 BAND1 report (band unchanged) |
| migration | `alembic_version = 20260916_0021`, unchanged |

## What to READ Tuesday 09-29 (owner · first read) — report UNPROMPTED

| change | first evidence | owner |
|---|---|---|
| **Restart completion** | codex gate 06:20 ET 9/9 incl. REST warmup + BOOT-HOLD literal release before 07:00; bar continuity | codex gate / claude-1 read |
| **#1054 EH1** | next pre-market soft rest with a print ≥ trigger: `[V2-RESTING-EH-CROSS-STREAM]`, or `[V2-RESTING-EH-STREAM-SKIP] reason=ask_past_band / no_fresh_ask`; never after 09:30 | claude-1 |
| **#1055 PMCAP1** | a pre-market cross with the ask 0.5–1% over the trigger → BOTH legs `[OMS-ABANDON-INTENT] ASK_PAST_BAND`; in band → both legs the same limit. Watch whether Webull fills at the unbuffered ask | claude-1 |
| **#1049 SLOTCLEAR1** | first same-session re-add on the new process | claude-1 |
| **#1056 PAGEGAP** | first real dropped confirmation exit → one page per new `account:source_fill_id` (15-min throttle) | claude-1 |
| **Pass mark (Webull exits)** | 09-28 counts if clean → **2 of 5** (09-25, 09-28); recount at close | claude-1 |
| **B11 result** | codex's overnight run; report due **Thu 10-01** | codex / claude-1 review |
| **#1029 replay** | overnight after B11, finish by 03:30 ET, limit 3.5 → PASS / FAIL / VOID | codex / claude-1 review |

## Open decisions (operator)
1. **Board 30 + 22 (sent to codex 09-28):** extend the INC1 pager to 20:00 ET, DST-proof it, and build the INC1 auto-close. The operator said yes; build + review pending.
2. **B11 ruling** after the report (valid / invalid / mixed).
3. **DRIFT1** (replay mirror 16:00 vs live 15:45) — parked TBD by the operator until stable.
4. **B4 Massive ATR seed — still UNRESOLVED** (carried from 09-27): two codex-2 PMAX runs disagree (1.35957 / SELL
   11:52 vs 1.7842 / 11:09); `replay-30.md` has no PMAX 09-24 replay; the 30-session parity failure (98.05% < 99.5%)
   keeps `ATR_MASSIVE_SEED_ENABLED=false`. codex-2 must publish the replay path/parameters before any ruling.
5. **Weekdays-only digest** (currently daily) — follow-up if wanted (carried from 09-27).
6. Refusal response table — **DROPPED by the operator 09-28** (with old-board C5/C6); recorded, no build.

## Board (Mon 18:54 ET) — open rows only
| # | Item | Status | Evidence | Owner | Next action |
|---|---|---|---|---|---|
| 14 | #1049 seed-cap slot ownership | TO BE EXERCISED (live 18:42) | v2 1329729 on 8f69a08a | claude-1 | first same-session re-add |
| 19 | #1054 EH1 pre-market stream cross | TO BE EXERCISED (live) | flag default on, no INERT line | claude-1 | Tue pre-market soft rest |
| 21 | #1055 PMCAP1 | TO BE EXERCISED (live 18:39) | oms 1328348 | claude-1 | 0.5–1% pre-market cross |
| 27 | #1056 PAGEGAP | TO BE EXERCISED (installed) | box 3141bbd5; first run clean | claude-1 | first dropped exit |
| 31 | Restart evidence (warmup / BOOT-HOLD / bars) | TO BE EXERCISED | HELD off-session (expected) | codex / claude-1 | 06:20 gate |
| 3 | LXEH storm #1045 | TO BE EXERCISED | trigger never occurred | claude-1 | next removed name w/ pending recovery |
| 4 | Child fill before close #1046 | TO BE EXERCISED | no Webull native stop fill yet | claude-1 | first one |
| 5 | 16:00 handover #1047 | TO BE EXERCISED | flat at 16:00 09-28 | claude-1 | first held-at-16:00 day |
| 6 | After-hours Webull ladder #1048 | TO BE EXERCISED | nothing held after 16:00 | claude-1 | first held Webull share |
| 7 | Entry cutoff 15:45 | TO BE EXERCISED | no rest live at 15:45 09-28 | claude-1 | first rest live at 15:45 |
| 9 | Pager delivery, new sources | PARTLY EXERCISED | policy-reject pages delivered ×2 09-28; oco_exit_fill_unrecorded / webull_eh_ladder_unsold not yet | codex | first of either |
| 10 | 09-18 naked-leg hard case | TO BE EXERCISED | none since 09-18 | claude-1 | wait |
| 11 | Pass mark 5 Webull-exit sessions | IN PROGRESS | 2 of 5 if 09-28 stays clean | claude-1 | count at close |
| 25 | B11 rebuilt-arm per-trip (resting-buy provenance) | IN PROGRESS | amendment ad0ba310 17:56 before outcomes | codex | report ≤ Thu 10-01 |
| 15 | #1029 Momentum gateway | IN PROGRESS | replay overnight, limit 3.5; census peak 3.48 | codex | PASS/FAIL/VOID by morning |
| 22 | INC1 alarms never self-clear | OPEN | open=4 (BENF ×2 stale, EGG, NAMI) | codex | build (relay sent 09-28) |
| 30 | INC1 pager runs 07:00–17:59 ET only | OPEN | crontab `* 11-21 * * 1-5` UTC; shifts in EST | codex | extend to 20:00 ET, DST-proof (sent) |
| 20 | DRIFT1 replay mirror 16:00 vs 15:45 | TBD (parked) | drift page RED every 6 h | operator | un-park when stable |

**Closed 09-28:**
- #1043 GAPHOLD (skip once 07:00; real post-halt detect 09:42:46).
- #1044 policy rejects (NAMI 13:35; 7 retries dropped via the cache; Webull kept; EGG and NAMI paged).
- Pre-open gate 9/9.
- Alert split + digest.
- #1052 close-out.
- Row 23: the CLRO LULD pause (market event; no chase).
- Rows 28/29: watch-only.
- The cutoff analysis: dropped; 30% stays.
- BAND1: 0 of 12 proven band-miss winners; 0.5% band unchanged.
- Old board C1 and C4–C7.
- The deploy of 8f69a08a.

## Corrections I owe the record (09-28)
- **CLRO 07:24:** I said the fixed code "would have entered". The first spike's ask of 5.59 was above the 5.5868 cap, so the OMS would have abandoned it and burned the latch, and the in-band second spike would have been ignored (codex's review of #1053; fixed in #1054).
- **#1053:** I authored it myself when the operator meant "codex builds". Closed; codex rebuilt it as #1054.
- **Row 23:** I read "no [V2-ATR-PROBE] lines" as "no Schwab bars". The bars were stored live; GAPHOLD suppresses the probe line while holding.
- **Timestamps and a watch script:** I labelled a table "15:35 ET" when it was ~14:40 ET. A zsh word-split bug made one deploy watch print a false "CHANGED".
