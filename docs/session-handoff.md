# Session Handoff — ACTIVE (read this first)

> **This is the single entry point.** It stays small and current. Full dated history lives in
> [`handoff-archive/`](handoff-archive/) by month. To onboard an agent: *"Read `docs/session-handoff.md`."*
>
> **Structure:** status + open items + live ops state + recent activity here → deep history in the archives →
> design/reference docs linked throughout. The v2-isolated bot's deep history is
> [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md).
>
> **Maintenance rule:** new work appends to **Recent activity** below; monthly, roll entries older than ~2
> weeks into [`handoff-archive/<YYYY-MM>.md`](handoff-archive/). Keep this file under ~400 lines.

---

## 🚦 STATUS — v2 IS LIVE (2026-06-17, ATR-only, real Schwab account)

v2 went **live-credentialed** on **2026-06-17** as a **reasoned, operator-accepted risk** (profitability-after-spread
was/is still accumulating — see open items). Running config, ground-truthed from `/proc/<pid>/environ` + DB on deploy:

- **`broker_provider=schwab`, `account_name=live:schwab_1m_v2`**, real shared hash bound (the only `live:` Schwab key);
  `go_live_enabled=true`, `atr_only_mode=true` (P1/P2 disabled at two layers), qty 10, ATR fresh-flip qualifier on (age<5).
- **CYN is PROTECTED** — `MAI_TAI_PROTECTED_SYMBOLS=CYN` → `protected_symbol_set={CYN}` in the running config; the real
  account **holds 8000 sh CYN @ $2.57** (operator's manual position). 3-layer block + watchlist exclusion + #326. v2
  has never emitted/ordered/filled CYN (verified). `oms_managed_positions` CYN rows = 0 (bot does not manage it).
- **Rollback (tested):** `systemctl stop project-mai-tai-schwab-1m-v2.service` halts new entries instantly (OMS +
  market-data keep managing exits). Re-isolate to paper = `GO_LIVE_ENABLED=false` + `BROKER_PROVIDER=simulated` + restart.
  Env backup: `/etc/project-mai-tai/project-mai-tai.env.bak.pre-golive.20260617T003247Z`.

**What "live" has and hasn't proven yet:** the execution path is proven **to Schwab acceptance** (06-17: LNAI order
accepted by Schwab, working order, broker_order_id assigned). It is **NOT yet proven to a real FILL** — see open items.

---

## 🔴 OPEN ITEMS — DO NOT LOSE (future-you: read these)

1. **RESTART-WHILE-HOLDING is UNTESTED.** The 06-17 mid-session restart recovery test (below) was run **FLAT**. We have
   **not** verified how v2 recovers a restart **while holding an open position** — i.e. position reconciliation across
   restart, exit-ladder continuity, and whether the held position's exit metadata/floor/stops survive. **Test this before
   relying on restart safety during a live trade.** (Recovery-while-flat = ~17s, proven; while-holding = unknown.)
2. **✅ RESOLVED (confirmed 2026-06-17) — CI `validate` is GREEN again and can gate.** The JSONB-on-SQLite harness
   incompatibility (`market_trade_ticks`/`market_quote_ticks.raw` → JSON variant) + stale assertions that made every
   push red are fixed on main. **Proof: PR #333's `validate` ran fully green** (unit + integration/replay + ruff, 1m24s)
   — a branch off main could not pass if the ~150 JSONB CompileErrors were still present. Merges no longer *need*
   `--admin` to bypass red CI (admin-merge stays available). Keep running the targeted test file + ruff locally anyway.
3. **First real ATR FILL still PENDING.** 06-17 fired 4 live qty-10 ATR orders (08:05–08:17 ET): **NIVF/YMAT/EHGO REJECTED**
   by Schwab — *"Opening transactions for this security must be placed with a broker. Contact us"*; **LNAI** Schwab-**ACCEPTED**
   then **OUR-side CANCELLED** (`abandon_reason_code=SETUP_INVALID` — ATR setup reverted next bar). **All $0 filled, no
   position.** So the headline behavioral proof (real fill @ qty10 → managed exit) awaits a flip on an **API-eligible**
   symbol whose setup holds. Detail: [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) → 2026-06-17.
4. **Schwab API-open RESTRICTION narrows the live universe.** 3 of 4 06-17 names were Schwab-refused for API opening
   (foreign/manual-handling). A meaningful share of the momentum scanner's small-caps are likely un-openable via the v2
   live API path — the tradeable universe is **narrower than the scanner surfaces**. #326 now auto-evicts these.
5. **Profitability-after-spread — the open validation gate (now POST-go-live).** Still needs a real kept-win sample (not
   2 events) + Schwab-tick spread-adjusted P&L. Idealized sim-fills are pipe validation, NOT a track record. Replay
   Phase 2 data clock running since 2026-06-15 (#282); needs ~a week+ of RTH ticks.
6. **Exit-fill QUALITY — Phase 2 (resting-limit brackets) is the design-first follow-up.** Phase 1 (#333, below) made the
   OMS decide tick-by-tick within ms, but a market-order-on-decision still slips on a violent spike-and-collapse. Phase 2
   = pre-stage scale/floor/stop as **broker-resident bracket orders at entry** so fills execute at exchange speed,
   independent of any OMS reaction. Design-first (lifecycle: partial fills, cancel-on-other-exit, reconciliation across
   restart). Also: **`deploy_preflight` blocks every in-window OMS deploy on the protected-CYN holding** (CYN
   `position_quantity_mismatch` critical + "1 open position" + reconciler-degraded cascade — all benign) and its 5.0s
   HTTP timeout is too tight for the 5.4s `/api/overview` — whitelist `MAI_TAI_PROTECTED_SYMBOLS` + bump the timeout.
7. **⏸️ TIMESALE capture ENABLE is PENDING (attended, next-session open).** PR #335 (`59500bc`) added additive,
   capture-only TIMESALE_EQUITY (true trades) to the v2 streamer — **MERGED + DEPLOYED with the flag OFF (inert,
   byte-identical; v2 PID 2252021→2319110, clean)**. Why pending: our `market_trade_ticks` are LEVELONE quote-snapshots
   (throttled), NOT true trades; TIMESALE was never subscribed (0 rows); Schwab has **no historical T&S endpoint** so it
   must accrue LIVE. ENABLE = set `MAI_TAI_STRATEGY_SCHWAB_1M_V2_TIMESALE_CAPTURE_ENABLED=true` + restart schwab-1m-v2 —
   but do it ATTENDED at the next open (after-hours has no v2 watchlist → can't read the SUBS/entitlement; arming
   unattended risks a flap on the shared CHART_EQUITY streamer that feeds the live ATR bot; zero capture lost — first
   real trades are next RTH). Watch `[V2-WS-SUB]` (services incl TIMESALE_EQUITY) + any `[V2-WS-RESP-ERR]
   service=TIMESALE_EQUITY` (= not entitled) + reconnect/flap; flag-disable ready. Design: `docs/timesale-capture-design.md`.
8. **Tick-confirmation entry research (parallel track, NOT deployed).** After a setup bar, enter only if upticks>downticks
   in the next ~15s. HELPS P5 (6.4:1)/P1 (8.5:1), HURTS P4-burst. P4 is NOT a loss engine (+$7.11 2-day; the real bleeder
   is **P1 MACD-cross −$14.53**); mixed (P4-base + P1/P5-tick) = +$3.41. Tonight's ticks are LEVELONE-grade (see #7);
   DECIDER = a faithful 10-day TIMESALE test ~early July ([[project-mai-tai-tick-confirmation]]). Docs in `/home/trader/`:
   tick_confirmation_findings, combined_tickconfirm_2day, p5_3path_baseline_2day, p4_tickconfirm_optionB_plan,
   intrabar-execution-design, timesale-capture-design.

---

## ✅ CLEARED (was a go-live blocker)

- **OMS exit path is TICK-BY-TICK — FIXED (#333 `c79e8f5`, deployed live 2026-06-17 ~19:30Z).** Diagnosed off the live
  LNAI ATR-Flip trade: the +2% scale fired **~70s late at 4.345** (not ~4.45). Root cause (DB+code pinned): market-data
  had bids above +2% (4.43–4.46) for a ~14s window during the spike, but `sync_broker_state()` REST ran **inline every
  5s on the same loop that read quotes** → ticks backed up; AND the 5s staleness guard was blind because `received_at`
  was stamped at processing-time, not event-time. Fix = dedicated `_run_tick_consumer` task (market-data on its own task,
  never starved by broker-sync/intents) + last-quote-wins `_coalesce_ticks` + event-time staleness from `produced_at`.
  Behavior-identical for intents/sync/heartbeat + ladder logic; 57 passed/1 xfailed; deployed flat (only protected CYN
  held), clean (0 tracebacks). Design: [`oms-tick-consumer-design.md`](oms-tick-consumer-design.md). **Phase 2
  (resting-limit brackets) = open item #6.** True verdict still wants the next live intrabar spike on a v2 position.
- **04:00 ET watchlist-staleness race — FIXED (#324, deployed) + VERIFIED LIVE 2026-06-17.** At the 08:00 UTC / 04:00 ET
  roll: `bot day-roll fired` (08:00:00.654) → `scanner session-roll fired` (08:00:01.106) → scanner reset; v2 watchlist
  → count=0 with yesterday's 5 symbols UNSUBSCRIBED. **Zero stale symbols survived; no re-promotion race; no errors.**
- **Whole exit ladder live-proven** (2026-06-15, CUPR — scale/floor legs on simulated).
- **ATR fresh-flip qualifier MECHANISM** ✅ validated/complete (2026-06-16, live both directions).

---

## 🟢 LIVE OPS STATE (as of 2026-06-17)

- **Service PIDs (prove unchanged after any restart):** strategy **2299529**, OMS **2299517** (both rotated at the #333
  tick-consumer deploy ~19:30Z 06-17), v2 **2319110** (rotated at the #335 TIMESALE deploy ~00:3xZ 06-18, flag OFF/inert).
  *(Retired: v2 2252021 [#326], OMS 2207792 / strategy 2207786 [#333], pre-go-live 2104716/2121312.)*
- **#326 — Schwab-ineligible watchlist eviction: DEPLOYED + restart-verified 2026-06-17.** v2 now evicts symbols Schwab
  refused to open today (`schwab_ineligible_today`, per-account, 60s-cached) from its watchlist, so it stops *emitting*
  for them (the OMS already blocked *re-submission*; this halts the bot at the source — parity with the old schwab_1m
  bot). Proven on the fresh boot: scanner confirmed 6, v2 watchlist = 3 (CLWT/EHGO/YMAT evicted = exactly today's
  ineligible set). ⚠️ **Known ≤60s stale-carryover window at the 04:00 roll** (cache TTL not coordinated with session
  roll) — benign (over-conservative, self-corrects, 3h pre-trade); optional hardening = key the cache on session_date.
- **Mid-session RESTART recovery (FLAT) — measured 2026-06-17:** WS re-subscribe **~4s**; `state.bars` hydrated via
  DB-seed **~2s** (Fix-b) + REST warmup **~17s** (all `warmed=3/3`); buffered streamer bars drained. **Effectively blind
  ~17s, NOT the old ~135-min blackout** — DB-seed + REST warmup backfill the strategy buffer. (Supersedes the 135-min
  worst-case in [[project-mai-tai-v2-entry-warmup-gate]] for the DB-history case.) **Note:** the snapshot `bar_counts`
  telemetry resets to live-only on restart (≠ the eval buffer `state.bars`, which is the warm one).
- **Forward-test watcher** `/tmp/atr_fwd_watch.py` → `/tmp/atr_fwd.log` (flags any live fire age≥5 as GATE-BROKEN).
- **Go-live confirm captures (VPS):** `/tmp/v2_golive_cp1.txt` (04:00 roll), `/tmp/v2_golive_cp2.txt` (7AM session),
  `/tmp/v2_golive_firstfill.txt` (first-fill watch; transient timers `v2-golive-cp{1,2}`, watch fired + exited).
- **Tick-capture retention:** prune-ticks `--keep-days 30`; first effective deletion ~2026-07-15; `market_*_ticks` only.
- **Deploy discipline:** PR + Validate mandatory (CI `validate` GREEN again — open item #2; admin-merge still available),
  direct push forbidden; attended + explicit-GO before any live-money merge/restart; restart ONLY named services + capture PIDs.
  See [[project-mai-tai-multi-agent-deploy-rules]], [`vps-deployment.md`](vps-deployment.md).

---

## 🗓️ RECENT ACTIVITY (newest first — full text in [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md))

- **2026-06-18 — ORB opening-range path: entry thesis-check + multi-day EXIT-rule research (read-only).**
  New proposed P6 "OPEN" entry (5-min OR breakout-close + vol≥1.5× + VWAP/EMA, width<12%, cutoff 10:30).
  Full writeup: [`orb-opening-range-exit-research.md`](orb-opening-range-exit-research.md); engine
  [`scripts/orb_exit_backtest.py`](../scripts/orb_exit_backtest.py) (VPS `/tmp/orb_master.py`, cache
  `/tmp/orb_bars.pkl`). **Key data finding:** stored `schwab_1m_v2` bars are watchlist-gated → winners
  (CRVO 09:35, ATPC 09:51) promoted *after* breakout, so their 09:30 OR is missing → **must source from
  Schwab REST pricehistory** (validated vs Pine). Live prerequisite: scanner must surface candidates
  pre-09:30. **Exit sweep (7 days, 35 entries, RGNT-06-15 excluded):** the operator's **trailing-% hard
  stop BEATS the EMA/multi-layer exits on the robust metrics** — TRAIL-8% win 63% (vs C-2×EMA9 46%),
  TRAIL-3% median-capture 0.41 (vs 0.04), give-back 4–8% (vs 12.5%). **TRAIL-8% = best all-rounder.**
  C wins only on monster-driven total return. The **COMBO (TRAIL-8% OR 2×EMA9, first-to-fire) does NOT
  beat pure TRAIL-8%** — "whichever fires first" can only exit *earlier*, so it can't add C's
  monster-holding. Multi-layer "2-of-3" (E2) underperforms (ingredients correlated). **Gap-through caveat
  on the record:** trailing is a hard intrabar stop → exposed to gap-through slippage (cf. the CDT −3.7%
  incident); fills modeled at the stop are optimistic on thin books, so the trailing edge may erode live.
  **Leading candidate, NOT a verdict** (35 trades); 20–30-day extension in progress. Live needs the
  TIMESALE/intrabar layer.
- **2026-06-17 (late) — TIMESALE capture (#335) + tick-confirmation research day.** Audit found our trade ticks are
  LEVELONE quote-snapshots not true trades (Schwab has no historical T&S). Built additive capture-only TIMESALE_EQUITY
  (#335, merged + deployed flag-OFF/inert); **enable pending attended next-open (open item #7).** Also de-flaked 2
  time-dependent strategy-engine data-halt tests (trading-hours gate vs CI-run-time → frozen clock) to keep CI real-green.
  Research (NOT deployed): tick-confirmation per-path (P5/P1 help, P4 doesn't; P4 not a loss engine, P1 the bleeder;
  mixed +$3.41), ATR intrabar false-flip cost ≈ cancels benefit, intrabar-execution + timesale design docs. Open item #8 +
  [[project-mai-tai-tick-confirmation]]; docs in `/home/trader/*.md`.
- **2026-06-17 — OMS tick-by-tick exit consumer (#333) diagnosed → built → CI-green → DEPLOYED live.** From the live LNAI
  scale that filled ~70s late at 4.345: root-caused to quote-consumption lag (broker-sync REST inline-blocking the
  shared read loop) + processing-time staleness stamping; fixed with a dedicated `_run_tick_consumer` task +
  last-quote-wins coalescing + event-time staleness. Attended deploy (v2 flat, only protected CYN), OMS+strategy restart
  only, clean. Also confirmed CI `validate` is green again (open item #2 cleared). Phase 2 (resting brackets) = open #6.
- **2026-06-17 — #326 (Schwab-ineligible eviction) built → reviewed → DEPLOYED → restart-verified.** Ported the old
  schwab_1m bot's watchlist eviction into the isolated v2 bot (`_schwab_ineligible_symbols`, 60s cache, OmsStore loader,
  session_date parity with the OMS write-side). Merged `fe76f06`; v2 restarted; eviction proven on fresh boot.
- **2026-06-17 — Mid-session restart recovery profiled (FLAT):** ~17s blind, comes back with scanner-set-minus-ineligible.
  **restart-while-HOLDING still untested (open item #1).**
- **2026-06-17 — v2 GO-LIVE deployed (attended, reasoned risk).** PR #325 merged (`cbd1a09`); env set (ATR-only +
  go-live + `live:schwab_1m_v2` + schwab); strategy/OMS/v2 restarted clean; all gates verified. **Morning verdict:**
  04:00 roll/race-fix ✅; isolation ✅ (zero P1/P2, zero CYN); **4 ATR orders all $0** (3 Schwab-REJECTED foreign-restricted,
  1 LNAI accepted-then-our-cancel) → first real fill pending (open item #3); Schwab API-open restriction finding (open #4).
- **2026-06-16 — Go-live workstreams scoped; ATR qualifier MECHANISM validated; secondary qualifier rejected; age-gate
  validated; 04:00 race diagnosed; handoff restructured.** (Full detail in the archive.)
- **2026-06-15 — ATR qualifier built+enabled (#320); tick-capture activated (#282); warmup early-fire + DB-seed fixes
  live-proven; v2 OMS exits activated.**

---

## 📚 ARCHIVE INDEX (deep history — open only to dig)

| file | covers |
|---|---|
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | go-live + morning verdict + #326 + restart recovery, OMS exits, ATR qualifier, age-gate, 04:00 race, tick-capture |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine groundwork, regression battles (56 entries) |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | earliest — token-SPOF saga, early v2 scaffolding, streamer fixes (3 entries) |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2-isolated bot's own deep design/status history |
| `session-handoff-global.md` | frozen pre-split monolith (backup; to be retired) |

---

## 🔗 KEY REFERENCE DOCS (design-first / canonical)

- **Entry rules:** [`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md) ·
  [`schwab-1m-v2-atr-flip-entry-design.md`](schwab-1m-v2-atr-flip-entry-design.md) ·
  [`schwab-1m-entry-gates-extracted.md`](schwab-1m-entry-gates-extracted.md)
- **ATR qualifier + warmup:** [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md) ·
  [`v2-atr-early-warmup-fix-design.md`](v2-atr-early-warmup-fix-design.md) ·
  [`v2-warmup-db-seed-fix-design.md`](v2-warmup-db-seed-fix-design.md)
- **Go-live / race fix:** [`v2-paper-to-live-credential-transition-scoping.md`](v2-paper-to-live-credential-transition-scoping.md) ·
  [`v2-0400-watchlist-race-fix-design.md`](v2-0400-watchlist-race-fix-design.md)
- **Exits / ticks / pricing:** [`v2-tick-capture-design.md`](v2-tick-capture-design.md) ·
  [`v2-reference-price-fix-design.md`](v2-reference-price-fix-design.md)
- **Resilience / ops:** [`schwab-1m-v2-loop-resilience-design.md`](schwab-1m-v2-loop-resilience-design.md) ·
  [`vps-deployment.md`](vps-deployment.md)

## 🧠 MEMORY POINTERS (auto-load each session; listed for cross-reference)

[[project-mai-tai-context]] · [[project-mai-tai-0400-watchlist-staleness-race]] ·
[[project-mai-tai-v2-real-account-routing-risk]] · [[project-mai-tai-v2-entry-warmup-gate]] ·
[[project-mai-tai-v2-no-exits]] · [[project-mai-tai-v2-entry-criteria]] ·
[[project-mai-tai-schwab-bar-build-core]] · [[feedback-session-doc-and-memory-discipline]]
