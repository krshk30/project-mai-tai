# OMS SPOF P3 fast-follow — wholesale off-loop of the remaining blocking-DB surface

**Status:** DESIGN-FIRST (no code until operator approves). Fast-follow to #391.
**Author:** session 2026-07-07. Grounded in a read-only code map of `/home/trader/project-mai-tai` (all `file:line` below are from the running tree).
**Class being closed:** synchronous Postgres I/O on the OMS asyncio event loop → a stalled DB connection freezes the whole OMS (the 07-01 / 07-02 zombie, 2× in 12h). #391 bounded the *incident method*; this closes the *class*.

---

## 1. What #391 already did (baseline)

- **Fix 1 — timeouts:** `build_oms_session_factory` (`db/session.py:58`) wires libpq `statement_timeout=5s` / `lock_timeout=3s` / `connect_timeout=5s` / `pool_timeout=5s` / `pool_recycle=1800s`, gated `MAI_TAI_OMS_DB_TIMEOUTS_ENABLED` (default true, `settings.py:548-554`). A hung `psycopg wait` can now no longer block *forever* — worst case ~5s.
- **Fix 2 — `_run_db` off-loop helper** (`oms/service.py:166`): runs a **pure-sync session unit** on a worker thread via `asyncio.to_thread`. Applied to only **4 sites**: `sync_broker_positions` phase-1 load (`:1501`) + phase-3 persist (`:1528`, the flush that zombied), and two native-stop-guard dedup checks (`:1454`, `:1907`).
- **Fix 3 — hard-stop decouple** (proven by test `test_hard_stop_fires_when_preclose_position_sync_hangs`).
- **Fix 4 — control-loop hardening.**

**The contract that makes `_run_db` safe (load-bearing):** *only the DB session unit runs on the worker thread.* Broker `await`s and all mutations of in-memory OMS state stay on the loop thread. Reason — see §3.

---

## 2. The map: what is still synchronous-on-loop (ground truth)

There is **no locking anywhere in the OMS** (`grep Lock/Semaphore/threading` in `oms/` → nothing). All concurrency safety today is "single loop thread, run-to-completion." That fact drives the entire design.

### 2a. 🔴 The finding that reframes this work: **two committing DB sessions run *directly on the tick/quote hot path*** and #391 never touched them

`_handle_quote_tick_event` (`service.py:1921`) — invoked from the tick consumer on **every quote tick** — synchronously calls, on-loop, with its own committing session:

| site | method | opens session | commits | also awaits broker? |
|---|---|---|---|---|
| `service.py:2993` | `_cancel_drifted_working_orders` | `:2993` | `:3040` | yes (cancel) |
| `service.py:1237` | `_evaluate_v2_managed_exit` | `:1237` | `:1318` | yes — `_emit_v2_managed_sell` |

**These sit inside the exact loop #391 set out to protect.** A DB stall here freezes the tick consumer → the OMS stops reacting to quotes → stops don't evaluate. They are arguably a **higher-priority target than the two methods named in the ticket**, because they fire at tick frequency, not at intent/control-loop frequency. **Recommendation: fold these two into scope as P3-item-0.**

### 2b. `process_trade_intent` (`service.py:407`) — the ticket's item A

One on-loop session held open `:408 → commit :693`. Interleaves **3+ broker `submit_order`/refresh `await`s** through the body (`:505, :577, :648, :662, :683`), and the reads that size the order (`get_virtual_position`/`get_account_position`/reserved-qty) must complete *before* the submit. **It cannot be one `to_thread`** — it needs `sync_broker_positions`-style phase-splitting (DB-read phase off-loop → broker phase on-loop → DB-write/commit phase off-loop), with several interleave points.

**⚠️ It is literally on the hard-stop trigger path:** `_trigger_hard_stop` (`:2051`) calls `process_trade_intent` directly at `:2111`, and `_trigger_hard_stop` runs *inside the tick consumer* via `_evaluate_hard_stop_market_event`. **Any latency added to `process_trade_intent` directly delays the protective close → the #391 stop-decoupling proof is mandatory here.**

### 2c. `sync_broker_orders` (`service.py:1535`) — the ticket's item B

One on-loop session `:1536 → commit :1778`, with a broker `await fetch_order_update` **per open order** (`:1585`) interleaved with heavy per-order DB writes (`record_fill_if_needed`, `apply_fill_to_positions`, `append_order_event`, `mark_intent_status`) plus more `submit_order` awaits in `_manage_native_stop_after_fill`/`_rearm_native_stop_from_registry`. Hardest to phase-split (N interleave points).

### 2d. Cold-path inventory

- `oms/store.py`: **30 methods, 59 `session.*` ops**, all take a passed-in `session` (they never open their own) → each is already a clean pure-sync unit-of-work body, ideal `_run_db` payloads.
- `service.py`: **81 `self.store.*` call sites**, ~40 direct `session.*` lines. Independent session-opens beyond the hot ones: `_rehydrate_managed_v2_symbols` (`:1166`, **startup-only, sync**), `_terminalize_orphaned_active_intents`, `_target_order_for_cancel_intent`, `_intent_setup_invalid_reason`.

---

## 3. The core hazard, and the design rule that avoids it

**Hazard:** the naive reading of "off-load `process_trade_intent`/`sync_broker_orders`" is "wrap the whole method in `to_thread`." That is **unsafe**: both methods mutate plain dicts/sets that the tick/stop path also reads and writes on the loop thread — `_armed_hard_stops` (written by `sync_broker_orders` callees `:2233/2266/2292…`, read by the stop path `:1962/2114`), `_managed_v2_symbols` (`:1128/1152` vs `:1242/1286`), `_latest_quotes_by_symbol`/`_latest_trades_by_symbol`. Running the method body on a worker thread while the loop thread touches the same structures = **a real data race that does not exist today**.

**Design rule (non-negotiable):** we do **not** move method bodies off-loop. We move **only DB session units** off-loop via `_run_db`, exactly as #391's contract requires. Method orchestration — broker `await`s, dict mutations, decision logic — stays on the loop thread. "Wholesale off-loop" therefore means **refactor each target method into explicit phases**: pure-DB units (→ `_run_db`) separated from on-loop broker/state steps. This is the `sync_broker_positions` pattern (phase1 DB / phase2 broker / phase3 DB), applied more granularly.

This keeps the single-writer-per-dict invariant intact — no new locks, no new races — while getting every blocking `session.*` off the loop.

---

## 4. Proposed scope & sequencing (each its own PR, each independently deployable)

Ordered by *risk-reduction-per-unit-risk*, safest and highest-value first:

**PR-A — tick-path off-load (P3-item-0, NEW, highest priority).** Phase-split `_cancel_drifted_working_orders` (`:2993`) and `_evaluate_v2_managed_exit` (`:1237`): DB reads/writes → `_run_db` units; keep the `_emit_v2_managed_sell`/cancel broker awaits and any dict mutation on-loop. **Touches the hot path → stop-decoupling proof required (see §6).** Biggest freeze-risk reduction because these fire per-tick.

**Broker coverage of PR-A (verified against `_handle_quote_tick_event`, `:1921`):** the quote-tick handler runs three things in order — (1) `_evaluate_hard_stop_market_event` (**pure in-memory, no DB** — the ORB trailing stop + v2 hard-stop *decision*; not a freeze source), (2) `_cancel_drifted_working_orders`, (3) `_evaluate_v2_managed_exit` (gated `symbol in _managed_v2_symbols`).
- `_cancel_drifted_working_orders` is **broker-agnostic** — it selects `BrokerOrder` by symbol/status across *all* accounts, so it covers **both** ORB/Webull working entry limits (the Piece-1 quote-priced entry) and v2/Schwab working orders. ✅ dual-broker.
- `_evaluate_v2_managed_exit` is **v2/Schwab-only by design** and correctly so: ORB has **no** `oms_managed_positions` row (`:1107` early-returns non-v2), so ORB positions never flow through it. Off-loading it is the v2/Schwab tick-path coverage. ✅ (not a single-broker gap — ORB simply uses a different exit path).
- **⚠️ The ORB/Webull exit DB is NOT in PR-A.** ORB's stop, when it *triggers*, runs `_evaluate_hard_stop_market_event` → `_trigger_hard_stop` (`:2051`) → **`process_trade_intent`** (`:2111`), which opens a synchronous on-loop DB session. That is a real tick-path freeze source for the ORB side — but it lives in `process_trade_intent`, which the approved sequence scopes to **PR-D**. So after PR-A, ORB's stop *evaluation/ratchet* is DB-free (never a freeze source), but the ORB stop *act* (trigger→submit) still has on-loop DB until PR-D. Note #391 Fix-1 already bounds every OMS DB call (incl. `process_trade_intent`) to ~5s, so this is a *bounded* ≤5s loop stall today, not an unbounded hang — PR-A/PR-D take it to zero. See §8 open question 4.

**PR-B — cold-path store units.** Convert the genuinely cold `self.store.*`/session opens (startup rehydrate, terminalize-orphans, cancel-intent target lookup, setup-invalid reason, schwab-ineligible reads/writes) to `_run_db`. No broker interleave, no hot-path coupling → mechanical, low-risk, no decoupling proof needed. This drains the "~55 sites" bulk safely.

**PR-C — `sync_broker_orders` phase-split.** Restructure to: collect open orders (DB unit) → per-order broker `fetch_order_update` (on-loop) → batch the resulting DB writes into `_run_db` units, with the native-stop-registry dict mutations kept on-loop between phases. Control-loop cadence (not per-tick), so lower urgency than PR-A but larger surface.

**PR-D — `process_trade_intent` phase-split.** The most interleaved; do it last with the most test coverage. Phases: pre-submit reads (`_run_db`) → risk/size decision (on-loop) → broker submit (on-loop) → post-submit record/re-arm writes (`_run_db`), preserving the atomic-commit semantics (see §6, the "atomicity" risk). **On the hard-stop path → decoupling proof required.**

**PR-E — fleet-wide timeout rollout.** See §5.

Splitting PRs this way means the highest-value, lowest-risk change (PR-A/PR-B) can ship first and the hardest (PR-D) carries the most scrutiny, rather than one giant risky diff.

---

## 5. Fleet-wide timeout rollout (PR-E)

Every non-OMS service builds the **untimed** `build_session_factory`: reconciler (`reconciliation/service.py:66`), control plane (`services/control_plane.py:3389`), ORB (`services/orb_app.py:209`), trade-coach (`:81`), **schwab_1m_v2 (two sites: `services/schwab_1m_v2_bot.py:328` and `:353`)**, market-capture (`:108`), strategy-engine (`services/strategy_engine_app.py:5889`), plus `runtime_seed.py:30` and `maintenance/reset_active_state.py:18`.

**Do NOT apply the OMS's aggressive 5s bound fleet-wide** — `db/session.py:60-66` explicitly warns the reconciler's scans and the bots' warmup backfills have *legitimately* slow queries. Design: a `build_timed_session_factory(settings, profile)` with **per-service profiles**:
- **OMS / bots (latency-critical, event-loop):** ~5s statement (as today).
- **Reconciler / market-capture / warmup paths:** generous (e.g. 30–60s statement) — enough to never false-trip a legitimate scan, but still finite so a *stalled connection* can't hang forever (the actual SPOF).
- Each profile behind its own `MAI_TAI_<SVC>_DB_TIMEOUTS_ENABLED` flag, default off per-service until validated, so rollout is incremental and independently reversible.

The value here is not latency — it's that **no service can hang forever on a dead DB connection**, which is the class. Timeouts are cheap insurance even where off-loading isn't warranted.

---

## 6. Protective-path gate & the proofs required

**Gate (operator's, restated):** *no change may delay a stop.* Where a change is on a protective path, the #391 stop-decoupling proof is repeated.

Protective-path touchpoints in scope:
1. **PR-A** (`_evaluate_v2_managed_exit`, `_cancel_drifted_working_orders`) — on the tick path.
2. **PR-D** (`process_trade_intent`) — reached by `_trigger_hard_stop:2111`.

Proofs (extend `tests/unit/test_oms_spof_resilience.py`, which already has the harness — SQLite `StaticPool` + `check_same_thread=False` chosen precisely for `to_thread`):

- **P-DECOUPLE (repeat of Fix-3):** with a phase-split target, inject a hang/timeout into a *non-protective* DB phase and assert the **protective close still submits** within bound. New variants: "hard-stop fires while `_evaluate_v2_managed_exit`'s DB read hangs," "drift-cancel DB write hangs but the next tick's stop eval still runs."
- **P-BEHAVIOUR-IDENTICAL:** for each converted method, a characterization test proving byte-identical outcomes (same intents, same order requests, same DB rows, same events) between old and new, on the unmodified-then-modified pattern — the endorsed refactor methodology.
- **P-NO-RACE:** assert that every dict mutation (`_armed_hard_stops`, `_managed_v2_symbols`, `_latest_*`) remains on the loop thread — e.g. a test that fails if a mutation is observed from a worker thread. This guards the §3 invariant against future drift.
- **P-ATOMICITY (PR-D specific):** `process_trade_intent` currently commits once at `:693`. Phase-splitting introduces multiple commits; the test must prove no *partially-applied* intent is observable (e.g. an order recorded but the intent left un-terminalized) if a later phase fails — i.e. define and prove the new consistency boundary. If atomicity can't be preserved cleanly, PR-D's design must document the exact new failure modes for operator sign-off before build.

Full unit suite green + the existing integration/replay roundtrip (`tests/integration/test_strategy_oms_roundtrip.py`) green.

---

## 7. Rollout / rollback

- Each PR flag-gated where behaviour could differ (reuse the `MAI_TAI_OMS_DB_TIMEOUTS_ENABLED` style), default matching current behaviour until validated, so every step has a **single-env-var + restart** rollback lever.
- **Genuine-green CI** (no admin bypass) — the JSONB-on-SQLite harness issue is resolved so `validate` gates.
- **Attended quiet-window deploy** (OMS touches the stop path → never at the open; the #391 deploy at 11:21 ET is the template). OMS-only choreography (stop-strategy → restart-oms → start-strategy), **fleet verified flat at the restart moment**, drift-check the exact files.
- Watchdog (#2) + the health system (framework item 3) remain the running safety net; ultimate verdict = the next real DB-stall event, self-recovered instead of zombied.

---

## 8. Open questions for operator before build

1. **Fold PR-A (tick-path) into this work?** It's not in the ticket wording but it's the strongest remaining freeze risk and directly in the #391-protected loop. Recommend yes, as the *first* PR.
2. **PR-D atomicity:** acceptable to introduce a documented multi-commit consistency boundary in `process_trade_intent`, or must it stay single-commit (which limits how much of it can go off-loop)?
3. Timeout **profile values** for reconciler/warmup paths — I've proposed 30–60s; confirm or set.
4. **ORB/Webull stop-act coverage timing:** the ORB stop *trigger→submit* DB (via `process_trade_intent`) is scoped to PR-D. It's bounded to ≤5s by #391 Fix-1 today. Keep it in PR-D (recommended — it's the most-interleaved method, deserves the most test scrutiny, and is already bounded), or pull it forward so ORB's stop-act is fully off-loop sooner? Recommend keep in PR-D.
