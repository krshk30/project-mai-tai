# Session Handoff — CURRENT STATE (read this first)

> ## ⛔ HOW TO MAINTAIN THIS FILE — two verbs, never merge them
> 1. **OVERWRITE this file.** It answers one question: *what is true RIGHT NOW.* If a line here is
>    no longer true, **delete or rewrite it** — never append.
> 2. **APPEND to [`handoff-log.md`](handoff-log.md).** That is where *what changed today* goes.
>
> ⭐ **Why it is split (2026-07-29).** These two jobs used to share one file. Appending is easy and
> got done every day; overwriting requires noticing that something written two weeks ago is no
> longer true, and nothing forced it. Result: the narrative was always current while LIVE OPS STATE
> sat **twelve days stale**, and the file's own "keep under 400 lines" rule was structurally
> impossible. Keep them apart and staleness needs someone to actively write something false.
>
> **Target: ~150 lines.** If it grows, detail belongs in the log or a linked doc, not here.
> To onboard an agent: *"Read `docs/session-handoff.md`."* — this file alone should be enough to
> know what is live, what is open, and what to watch.

> **⛔⭐ OUTPUT CONSTRAINT (every study, no exceptions): report per-trade %, MEDIAN-FIRST, with a
> drop-one. NEVER a bare dollar total.** One VEEE at $25-29 notional outweighs sixteen $1-7 names
> and flips conclusions (violated twice 07-15 -> two wrong answers). This is the single line that
> saves the most rework. [[project_mai_tai_percentages_not_dollars]]

---

---

## ⚡ FIRST SCREEN — act on this alone

**Fleet: 5 services active. Broker FLAT except `CYN 5000` (operator manual, never touched).**
Deployed HEAD **`b222b36`** (#657 merged 2026-08-05 21:01 ET, verified on the BRANCH).
**`SESSION_TIME_ROLL_ENABLED=true`** — the time-driven 04:00-ET roll is LIVE.

## ✅ DEPLOYED 2026-08-05 21:01-21:07 ET (attended, operator override)

Full sequence, 0 errors, no bar hole (none was possible — EH ended 20:00, zero bars due).
Pre-flight ran **before BOTH restarts** and the second one earned its place: between restarts the
DB-seed replay **re-armed 4 symbols** (Bug 2, live, in exactly the gap that check exists for).

```
#1  [OVERRIDE] 7 ARMED accepted by OPERATOR: AXTL CLRO FUSE GTE HYFM INLF ZYBT
#2  [BLOCK]    4 ARMED: CLRO GTE INLF ZYBT      <- the replay re-armed these
#2b [OVERRIDE] 4 ARMED accepted by OPERATOR: CLRO GTE INLF ZYBT
boot line: [V2-SESSION-ROLL] boundary_crossed=True rolled=0 symbols=- (watchlist=6 armed=4)
```

⛔ **THE BOARD IS RESTART-PROVEN, NOT D1a-PROVEN.** FUSE/HYFM/AXTL cleared because of the
**restart**, which a bare restart would also have done. `rolled=0` is the CORRECT answer — all 4
armed segments came from today's bars, so nothing was stale — which makes the boot line
**proof-of-life, not an exercise**. D1a is live and **UNEXERCISED**.

**NEXT ACTION — TOMORROW after 04:00 ET: verify D1a, and say WHICH of two things happened.**
1. `grep V2-SESSION-ROLL /var/log/project-mai-tai/schwab-1m-v2.log*` — read **rolled=N** for its
   value, not its existence.
2. **rolled>0 on a symbol that went silent ⇒ VALIDATED.** That is the only outcome that counts.
3. **No silent candidate existed ⇒ say UNEXERCISED, not validated.** The flag having been on all
   day proves only that it broke nothing.
⛔ A clean `cw_armed_segments` proves the restart. Do not report it as the fix working.

### The honest ledger — read before quoting any finding here
**Demonstrated money cost of 2026-08-05's findings is ~ZERO, and EIGHT claims were withdrawn**
(full table: [`handoff-detail-2026-08-05.md`](handoff-detail-2026-08-05.md)). The operator's own read
-- *"everything I bought closed cleanly, nothing pending"* -- was correct, and correct against three
alarms raised that day.
**The case is NOT "it is bleeding money." It is: the books and the broker disagree, and size cannot
be scaled until they don't.** Overstating it once costs more than the whole board is worth.
⛔ **Never quote a P&L number from `<day>.jsonl` alone** — it holds only RTH, `-ocoexit-` exits. Union it with `<day>.unpaired.jsonl`, and say which returns are asserted vs candidate.
⚠️ The "12% attribution / 16 entries -> 2 round trips" figure was an **`eod_counts.py` artifact** (it groups fills per symbol-day and drops any symbol with >1 fill per side). It is not a real coverage number and should not be requoted.

**Survives with evidence:** 24 blocked hard-stop episodes / ~12 days, 4 never closing same day
(real, small at qty 2) - and a books-vs-broker divergence dated 08-05 (GTE's 14:54 lot exited with
no `fills` row and no trade record; cost ~$0.95).

### BOARD -- sized, not run
| item | note |
|---|---|
| **ATTRIBUTION — ✅ ANSWERED 08-05, it was NOT a coverage problem** | Capture is **100%**: `<day>.jsonl` (36, `-ocoexit-`, attribution ASSERTED) + `<day>.unpaired.jsonl` (9, `-close-`, `close_candidate_ret_pct` accurate to **0.0004 pts**). ⛔ **The bias is in the READER** — 3 of 3 consumers read paired-only. ⛔ **Zero native-OCO exits exist in extended hours** (Schwab refuses a STOP leg there), so the paired file contains **no EH exits at all** and degrades as pre-market volume grows (close route: 1,1,0,2,1,**5**,**8**). Remediation = union both files in `open_capture_0731.sh`, `live_trade_tape*`. |
| **A2 reverse-reject defer** | 393/394 on the managed-exit ladder, no defer handler. 384 are CW_HARD_STOP -- deferring lengthens the naked window; acceptance must measure **trigger->fill**, not reject count |
| **marginal seed-cap distribution** | the one item whose payoff is **recovered trades**; 08-05 caps spanned 1 -> 648 min |
| **third-class recency** | "we hold it, the broker says flat" -- fresh instance 08-05 |
| **A1b · GTE bar accounting · `dangerous=false`** | `dangerous` derives from a retired counter => **P1.3 boot-hold release is UNVALIDATED** |
| **lingering rows** | hop one **NEGATIVE** (entry path unaffected). Real consequence: a stale row keeps `_managed_v2_symbols` armed => the exit ladder works a gone position. Belongs to the A3 / stand-down family |

---

## ⛔⭐⭐ THE BUG CLASS OF THE WEEK — lead any write-up with this
**A signal authoritative for job A is NOT authoritative for job B.** Five instances in two days:

| signal | legitimate job | illegitimate reuse | cost |
|---|---|---|---|
| `_managed_v2_symbols` | quote guard | exit-poll **work-list** | phantom rows, blocked fan-out |
| **volume floor** | **admission** | **re-check** | flaps the resting order off the book |
| `[V2-CW-ARM]` lines | event record | **cross census** | denominator inflated **~14×** |
| `[OMS-V2-MANAGED-EXIT]` lines | event record | **exit census** | 2% should have been ~12% |
| **placement intents** | order-lifecycle record | **cross denominator** | #625 altered the very denominator it was judged on |
| a **wildcard match** (`ILIKE '%HARD_STOP%'`) | finding a family | **defining** one | swept a 1-reject/episode refusal into a 41-reject/episode storm class |
| **in-memory flags** (`cw_entries_this_flip` · `_managed_v2_symbols` · `_cw_floor_armed` · `_cw_flip_pending`) | a within-tick guard | **durable-looking behaviour** | 14 such structures in the OMS; ~9 gate a money decision |

⇒ **the managed-exit log is not an exit census** — Schwab exits mostly resolve broker-side via
native OCO and never emit a line (`OMS-OCO-EXIT-FILL` = 1174 lines / 6 days). **Any earlier study
that counted Schwab exits, crosses, or segments from log lines carries the same hole.**

## ⛔ NO INTRADAY COUNT IS A RESULT UNTIL THE SESSION CLOSES
Burned twice today, both already reported before being caught:
*"19 resting placements / ZERO fills"* (2 had filled by 10:46) · *"tape collapsed 17 → 2"*
(**6 by 12:05**). ⇒ **date-and-time-stamp every mid-session figure and mark it PROVISIONAL.**

**Provisional until the 18:05 report lands:** the composition-cap "0 breaches" reading · every
08-04 no-entry rate · the day's round-trip P&L.

---

## 👀 WATCH NEXT SESSION

1. **The 18:05 EOD report** — it converts today's provisional readings into results. Read it before
   quoting any 08-04 number.
2. **`[OMS-V2-POLL-REENROLL]` has still NEVER fired** — on any day. Fix 2's real path remains
   unexercised. Repeated fires would mean the leak is live and self-healing is masking it.
3. **P0a is still unvalidated** — two consecutive fastfills (41 ms 08-03, **25 ms** 08-04 AAOG),
   both correctly INCONCLUSIVE, `churn=0`. EH exits are marketable limits priced off the bid, so
   they may **never** engage the hold organically ⇒ the **A3 forced stand-down** is the realistic
   route. ⚠️ A thin tape starves its sample along with everything else.
4. **Gate 1 is OPPORTUNISTIC and blocks Gates 2/3** — it needs a v2-held **Schwab** long during
   **RTH** with **unreserved** shares that are **ours** (⛔ not CYN). It cannot be done tonight.
5. **Webull `CW_FLIP` fan-out gap** — the leg is deaf to the flip exit (**27 arms vs 0**, **7 emits
   vs 0**). ~**12%** of Schwab exits by distinct symbol-day. ⛔ Keep the three claims separate:
   **mechanism certain · frequency measured · cost n=2.**
6. **KUST manual stop** — status not re-verified since 07-31; it is not in the env file (manual
   stops are DB-held). Confirm before assuming KUST is tradeable.


## 🔴 OPEN THREADS (detail: [`handoff-open-items.md`](handoff-open-items.md))

1. **Backtest-vs-live parity on a STABLE-CODE day** — still unproven end-to-end. 07-30 unusable.
2. **polygon/strategy-engine freeze — HALF CLOSED.** The trade-coach half is resolved (see log).
   The JSON-snapshot-encode freeze is not; #366 remains undeployed.
3. **Schwab API-open rejects ~3/day and nothing evicts** — ~20 lost entries last week, a different
   symbol each day. `#326`'s eviction does not fire on this reject reason.
4. **An unnamed suppression stops a rejected-symbol retry** — risk PASSES, no broker order is
   created, the intent is marked rejected, nothing is logged. Currently protective; mechanism
   unidentified. Same shape as #580/#608, which both cost money.
5. **A Schwab rejection vetoes the Webull leg too** — the fan-out itself works (proved on
   APLX/SNDG); but a name Schwab refuses via API is traded on NEITHER broker.
6. **Order churn 284 orders -> 23 round trips (12:1)** — resting-entry reprice churn. Invisible to
   the recorder, fully visible on the live tape. Understand before the coach redesign.
7. **Reconciler severity is INVERTED — an UNOWNED position pages CRITICAL.** A hand-bought AZIO
   (972 sh, `live:orb`, zero orders/fills/bars of ours) paged RED. `virtual_quantity == 0` is the
   *definition* of "not ours", and it forces `critical`; a real drift on a position we own is only a
   *warning*. The payload already computes `strategy_codes: []` and discards it. ⛔ PROTECTED_SYMBOLS
   gates the OMS, a *different* list gates the reconciler. ✅ OMS never touched AZIO.
8. **Redis evicts the HEARTBEAT stream ⇒ false "oms-risk fleet down" RED page.** `maxmemory 512 MB`
   + **`allkeys-lru`** + `snapshot-batches` at **180 MB in 26 entries** ⇒ the 47 KB heartbeat key gets
   dropped and the watchdog reports a zombie. OMS was healthy throughout (0 log gaps >60s).
   ⛔ **Do NOT fix by cutting `snapshot_batch_stream_maxlen`** — 180 is load-bearing (the scanner
   warmup prefill needs **120** batches); cutting it blinds squeeze detection ~10 min per restart.
9. **⭐⭐ SELECTION — we buy stocks whose move is already SPENT (scanner AND bot).** AXTU 07-31 was
   **+54.5%** before we touched it; range then compressed 8.4%→5.1% while median bar volume fell
   4,388→1,200, and we bought it **three times** during that decay. Operator wants names that still
   OSCILLATE ("down, up 20%, down"), not exhausted ones. ⛔ The vol floor can't fix it: it samples
   **one** bar (AXTU armed off a 10,467 bar, filled into 2,999; median 2,217, only 17% of bars clear
   10,000). ⛔ **DISCUSS BEFORE BUILDING.**
10. **IRE: a Schwab REPLACE spawned an order we never recorded** — our books said 2, broker said 4.
   We never issue a replace. Parked by operator decision: catch it live next time (#626 now
   surfaces the drift in ~8 min instead of hours).
11. **⭐✅ PRE-MARKET OCO HOLE — SETTLED, BUILT, MERGED DARK (08-04).** Probe P measured the
   answer preview-only, zero risk: Schwab rejects a STOP leg in the EH session — *"This order type
   is not available for this session"* — single leg, entry bracket **and** exit-only OCO, with
   `session=NORMAL` controls accepting. ⇒ **native pre-market protection is structurally
   impossible**; 09:30 is the BROKER's earliest arm point. #646 = design, **#647 merged
   (`9a193b1`)** = build, all flags OFF. ⛔ Still owed: the **STEP-1 shape proof** against a real
   held position (Probe P's control rejected on position, so the SHAPE is unproven) and the
   **not-marketable-at-stand-down** pricing rule. See [[project_mai_tai_premarket_exit_protection]].

✅ **CLOSED today: VPS retention** (DB 12->10 GB, logs 1.7 GB->426 MB, logrotate installed).

---

## 🔔 ALERTING — what reaches the phone

| watch | fires on |
|---|---|
| `bar_gap_watch_cron.sh` | a hole in the v2 bar series — **and auto-repairs the DB**, then reports what it filled |
| `reconcile_alert_cron.sh` | any reconciler **critical** finding, fingerprint-deduped (7/day, not 1149) |
| `entry_fix_watch/watch_cron.sh` | **NEW 08-04** — 5 sections; pushes on first live cross, composition breach, POLL-REENROLL, P0a validated/churning, or the checker itself failing. ⛔ **silence is NOT green — read `STATUS.txt`** |
| `entry_fix_watch/eod_cron.sh` | **NEW 08-04** — end-of-session counts at **18:05 ET**, the figures allowed to be called results |
| existing | OMS liveness, pre-open readiness, token expiry, OCO capture, orphan orders |

⛔ All are ROOT crontab, guarded in ET **inside the script** (`CRON_TZ` is ignored on this box).
⛔ **A script committed from Windows lands mode 664 AND carries CRLF** — both make it silently never
run. Verify `stat -c %a` and `bash -n` **on the box**. Hit both today.

---

## 🔗 KEY REFERENCE DOCS (design-first / canonical)

- **Entry rules:** [`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md) ·
  [`v2-fresh-flip-since-confirmation-design.md`](v2-fresh-flip-since-confirmation-design.md) **(NEW 07-30)** ·
  [`schwab-1m-v2-atr-flip-entry-design.md`](schwab-1m-v2-atr-flip-entry-design.md)
- **ATR qualifier + warmup:** [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md)
  ⛔ *its gate is implemented in DEAD CODE and the flag is off — enabling it gates nothing*
- **Resilience / ops:** [`schwab-1m-v2-loop-resilience-design.md`](schwab-1m-v2-loop-resilience-design.md) ·
  [`vps-deployment.md`](vps-deployment.md)

## 🧠 MEMORY POINTERS (auto-load each session)

[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project-mai-tai-restart-bar-gap-checklist]] **(READ BEFORE ANY RESTART)** ·
[[project-mai-tai-orb-window-defers-entries-to-1000]] · [[project-mai-tai-trade-coach-dead-retry-storm]] ·
[[project-mai-tai-oms-scoping-invariant]] · [[feedback-be-crisp-no-essays]] ·
[[feedback-session-doc-and-memory-discipline]]
