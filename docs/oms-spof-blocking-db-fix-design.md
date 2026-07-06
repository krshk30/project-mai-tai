# OMS SPOF fix — blocking DB on the asyncio event loop (the zombie cure) — DESIGN

**Status:** design-first, for operator review. **No code yet.** Fleet chokepoint, live money →
full discipline (design → PR → genuine-green full CI → attended fleet-flat OMS restart → operator GO).

**Incident:** OMS zombied 2× (2026-07-01 ~5h, 2026-07-02 ~10:03 ET). py-spy pinned both: a synchronous
Postgres `session.flush()` in `sync_broker_state`→`sync_account_positions` (`oms/store.py:662`) ran INLINE
on the asyncio event loop and hung forever on a stalled DB connection → whole loop frozen → "active but
heartbeat-frozen" zombie. This is the **2nd SPOF of this class** (strategy-engine freeze was the 1st).

---

## 1. Root cause — systemic, not one flush

`db/session.py:12-13` builds a **sync** psycopg3 engine with **only `pool_pre_ping=True`** — no
`statement_timeout`, no `connect_timeout`, no explicit `pool_timeout`. **Zero** OMS DB calls are wrapped
in `run_in_executor`/`to_thread` (broker REST *is* offloaded in the adapters; the DB I/O around it is
not). So **every** `with self.session_factory() as session:` block and **every** `self.store.*` call
inside an `async def` runs blocking psycopg I/O directly on the loop, and a stalled connection at ANY of
them freezes the loop exactly like the incident flush. The incident flush is one of **dozens** of
equivalent freeze points.

## 2. The pattern (audit) — every loop-resident DB freeze point

Full audit table in the session record; condensed by exposure:

**HOT — reachable from a single quote/trade tick or a hard-stop trigger (SPOF-class):**
- **A1** `_has_active_native_stop_guard_order` (`service.py:1346-1357`) — session opened at
  `_trigger_hard_stop:1920` **before** the close. Stall ⇒ *the stop never fires AND the loop freezes.*
- **A2–A14** the whole `process_trade_intent` block (`service.py:359-644`, **10 `commit()`s**), called
  inline from `_trigger_hard_stop:1955`.
- **C1–C12** `sync_broker_state` fan-out incl. **the incident flush** `store.py:662` — reached HOT via
  `process_trade_intent:649` **after** a hard-stop close (post-close reconcile).
- **D1–D7** `_cancel_drifted_working_orders` (`service.py:2837`) — session opened on **every quote tick**
  (highest-frequency).
- **E1–E6** `_evaluate_v2_managed_exit` (`service.py:1182`) — session on the quote path; has a try/except
  that catches *exceptions* but **not a hang**.

**PERIODIC — `_run_control_loop`:** P1 `_has_active_stop_guard_orders` at the top of *every* iteration
(`service.py:220`, un-wrapped); P2 the `sync_broker_state` fan-out on the 5s cadence — **where both
07-01/02 zombies actually occurred.**

**INTENT:** `process_trade_intent` + fills recording (`_record_order_reports`, `store.py:545/614/...`).
**STARTUP:** `seed_runtime_metadata`, `_rehydrate_managed_v2_symbols` (one-time; delays boot, not a
steady-state zombie).

## 3. Loop-resilience audit — two distinct death modes

- **`_run_tick_consumer` (259-300):** per-event `try/except` ⇒ an **exception** skip-continues (survives).
  But a **hang** raises nothing ⇒ the `await` never returns ⇒ both tasks + heartbeat freeze, nothing
  logged. **Exception→survives; hang→zombie.** No `asyncio.wait_for` anywhere.
- **`sync_broker_state` / `_run_control_loop:244-250`:** exception caught+continue; **hang → freeze**
  (the exact incident mechanism).
- **🔴 New finding — the control loop has *fatal* gaps:** intent dispatch `_handle_stream_message`
  (`service.py:240`) and the interval check (`220`) are **outside any try/except** ⇒ an **exception**
  there propagates to `run():193`, the `finally` cancels the tick task, and **the whole service exits.**
  So a bad intent (or a timeout-exception once we add timeouts) is *fatal to the OMS*, not skip-continue.

---

## 4. The fix — four parts

### Fix 1 — Engine timeouts (the universal backstop; requirement 2)
Bounds **every** DB call (all dozens of sites) so a stall **raises within seconds instead of hanging
forever** — the single highest-leverage change; it alone makes the unbounded zombie impossible.

`db/session.py`, OMS engine:
- `connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000 -c lock_timeout=3000"}`
- `pool_timeout=5` (bounds waiting for a free pooled connection — matters because the pool is shared by
  both tasks; a hung connection + default pool can starve *both*)
- keep `pool_pre_ping=True`; add `pool_recycle=1800`.
- **⚠️ Deliberately NOT setting an aggressive `idle_in_transaction_session_timeout`.** The audit shows
  `process_trade_intent` **holds a DB transaction open across the broker `submit_order` await** — a legit
  transaction can sit "idle in transaction" for the broker-call duration. An aggressive value would kill
  legit orders. Leave default (or set generous ≥60s). (The held-open-across-broker-await pattern is
  itself a pool-pressure contributor → noted as a fast-follow refactor, out of scope here.)
- **`statement_timeout=5000ms` is safe:** every OMS query is a point-read / small flush (sub-second
  normally); 5s is 100×+ headroom. It's *per statement*, not per transaction, so fan-outs are fine.

**Scope decision (surface for operator):** `build_engine` is `@lru_cache`'d and **shared by all
services**. Options:
- **(Recommended) OMS-scoped:** parameterize `build_engine`/`build_session_factory` with timeout kwargs
  (default off), supply the aggressive values from the OMS entrypoint (`services/oms_risk.py`) via new
  env-tunable settings (`oms_db_statement_timeout_ms=5000`, `oms_db_connect_timeout_s=5`,
  `oms_db_pool_timeout_s=5`). Safe blast radius; cures the proven repeat-offender now; other services
  unchanged. **Fleet-wide hardening (strategy-engine et al.) = deliberate fast-follow with per-service
  values** (they have slower legit queries — reconciler scans, backfills — so a blind global 5s could
  break them).
- **(Alt) Global:** edit `db/session.py:13` directly → hardens the whole fleet in one change, but risks
  killing a legit slow query elsewhere = a *new* incident. Not recommended for this PR.

### Fix 2 — Blocking DB off the loop for the HOT sync blocks (requirement 1)
Add an off-loop runner and route the cleanly-wrappable HOT / periodic sync blocks through it, so even a
timeout-bounded stall never blocks the shared loop *at all* (other tasks + heartbeat keep running):
```python
async def _run_db(self, fn):
    def _unit():
        with self.session_factory() as session:   # opened+committed+closed IN the worker thread
            r = fn(session); session.commit(); return r
    return await asyncio.to_thread(_unit)
```
Convert (each isolates a **pure-sync** unit — broker `await`s stay OUTSIDE the thread): **the incident
method `sync_broker_state` (sync_broker_positions/orders — where both zombies occurred), A1
(`_has_active_native_stop_guard_order`), D (`_cancel_drifted_working_orders`), E
(`_evaluate_v2_managed_exit` DB block).** Sessions never cross threads (created+used+closed inside one
`_unit`), so SQLAlchemy thread-safety holds.

**Not wholesale-converted this PR:** `process_trade_intent` (359-644) *interleaves* async broker `await`s
with sync DB inside one long session — moving it off-loop is a risky refactor. It is **fully protected by
Fix 1** (each DB op bounded to 5s) **and Fix 3** (the stop fires regardless). Wholesale off-loop refactor
of the intent path = fast-follow. **Scope decision (surface):** hot-path off-loop now vs. full off-loop
of every site.

### Fix 3 — Decouple hard-stop PROTECTION from DB bookkeeping (decision a)
The stop's protective action = **submitting the broker close** (already off-loop via the adapter's
`to_thread`). It must fire regardless of DB health. Changes:
- Pre-close DB check A1 → **best-effort**: run via `_run_db` with the timeout; on timeout/exception,
  **log and PROCEED to fire the stop** (do not abort protection on a DB stall). Dedup-against-existing is
  an optimization, not a safety gate.
- Post-close reconcile (`sync_broker_state` at `process_trade_intent:649`) → **non-blocking / best-effort**
  (off-loop + timeout); its failure must not unwind the already-submitted close. The broker order is the
  source of truth; the next periodic `sync_broker_orders` back-fills the DB from broker.
- Net: **DB stalls degrade bookkeeping, never block the stop.**

### Fix 4 — Loop hardening (requirement 4)
- **Wrap the fatal control-loop gaps:** put `_handle_stream_message` (`240`) and the interval check
  (`220`) in `try/except (CancelledError re-raised; Exception logged) → continue`, so a bad intent or a
  timeout-exception **skip-continues instead of exiting the service.**
- **Timeout-exceptions now become the common raised error** (from Fix 1) — every DB call site's enclosing
  `try/except` already catches `Exception`; verify each HOT/PERIODIC handler treats a raised
  `OperationalError`/`QueryCanceled` as log-skip-continue (the tick consumer already does; the control
  loop after Fix 4 does; the exit-eval `_evaluate_v2_managed_exit:1227` already does).
- Result: **exception → skip-continue everywhere; hang → bounded to ≤5s then raised → skip-continue.**
  Neither can zombie or exit the service. Worst case under a full DB outage: OMS stays **alive** (heartbeat
  beating → watchdog stays informed), logs errors, and reports degraded — vs. a silent 5h zombie.

---

## 5. Design decisions (surfaced)

**(a) Does the hard-stop still fire if the sync times out?** **YES — Fix 3.** The protective action is the
broker close submit (off-loop, DB-independent). Pre-close DB checks are best-effort (proceed on
stall/timeout); post-close reconcile is non-blocking. A slow position-sync can no longer block the stop.

**(b) Consistency if a DB write times out mid-flush.** Postgres `statement_timeout` cancels the *statement*
and the transaction **rolls back** (session context-manager rolls back on exception) — **no partial
commit**, atomic all-or-nothing. The un-written state is reconciled by the **existing** mechanism:
`sync_broker_state` exists precisely to project broker truth → DB (broker is source of truth; DB is a
reconcilable projection). A timed-out intent/order/fill write is back-filled next sync cycle / by the
reconciler. No new consistency hazard; we lean on the reconcile loop already in place. (No app-level retry
inside the tick — that would re-block; next-cycle reconcile is the retry.)

**(c) Does moving sync off-loop change ordering the OMS relies on?** **No.** (i) Within a handler, steps
are still `await`ed in sequence (broker-submit-then-DB-record order preserved). (ii) Cross-task
interleaving between the tick consumer and control loop **already exists** at today's await points; each
unit-of-work is an isolated per-session transaction, so DB-level isolation (+ `lock_timeout`) handles
concurrent row access — adding await points doesn't create a new shared-state race. (iii) Coalescing /
last-quote-wins and the un-coalesced trade order for armed stops are untouched. The one care-item:
`_run_db` must open+commit+close each session **inside** the worker thread (no session shared across
threads) — enforced by the runner's shape.

---

## 6. Tests + verification

**Unit/integration:**
- Fix 1: engine built with the timeout `connect_args`/`pool_timeout` (assert the kwargs reach
  `create_engine`); a session whose statement exceeds `statement_timeout` raises (simulate via
  `SELECT pg_sleep`) rather than hanging.
- Fix 3: hard-stop path with a stubbed DB that raises/stalls on the pre-close check → **the broker close is
  still submitted** (the decouple proof); post-close sync failure does not unwind the close.
- Fix 4: an exception injected in `_handle_stream_message` → control loop **logs and continues**, service
  does not exit; a raised `OperationalError` in a tick handler → tick consumer skip-continues.
- `_run_db`: unit-of-work commits in a thread; exception rolls back; no session crosses threads.
- Full existing OMS suite green (roundtrip, exit ladder, trailing stop, drift-cancel) — behavior-identical
  when DB is healthy.

**Fault-injection (the real verdict, on a scratch/paper instance — NOT live):** point the OMS at a DB
made to stall (e.g., a `pg_sleep`-injected statement or a paused connection) and confirm: heartbeat keeps
advancing (no zombie), the loop logs timeouts + skip-continues, and a hard-stop still submits its close.
This reproduces the 07-01/02 condition and proves the cure. (Mirrors the "survival-test = the verdict"
discipline.)

## 7. Deploy discipline
Design (this) → operator GO on approach → implement → PR → **genuine-green full CI** (no admin) →
attended, fleet-flat (0 open managed positions) → `git pull` + restart **OMS only** → confirm new PID,
0 tracebacks, heartbeat healthy, timeouts present in the running engine (log/settings). The **OMS-liveness
watchdog is now the safety net** while this ships. Separate from #390 (already live). Rollback = revert +
OMS restart.

## 8. Out of scope (deliberate, tracked)
- Fleet-wide timeout rollout to strategy-engine/reconciler/others (fast-follow, per-service-tuned values).
- Refactor `process_trade_intent` to not hold a DB transaction open across the broker await (pool-pressure
  reduction; wholesale off-loop of the intent path).
- The v2 closed-on-submit desync (#388-class) — separate tracked item.
