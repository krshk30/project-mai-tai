# Session Handoff — ACTIVE (read this first)

> **This is the single entry point.** It stays small and current. Full dated history lives in
> [`handoff-archive/`](handoff-archive/) by month. To onboard an agent: *"Read `docs/session-handoff.md`."*
>
> **Structure:** blocker list + live ops state + recent activity here → deep history in the archives →
> design/reference docs linked throughout. Supersedes the monolithic `session-handoff-global.md` (kept as a
> frozen backup, to be retired once this structure is confirmed). The v2-isolated bot's deep history is
> [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md).
>
> **Maintenance rule:** new work appends to **Recent activity** below; monthly, roll entries older than ~2
> weeks into [`handoff-archive/<YYYY-MM>.md`](handoff-archive/). Keep this file under ~400 lines.

---

## 🎯 AUTHORITATIVE PATH-TO-SCHWAB-v2-LIVE-CREDENTIALS LIST (2026-06-15, operator-reconciled)

The single source of truth for what gates wiring v2 to a REAL Schwab account (rename `paper:`→`live:` +
provider=schwab + wire hash). v2 is structurally paper today (P1 Phase 1) — these clear BEFORE that step.

**🔴 GO-LIVE BLOCKERS (must clear):**
1. **Forward-test expectancy gate** — is v2 actually profitable? Sim-fills are IDEALIZED (no slippage/partials)
   = pipe validation, NOT a track record.
2. **Replay Phase 2 (measured-spread)** — the decisive realism input (does v2 survive real spread).
   ✅ **Tick-capture (#282) ACTIVATED 2026-06-15 19:57Z — the data clock is RUNNING**; accumulate ~a week+ of
   RTH ticks before Phase 2 has a useful sample. Design: [`v2-tick-capture-design.md`](v2-tick-capture-design.md).
3. **v2 entry-criteria rules settled** — ([[project-mai-tai-v2-entry-criteria]];
   [`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md)). **Narrowed to ATR-ONLY:** P1/P2 are
   idealized losers even gated (~26% win/7wk); schwab P3/P4/P5 are a separate retired-engine system not in v2.
   v2's credible path = **screened-ATR + the live exit ladder**. The **ATR fresh-flip qualifier** (atr_state_age<5)
   is BUILT + ENABLED + forward-testing — the load-bearing piece. Design:
   [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md).
4. **04:00 ET watchlist-staleness race — fix BEFORE credentials** (diagnosed 2026-06-16, NOT fixed). Bot
   watchlists carry yesterday's symbols + today's pick at the 04:00 boundary; harmless today (Polygon
   credential-less, v2 paper/sim), but **live+credentialed v2 could enter yesterday's stale symbols at the open.**
   Two-part design-first fix (ordering+hard-purge **and** 03:55 timing-separation). Full diagnosis:
   [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) → "04:00 WATCHLIST-STALENESS RACE";
   memory [[project-mai-tai-0400-watchlist-staleness-race]].
- ~~Scale/floor exit legs proven live~~ ✅ **SATISFIED 2026-06-15** (CUPR — two ATR-Flip round-trips ran
  SCALE_PCT2 + SCALE_PCT4_AFTER2 + FLOOR_BREACH, all simulated). Whole exit ladder live-proven.

**🟡 NON-BLOCKING (hardening / cosmetic / ops — clear opportunistically):**
- **P1 Phase 2** (broaden `paper:` hash-refusal to ALL paper accounts) + **dashboard-visibility half** (surface
  `[SCHWAB-TOKEN-*]`/`loop_health` as badges) — v2 already isolated (Phase 1); visibility worth having pre-live.
- slice-4 tier MACD/stoch exits · CAST/VSME flatten timing · bar_counts cosmetic · "Polygon tick" dashboard
  mislabel · `_evaluate_paths` triage (**momentum-bot engine, NOT v2 — not on the v2 critical path**;
  memory [[project-mai-tai-evaluate-paths-test-failures]]).
- **Parked:** polygon_30s credentials (needs tuning first).

---

## 🟢 CURRENT IN-FLIGHT / LIVE OPS STATE (as of 2026-06-16)

- **ATR fresh-flip qualifier: ENABLED live** (`…ATR_FLIP_USE_MAX_STATE_AGE=true`, ceiling 5), ATR-Flip only,
  P1/P2 untouched. Forward-testing the screened-ATR win% toward the ~63% idealized target (7wk: ~86% of screened
  are losers, ~63% of kept win — NOT 100%; spread-adjusted Schwab-tick P&L is the credential arbiter).
- **Forward-test watcher ARMED on the VPS** — `/tmp/atr_fwd_watch.py` → logs `/tmp/atr_fwd.log`; reports the
  first live screened-ATR entries at ~7:00 ET (v2 has NO time gate — dead-zone 0/0 — so it fires pre-market
  from ~7:00 ET, not 9:30). Flags any live fire with `atr_state_age ≥ 5` as **GATE-BROKEN**.
- **Baseline service PIDs (prove unchanged after any restart):** strategy **2104716**, OMS **2121312**.
- **Tick-capture retention:** prune-ticks timer now **--keep-days 30** (was 14); first effective deletion
  ~2026-07-15. Targets `market_*_ticks` only — does NOT touch bar history.
- **Backtest artifacts (VPS):** `/tmp/atr_secondary_bt.py`, `/tmp/atr_secondary_rows.csv`.
- **Deploy discipline:** PR + Validate mandatory, direct push forbidden; attended + explicit-GO before any
  live-money merge/restart; restart ONLY named services and capture PIDs. See
  [[project-mai-tai-multi-agent-deploy-rules]], [`vps-deployment.md`](vps-deployment.md).

---

## 🗓️ RECENT ACTIVITY (newest first — full text in [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md))

- **2026-06-16 — Handoff restructured** into this active doc + monthly archives (this change).
- **2026-06-16 — Secondary ATR qualifier TESTED & REJECTED** (rel_vol + below-VWAP floor). 7wk backtest
  (771 entries): on the age-kept set it screens MORE WINNERS than losers in all 15 configs; below-VWAP "tell"
  inverts (ATR-Flip is a below-VWAP bounce). Age-gate alone is the complete ATR screen. No second gate.
- **2026-06-16 — Age-gate VALIDATED vs real 06-15 trades** — 89% of screened are losers (incl. the 2 biggest),
  kept set 67% win, one winner wrongly screened (CUPR +2.10 = expected ~11% cost). Performs to 7wk profile.
- **2026-06-16 — 04:00 watchlist-staleness race DIAGNOSED** (go-live blocker #4; see blocker list above).
- **2026-06-15 — ATR fresh-flip qualifier BUILT + DORMANT-DEPLOYED** then enabled (#320). Design
  [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md).
- **2026-06-15 — Tick-capture (#282) ACTIVATED** (Replay-Phase-2 data clock running).
- **2026-06-15 — Two v2 entry-gating issues FIXED + LIVE-PROVEN** (warmup early-fire + DB-seed). Designs
  [`v2-atr-early-warmup-fix-design.md`](v2-atr-early-warmup-fix-design.md),
  [`v2-warmup-db-seed-fix-design.md`](v2-warmup-db-seed-fix-design.md).
- **2026-06-15 — v2 OMS EXITS ACTIVATED** (mid-session, attended) — exit ladder live on simulated.

---

## 📚 ARCHIVE INDEX (deep history — open only to dig)

| file | covers |
|---|---|
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | OMS exits, ATR qualifier, age-gate validation, 04:00 race, tick-capture (34 entries) |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine groundwork, regression battles (56 entries) |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | earliest — token-SPOF saga, early v2 scaffolding, streamer fixes (3 entries) |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2-isolated bot's own deep design/status history |
| `session-handoff-global.md` | frozen pre-split monolith (backup; to be retired) |

---

## 🔗 KEY REFERENCE DOCS (design-first / canonical)

- **Entry rules:** [`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md) ·
  [`schwab-1m-v2-atr-flip-entry-design.md`](schwab-1m-v2-atr-flip-entry-design.md) ·
  [`schwab-1m-entry-gates-extracted.md`](schwab-1m-entry-gates-extracted.md) ·
  [`v2-entry-gate-port-design.md`](v2-entry-gate-port-design.md)
- **ATR qualifier + warmup:** [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md) ·
  [`v2-atr-early-warmup-fix-design.md`](v2-atr-early-warmup-fix-design.md) ·
  [`v2-warmup-db-seed-fix-design.md`](v2-warmup-db-seed-fix-design.md)
- **Exits / ticks / pricing:** [`v2-exit-phase2-slice2-quote-bridge-design.md`](v2-exit-phase2-slice2-quote-bridge-design.md) ·
  [`v2-tick-capture-design.md`](v2-tick-capture-design.md) ·
  [`v2-reference-price-fix-design.md`](v2-reference-price-fix-design.md)
- **Resilience / ops:** [`schwab-1m-v2-loop-resilience-design.md`](schwab-1m-v2-loop-resilience-design.md) ·
  [`strategy-engine-main-loop-resilience-design.md`](strategy-engine-main-loop-resilience-design.md) ·
  [`vps-deployment.md`](vps-deployment.md)

## 🧠 MEMORY POINTERS (auto-load each session; listed for cross-reference)

[[project-mai-tai-context]] · [[project-mai-tai-0400-watchlist-staleness-race]] ·
[[project-mai-tai-v2-real-account-routing-risk]] · [[project-mai-tai-v2-entry-warmup-gate]] ·
[[project-mai-tai-v2-no-exits]] · [[project-mai-tai-v2-entry-criteria]] ·
[[project-mai-tai-schwab-bar-build-core]] · [[feedback-session-doc-and-memory-discipline]]
