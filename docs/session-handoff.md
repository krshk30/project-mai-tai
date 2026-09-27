# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-27 (Sun) 12:05 ET.** Batch `2026-09-27-weekend-gaphold-unit-error-batch-of-eight-deployed-flag-audit-alert-split`
(covers Thu 09-24 → Sun 09-27). Integrator: `claude-1` (wrote the batch specs, reviewed and pinned every build);
`codex-2` built #1044 #1045 #1046 #1047 #1048 #1049 #1050 #1051, merged them, executed the ledger writes and the Sunday
deploy, and reviews this PR. The author never reviews.

> ⛔⭐⭐⭐ **OPERATOR'S STANDING LINES (all live).** (09-23) a fix ships with its flag ENABLED so it can be validated.
> (09-27) **"No dark flag gets enabled without my ruling. No rule change unless I approve it."** (09-27) **"Never ask
> me to trace or add logs; you have every source — a wrong answer is a source you did not read."** (09-27) **Validate
> across everything — code, every service, logs, DB, traces, tests — before answering, and again before recommending.**
> (09-27) **"No doc" means BUILD IT**; spec PRs close when the build merges. (09-27) **The board is a table**:
> `# | Item | Status | Evidence | Owner | Next action`, status ∈ {TO BE EXERCISED, IN PROGRESS, CLOSED, OPEN}, owner never blank.

---

# ✅ PRODUCTION — box SHA `59f9532e`; RUNNING CODE = `91a57a15` (+ docs/ops since); `#1049` merged NOT live

| | |
|---|---|
| box (checkout) | **`59f9532ed7e9817897b5a7bea4bba20908d08c7b`** clean, read 11:53 ET Sun |
| running services | **oms 890812 · strategy 890823 (06:27 ET Sun) · v2 893226 (06:30 ET Sun)** = code of `91a57a15` (the #1048 merge). Later merges: #1049 (code, NOT live until a restart), #1050/#1051 (ops cron, INSTALLED 11:51 ET). Ritual step 4 must RUN `git diff --name-only 91a57a15 origin/main` |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-28` · `EXPECTED_SHA=59f9532e…` · `EXPECTED_PID=893226`. **Sunday restart evidence 7/9** (REST warmup + BOOT-HOLD need session data) — codex re-runs the gate before 07:00 Mon |
| exposure | 11:52 ET: positions none, open orders 0, reconciler **0 critical** (WETO + MSGY stop fills written by codex on the operator's word; APUS 09-24 Webull share recorded as **operator-manual** close) |
| **flags on v2 (from `/proc`)** | `GAP_HOLD_ENABLED=true` (corrected detector, **first live session Monday**) · `ENTRY_WINDOW_END 15:45` · `OMS_V2_EOD_OCO_TRANSITION_ENABLED=true` (16:00 handover) · `ATR_MASSIVE_SEED_ENABLED=false` (parity gate 98.05% < 99.5%, operator ruling pending) · `RETRY_ONE=true` · `RTH_EDGE_BRACKET=true` (never exercised) |
| alerting | **Installed 11:51 ET:** six routine senders → low-priority topic `mai-tai-routine-…`; RED sub-alerts (bar-hole RED, seed-exposure RED, ENTRY CAP BREACHED / P0a) stay urgent; **20:00 ET digest daily** (first one Sun 20:00, zero-count) with sha guards that page on refusal; INC1 watch copy + crontab guards re-pinned; new pager sources: `schwab_opening_policy_reject`, `oco_exit_fill_unrecorded`, `webull_eh_ladder_unsold` |
| merges 09-24→09-27 | #1043 GAPHOLD age-from-close · #1044 policy rejects logged/cached/paged · #1045 LXEH storm · #1046 durable child fill before row close · #1047 16:00 handover guards · #1048 after-hours Webull ladder exits (bid-buffer limit) · #1049 seed-cap slot ownership · #1050/#1051 alert split + digest. Closed by ruling (no docs): #1036 #1037 #1039 |
| deploys | **09-24 09:04 ET** v2-only, GAP_HOLD off (rollback) · **09-26 19:02 ET** v2-only `063e0958` (#1043 #1045) · **09-27 06:27/06:30 ET** oms+strategy+v2 `91a57a15` + three env changes (operator's Sunday time-gate exception, Schwab positions 503 Sat 20:26–23:03 cleared first) |
| migration | `alembic_version = 20260916_0021`, unchanged |

## What to READ Monday 09-28 (owner · first read) — one marker per change, report UNPROMPTED

| change | first evidence to read | owner |
|---|---|---|
| **GAPHOLD corrected (#1043, flag ON)** | `[V2-GAP-DETECT-SKIP] SYM reason=no_live_bar_this_session` exactly once per name at 07:00; `[V2-GAP-DETECT]` ONLY with `last_bar_close_age_s>90` while prints continue; ⛔ Thursday's failure signature = detects at :30 every minute with age 90–95 s | claude-1 |
| **Policy rejects (#1044)** | first `Opening transactions… must be placed with a broker`: `[OMS-BROKER-REJECT]` line, one `schwab_ineligible_today` row, the RETRY-ONE second try DROPPED (`[OMS-INTENT-DROPPED] schwab_ineligible_cached`), **Webull 1-sh leg still placed** (operator: keep dual brokers), one page | claude-1 |
| **Entry cutoff 15:45** | `[V2-ENTRY-WINDOW-EXIT-ONLY]` at 15:45:0x, arms released, rests cancelled, zero fills 15:45–16:00 | claude-1 |
| **16:00 handover (#1047, ON)** | any position held at 16:00: `[OMS-V2-EOD-OCO-TRANSITION]` release at 16:00:0x (young-grace guard if a bracket filled 15:59:5x), floor carried if +5% printed during stand-down | claude-1 |
| **After-hours Webull ladder (#1048)** | a Webull share held past 16:00: fresh pair read → cancel → EH LIMIT under the bid; confirmation exits still refused after hours; 20:00 `webull_eh_ladder_unsold` page if unfilled | claude-1 |
| **Child fill before close (#1046)** | first Webull native stop fill: sell row in `fills` BEFORE the managed row closes; a failed read → row HELD, ladder silent, page `oco_exit_fill_unrecorded` | claude-1 |
| **LXEH storm (#1045)** | Monday overnight: no `[V2-FLIP-OWNER-RECOVERY]` repeating every 5 s on a removed name | claude-1 |
| **Alert split / digest** | routine senders on the low topic; RED sub-alerts urgent; 20:00 digest once (count + last line per sender) | claude-1 |
| **Pass mark (Webull exits)** | 5 sessions with ≥1 Webull exit and 0 dropped: **1 of 5** (09-25); 09-24 reset it | claude-1 |
| **#1049 (merged, NOT live)** | needs a weekday restart after 18:00; then the first same-session re-add | codex deploy · claude-1 read |

## Open decisions (operator)
1. **B4 Massive ATR seed** — reproduces the chart on PMAX (line 1.7842 @10:33, SELL 11:09 vs 1.2077/11:52 unseeded); parity gate 98.05% < 99.5%. Enable at 98% or keep the gate.
2. **B11 rebuilt-arm entries** — codex builds the per-trip classifier (option 3, sent); then the ruling: valid / invalid / mixed.
3. **Refusal response table** — count fixed (event_source client 40 vs broker 506 since 09-10), week gathered; build or drop (my view: drop).
4. Weekdays-only digest (currently daily) — follow-up if wanted.

## Board (Sun 11:53 ET) — statuses per the operator's vocabulary
| # | Item | Status | Owner | Next |
|---|---|---|---|---|
| 1 | GAPHOLD fix #1043, flag on | TO BE EXERCISED | claude-1 | Mon 07:00 |
| 2 | Policy rejects + cache #1044 | TO BE EXERCISED | claude-1 | first policy-blocked name |
| 3 | LXEH storm #1045 | TO BE EXERCISED | claude-1 | Mon overnight |
| 4 | Child fill before close #1046 | TO BE EXERCISED | claude-1 | first Webull stop fill |
| 5 | 16:00 handover #1047 | TO BE EXERCISED | claude-1 | 16:00 Mon if held |
| 6 | After-hours Webull exits #1048 | TO BE EXERCISED | claude-1 | first share past 16:00 |
| 7 | Entry cutoff 15:45 | TO BE EXERCISED | claude-1 | 15:45 Mon |
| 8 | Pre-open gate 9/9 | IN PROGRESS | codex | before 07:00 Mon |
| 9 | Pager delivery of new sources | TO BE EXERCISED | codex | first real incident |
| 10 | 09-18 naked-leg cures (#1014, LC1) | TO BE EXERCISED | claude-1 | until an uncancellable pair meets the fix |
| 11 | Pass mark 5 clean sessions | TO BE EXERCISED 1/5 | claude-1 | Mon close |
| 13 | This close-out | IN PROGRESS | claude-1 → codex reviews/promotes | — |
| 14 | SLOTCLEAR1 seed-cap ownership #1049 | TO BE EXERCISED (not live) | codex restart / claude-1 read | weekday restart |
| 15 | Momentum gateway #1029 | IN PROGRESS | codex | finish the measurement |
| 16 | Alert routing + digest #1050/#1051 | TO BE EXERCISED | claude-1 | Sun 20:00 digest; Mon traffic |
| 18 | Rebuilt-arm entries per-trip (B11) | IN PROGRESS | codex measure / claude-1 review | then the ruling |

Removed by ruling: dead-code removals (ATR re-arm, A2 backoff, TIMESALE, bracket realign), dark-flag enablement
(stand-down re-arm, RECLAIM1, EOD1601), ORB paper ATR gate, QUICK-FAIL, chase-down rule, sub-$1 Webull qty
(Webull needs 100 sh under $1; revisit when "stable"). Closed on evidence 09-27: the operator's eleven old
TO-EXERCISE rows, the 19-bug recurrence page (delivered for SLOTCLEAR1), ledger repairs, Schwab 503.

## Corrections I owe the record (09-24 → 09-27)
- PMAX 09-24: I said "2 sh Schwab + bracket" from the strategy log; Schwab had REJECTED (policy) twice, Webull 1 sh BARE.
- APUS 09-24: my 09-06 "ladder covers 16:00–20:00 on both brokers, nothing to enable" was wrong (Schwab needs #532's flag); my #1032 routing blocked the Webull floor exit after hours.
- WHLR 09-25: "missed +5% because of the seed cap" was wrong — in flip-owned mode a BUY flip places nothing; the rest-after-3-short-bars is the only producer.
- Flag audit: three "unruled" flags had rulings in the archive/memory/PR bodies.
- Re-pin: left a superseded record on the ledger after codex's rebase of #1047 (CI COULD_NOT_TELL); fixed the same night.
