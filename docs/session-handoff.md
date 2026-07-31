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

**As of 2026-07-31 EOD.** HEAD `24f2046`. **5 PRs shipped today (#631–#635), 3 deployed.**
Flat of everything ours; only the operator's manual CYN 5000 remains.

⭐ **Today was an EXECUTION day, not a strategy day.** A live KUST loss (−5.17% on a signal that was
right) root-caused to our own exit loop, and the chain that fell out of it. See
[`handoff-log.md`](handoff-log.md) 07-31.

🔴 **P0a is DEPLOYED but NOT VALIDATED.** Kill switch one command away:
`MAI_TAI_OMS_HOLD_MARKETABLE_MANAGED_EXIT=false` + `stop strategy → restart oms → start strategy`.
It needs a **pre-market / no-OCO software-ladder exit** to validate — FCUV cannot (native OCO exits
through the broker bracket, not the software refresh path). **Tomorrow's pre-market is the test.**

🛑 **KUST is on `global_manual_stop_symbols`** — clear with `/scanner/symbol/resume?symbol=KUST`
once you are happy the exit path is fixed.

| service | state | note |
|---|---|---|
| `project-mai-tai-schwab-1m-v2` | active | real money (+ Webull fan-out leg) |
| `project-mai-tai-oms` / `strategy` / `control` | active | |
| `project-mai-tai-trade-coach` | **inactive + disabled** | ⛔ do NOT restart — see below |
| `project-mai-tai-orb` | inactive + disabled | last ran 07-23 |

⛔ **Schwab holds the OPERATOR'S MANUAL positions: CYN 5000 sh @ $2.5057 and TE −3000 sh @ $3.9976.**
Not ours. Verified zero orders/fills/intents/bars for both. **Both are now in
`MAI_TAI_PROTECTED_SYMBOLS=CYN,CELZ,TE`** (TE added 07-30, ground-truthed from `/proc/<oms-pid>/environ`).

**Live flags (v2 + OMS):**

| flag | value |
|---|---|
| `..._ATR_FLIP_VOL_FLOOR` | **10000 — SETTLED 07-30.** Production always ran 10000; the "keep 5K" instruction was given believing production was 5000. #588 aligned the code default. Do not re-litigate. |
| `..._ATR_FLIP_QUANTITY` / `..._WEBULL_FANOUT_QUANTITY` | 2 / 1 — ⛔ `..._DEFAULT_QUANTITY=10` is a DECOY, not the live path |
| `..._CW_V2_RESTING_ENTRY_ENABLED` / EH variants | true |
| `..._CW_V2_RECLAIM_ENABLED` / `_GAP_BARS` | true / 1 |
| `..._DUAL_BROKER_FANOUT_ENABLED` | true |
| `OMS_NATIVE_OCO_EXIT_POLL_ENABLED` / `..._RECORD_..._FILLS` | true / true — ⛔ the poll DOES fire (proved on FCUV); it silently missed AXTU/AXTX for 26–90 min and the cause is still unproven |
| `OMS_HOLD_MARKETABLE_MANAGED_EXIT` | **true (NEW 07-31, P0a)** — a working managed exit is HELD while its limit is still marketable instead of cancel/replaced on the refresh cadence. **KILL SWITCH.** |
| `..._ATR_FLIP_USE_MAX_STATE_AGE` | false — ⛔ and its gate sits in DEAD CODE; enabling it would gate nothing |
| `ORB_ENABLED` | true — ⛔ the BOT is dead; the flag seeds the `live:orb` broker account the fan-out needs |

**Entry rules now:** a symbol newly confirmed by the scanner must wait for a flip that occurs AFTER
it joined the watchlist (per-symbol watch-start, #618). Symbols held since the open are exempt.
**The 09:30-10:00 ORB window is REMOVED** — v2 now trades the open for the first time (~3 extra
entries/day at open volatility).

---

## 👀 WATCH NEXT SESSION

1. **#628 is DEPLOYED BUT NOT VALIDATED LIVE.** No cancel intent occurred after the 15:39 deploy.
   Check at the open: **no cancel intent should linger at `accepted`.**
2. **v2 trading the 09:30-10:00 open is NEW.** Watch the first hour deliberately.
3. **`dashboard_snapshots` regrew 14 MB -> 96 MB in FOUR MINUTES** after VACUUM FULL. That is
   `_replace_dashboard_snapshot` on the hot path — the same write that is 72% of the polygon freeze.
   ⭐ **#366 is built, never deployed, and is now the root of two open items.** Cheapest win available.
4. **07-30 data is NOT usable for exit study or parity** — 11 deploys + bar holes from deliberate stops.

---

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

✅ **CLOSED today: VPS retention** (DB 12->10 GB, logs 1.7 GB->426 MB, logrotate installed).

---

## 🔔 ALERTING — what will now reach the phone (all ntfy, all NEW today)

| watch | fires on |
|---|---|
| `bar_gap_watch_cron.sh` | a hole in the v2 bar series — **and auto-repairs the DB**, then reports what it filled |
| `reconcile_alert_cron.sh` | any reconciler **critical** finding, fingerprint-deduped (7/day, not 1149) |
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
