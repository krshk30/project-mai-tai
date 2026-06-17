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
2. **CI `validate` is PERMANENTLY RED — it cannot gate.** Every push fails `validate` on a **test-harness incompatibility**,
   not real regressions: ~150 tests raise `sqlalchemy CompileError ... can't render element of type JSONB` (table
   `market_trade_ticks`, column `raw`) under the **SQLite** test DB (JSONB is Postgres-only), plus a few stale assertion
   failures (`ENVB`/`MASK`/`tos_intrabar`). **Consequence: all merges require `--admin` (standing auth), and CI provides
   NO safety net — a genuinely-breaking change could slip through.** Mitigation today = run the *targeted* test file + ruff
   locally before merge (e.g. #326 was verified `test_schwab_1m_v2_bot.py` 31-pass + ruff clean). **Real fix:** make those
   models render on SQLite (JSON variant) or skip-on-SQLite, so `validate` goes green and can gate again.
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

---

## ✅ CLEARED (was a go-live blocker)

- **04:00 ET watchlist-staleness race — FIXED (#324, deployed) + VERIFIED LIVE 2026-06-17.** At the 08:00 UTC / 04:00 ET
  roll: `bot day-roll fired` (08:00:00.654) → `scanner session-roll fired` (08:00:01.106) → scanner reset; v2 watchlist
  → count=0 with yesterday's 5 symbols UNSUBSCRIBED. **Zero stale symbols survived; no re-promotion race; no errors.**
- **Whole exit ladder live-proven** (2026-06-15, CUPR — scale/floor legs on simulated).
- **ATR fresh-flip qualifier MECHANISM** ✅ validated/complete (2026-06-16, live both directions).

---

## 🟢 LIVE OPS STATE (as of 2026-06-17)

- **Service PIDs (prove unchanged after any restart):** strategy **2207786**, OMS **2207792**, v2 **2252021**
  (v2 last restarted for #326 deploy at ~13:06 UTC 06-17). *(Pre-go-live baselines 2104716/2121312 are retired.)*
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
- **Deploy discipline:** PR + Validate mandatory (but see open item #2 — CI red → admin-merge), direct push forbidden;
  attended + explicit-GO before any live-money merge/restart; restart ONLY named services + capture PIDs.
  See [[project-mai-tai-multi-agent-deploy-rules]], [`vps-deployment.md`](vps-deployment.md).

---

## 🗓️ RECENT ACTIVITY (newest first — full text in [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md))

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
