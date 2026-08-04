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

## 🟢 FLEET — what is live right now

**As of 2026-08-04 EOD.** Deployed HEAD **`71c6c2c` (#645)** — ⛔ **UNCHANGED all day; nothing was
deployed 08-04.** `main` moved (#643 handoff, #646 design, #647 build, #649 runbook) but the box did
not. All five services active.

⭐ **Today was a MEASUREMENT day.** No code reached production. The output is a settled broker fact,
three merged PRs sitting dark, and **five corrected numbers — three of them corrected by the agent
that produced them.**

🔴 **TONIGHT — Gate 0.5, attended, after 18:00 ET.** Full runbook:
[`v2-premarket-exit-protection-rollout.md`](v2-premarket-exit-protection-rollout.md).

⛔⭐ **THE WINDOW IS AFTER 18:00 ET, NOT 16:30.** v2's entry window runs to 18:00, and **Bug 2 is
still live**: `cw_entries_this_flip` is in-memory and unpersisted, so a **v2** restart re-issues the
entry cap on every armed segment (the CPHI mechanism). ⛔ **The fleet-flat rule does not cover it** —
flat checks POSITIONS; the reset fires on ARMED SEGMENTS, which hold nothing. Proven live at 12:23
ET today: **fleet completely flat, 2 segments armed.**
✅ **But Gate 0.5 does NOT need a v2 restart** — #647 touches `oms/service.py`,
`broker_adapters/schwab.py`, `settings.py` only, all oms-risk; v2 is a separate unit. **Do not
restart v2 tonight.**

**Pre-flight is a runnable check, and it ASSERTS rather than prints** (the 07-15 lesson):
`sudo /home/trader/ops_preflight/preflight_v2_restart.sh` → exit 0 = GO. Gates: past 18:00 ET ·
zero armed segments · zero open managed rows · broker flat excluding operator manuals.

| service | state |
|---|---|
| `schwab-1m-v2` · `oms` · `strategy` · `control` · `market-data` | active |
| `trade-coach` · `orb` | inactive + disabled |

**Flags: unchanged.** #647's two new flags (`..._RTH_EDGE_BRACKET_ENABLED`,
`..._STAND_DOWN_CLEAR_REARM_ENABLED`) are **absent from the env ⇒ code default OFF**, verified.

---




## 🗓 TONIGHT — the ordered window

### A. PRE-FLIGHT (blocking · assert, don't print)
`sudo /home/trader/ops_preflight/preflight_v2_restart.sh` — exit 0 = GO, non-zero = WAIT.
⛔ Only needed **if** something restarts v2. **Gate 0.5 itself does not.**

### B. DEPLOY — attended, in order. ⛔ NOT scheduled; real-money deploys stay attended.
1. **Gate 0.5 — #647 (`9a193b1`)** per the runbook. Lands CODE, not behaviour: every flag stays
   **OFF**. Pull → **pre-restart bar-gap checklist** → account-flat → `stop strategy → restart oms
   → start strategy` → **post-restart bar-gap checklist**. ⛔ **restart `oms` only — not v2.**
   Write both new flags into the env explicitly (even as `false`) so their kill is a flip, never an
   append — the P0a lesson.
2. ⛔ **#366 IS STRUCK FROM TONIGHT — it is already deployed AND enabled, and it is not working.**
   Ground-truthed 08-04: its merge `3402756` is an ancestor of the deployed HEAD, and
   `MAI_TAI_SNAPSHOT_PERSIST_THROTTLE_SECS=1.0` is present in `/proc/<pid>/environ` for
   **strategy, oms AND control — control has carried it since Jul 14**. Yet
   `dashboard_snapshots` is **103 MB / 5002 rows and still growing**. "Built, never deployed,
   cheapest win available" was wrong on both counts. **Not a deploy — an investigation.**
   ⭐ Leading hypothesis *[inferred, not traced]*: a **1.0s** throttle against a **5s** snapshot
   cadence (`MAI_TAI_MARKET_DATA_SNAPSHOT_INTERVAL_SECONDS=5`) **never binds**. Configured ≠
   enforced, again.
3. Expect a **warmup-replay ARM burst** on any restart. Benign; the watch excludes it
   (`LIVE_ARM_MAX_AGE_SECS=300`) **upstream of every push condition**, so it cannot page.

### C. POST-DEPLOY — read-only, ✅ ALREADY SCHEDULED
`eod_cron.sh` fires **18:05 ET weekdays** (root cron, ET-guarded inside, fire-once per day) →
`/home/trader/entry_fix_watch/eod_<date>.txt` + an ntfy summary.
It re-takes **everything currently provisional**, on corrected denominators: live-arm crosses
(replay excluded), entries **by slot**, **resting fill rate PER LIVE ARM**, no-entry crosses, and
round trips in **percent, median-first, drop-one**, refusing to FIFO ambiguous pairs.
⇒ Then **P1** (below), which needs those numbers first.

### D. NOT TONIGHT — design-first, no deploys
Webull `CW_FLIP` fan-out fix · liquidity-floor hysteresis (⛔ constrained: keep the order MANAGED,
never "stop cancelling" — that recreates the #580 orphan) · Decision Tape #640 · **any trigger
change (waits on P1)**.

### E. OPERATOR
Review [#646](https://github.com/krshk30/project-mai-tai/pull/646) · #640 at leisure ·
⏰ **Wednesday 18:00 ET the Schwab re-auth reminder pages — 30 seconds, and it moves the token
expiry permanently off the Monday open** (next expiry **Mon 08-10 06:40 ET**, 19 min before the EH
window).

---

## 🔬 P1 — THE TRIGGER (the day's main open thread)

**`b117d89` re-shipped the rule of #467**, verbatim and unconditionally: the reactive/reclaim
trigger is `trig = state.cw_segment_high` — the **running segment high**, not the **frozen 2-bar
high** (`cw_trigger`). ⛔ Say "frozen 2-bar high", **never "flip+2"** — that shorthand reads as
flip × 1.02 and misled two readers into benchmarking against 2%.

⭐ **Structural proof, tape-independent — bucket 2 is EMPTY BY CONSTRUCTION.** Both fields seed to
`bars[-1].high` at the flip; `cw_segment_high` takes `max()` on **every** armed bar,
`cw_trigger` only while `cw_bars_waited < 2`, then freezes. `cw_trigger`'s bars are a strict
**subset** ⇒ **`segment_high ≥ cw_trigger` always**, and reactive cannot fire before
`bars_waited ≥ 2`. `max(segment_high, cw_trigger)` would be redundant, not protective. So it is
**#467 scoped to the reclaim slot** — the resting slot still uses the frozen 2-bar high.

**Why it matters regardless of today's tape:** `cw_segment_high` was measured **net-negative** in
the July port, rolled back by **#469 on a void justification** (its byte-identical test fed a break
*at arming*, where `segment_high == cw_trigger` by construction and could not diverge), and
**re-shipped 08-03 as a side effect of a different PR, with no backtest.**

⛔⭐ **AND THE CONVERSE — a normal count at the close is NOT evidence the rule is good.** July's
mechanism was that it *delays entries to a higher price*: that lands in entry **QUALITY**, not
entry **COUNT**. **Measure fill-vs-flip distance regardless of what the count does**, or we accept
a live rule on evidence that cannot see the failure it is meant to rule out — #467's mistake in a
new costume.

**The measurement (after the close, never during RTH — R&D CPU contends with the OMS loop):**
- denominator = **LIVE arms** · both buckets · the **2×2** (price touched the resting level while
  the order was off-book **×** price exceeded `cw_segment_high` — both can be true, so a split
  mis-assigns)
- ⚠️ **combined-commit caveat**: `b117d89` shipped the composition cap **and** the trigger together,
  so a raw before/after estimates the whole commit. Separate them or say "combined".
- ⭐ **natural control**: 07-30→08-03 = flap present, **old** trigger; 08-04+ = flap present, **new**
  trigger. The flap is held constant across the boundary.
- **fill-vs-flip distance as a first-class output.**

⛔ **n=1 settles nothing.** Even a final 12 against a median of 17 sits inside ordinary variance.
The measurement is the **multi-day** before/after.

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

## ⛔⭐ THE STAND-DOWN-CLEAR CONSTRAINT (binds open thread 11)

**Emitting a bracket is NOT sufficient. "OCO ⇒ churn-immune" is false.**

A native OCO bracket makes the BROKER own the exit, so timer-driven cancel/replace structurally
cannot happen — *while the bracket is live*. But when it resolves or stands down,
`[OMS-OCO-STAND-DOWN-CLEARED] ... OCO gone; ladder deferred` hands the exit **back to the bare
timer ladder** — which is KUST, now on a bracketed entry.

**The evidence it is real, not theoretical.** Cancelled/rejected sells within 60 min of an
**OCO-bracketed** entry: **NVVE 07-23 = 11**, KUST 07-22 = 6, FIEE 07-27 = 6, several at 3.
*(Caveat: symbol-level count in a time window; some sells may belong to another position that day.)*

⇒ **The requirement.** On stand-down-clear the exit must either **re-arm a bracket** or **inherit
the P0a marketable-hold** (`_managed_exit_refresh_exempt`, `oms/service.py:3770`). It must **never**
fall back to the bare refresh cadence. Any pre-market-OCO design that does not state which of those
two it does on stand-down-clear is incomplete.

⚠️ P0a alone does not close this: the hold engages only while `limit <= bid`. A bracket that stands
down while the exit is **not** marketable still lands on the plain ladder.

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

## 📜 HISTORY

- **What happened, day by day:** [`handoff-log.md`](handoff-log.md) (append-only)

| archive | covers |
|---|---|
| [`handoff-archive/2026-07.md`](handoff-archive/2026-07.md) | 07-16..07-25 — OCO build, wrong-bars root cause, resting flip-entry, EH trading, fan-out |
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | go-live, #326, OMS exits, ATR qualifier, 04:00 race |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | token-SPOF saga, early v2 scaffolding |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2 bot's own design/status history |

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
