# OMS SPOF P3 — PR-D design (process_trade_intent + sync_broker_orders + shared native-stop machinery)

**Status:** DESIGN-FIRST (no code). Grounded in a full classified interleaving map of `oms/service.py` @ `0d0da1c`.
**Bottom line up front:** the map shows the post-submit order-recording + stop-arming path is an **irreducible DB↔dict↔broker braid** that stays on-loop regardless of how far we refactor. A "full" PR-D therefore buys only the **prologue read/write off-load**, at the cost of a high-risk restructure of the live-money order/stop path *and* a multi-commit atomicity change. Given #391 already converted the SPOF from *unbounded hang* to *≤5s-bounded* and PR-A removed the per-tick driver, **my recommendation is to STOP the aggressive off-load here (Option C)** rather than take that risk for marginal benefit. The design of the full refactor (Option A) is specified below so you can weigh it.

---

## 1. The interleaving reality (from the map)

`process_trade_intent` (L453–739) and `sync_broker_orders` (L1680–1922) each hold **one session open across the whole body**, with the broker submit/fetch **in the middle**, and reuse ORM objects (`strategy`/`broker_account`/`intent`/`order`) across every broker await. The post-fill sequence in `_record_order_reports` (L2781) and per-order in `sync_broker_orders` is:

```
apply_fill_to_positions          [DB]
_update_hard_stop_registry_from_fill      [DICT: mutates _armed_hard_stops]   ← MUST stay on-loop
await _manage_native_stop_after_fill      [DICT read + DB + BROKER submit]     ← MUST stay on-loop
_apply_managed_position_after_fill        [DB + DICT: mutates _managed_v2_symbols]
mark_intent_status               [DB]
_update_hard_stop_registry_from_order_status  [DICT]                           ← MUST stay on-loop
await _rearm_native_stop_from_registry    [DICT read + DB + BROKER]            ← MUST stay on-loop
```

**Key facts that constrain the design:**
- **No pure-DB run survives past the first fill.** The DB is chopped into ≤4-op runs by an interleaved DICT mutation or BROKER await. The only "long" DB tail is the `session.commit()` itself.
- **Every `_armed_hard_stops` / `_managed_v2_symbols` mutation must stay on the loop thread** (no locks in the OMS — the load-bearing invariant). Sites: L2258/2265 (`_trigger_hard_stop`), L2377/2410/2417 (`_update_..._from_fill`), L2444/2447/2451/2453 (`_update_..._from_order_status`), L1173/1197 (`_apply_managed_position_after_fill`).
- **9 broker awaits are woven through this surface** (L693, L790, L1045, L1729, L2757, L3084, L3437, L3479, L3694) — all stay on-loop.
- **ORM objects are reused across broker awaits** → any off-loop `_run_db` unit must re-load by id/primitive inside the worker thread (the `_run_db` contract forbids a live Session/ORM object crossing threads).
- **The `_run_db` concurrency constraint** (learned in PR-C): a `_run_db` unit opens a *second* session on a worker thread. If the caller still holds its own session open, that's two concurrent sessions → fine on Postgres (pooled) but **deadlocks the SQLite `StaticPool` test harness** (single shared connection). So a `_run_db` unit can only be used when **no other session is held** — i.e. only if the method is restructured so it does NOT hold a session across the unit.

**Consequence:** the post-submit braid (`_record_order_reports`, `_manage_native_stop_after_fill`, `_arm_or_rearm_native_stop_guard`, `_rearm_native_stop_from_registry`, `_update_hard_stop_registry_*`, `_apply_managed_position_after_fill`, `_refresh_working_order`) is **irreducible** — it interleaves DB + dict + broker at ≤4-op granularity and cannot become clean off-loop units without splitting the native-stop *submit* itself out of its transaction (a deep, high-risk change to the exact code that arms real-money stops). **This braid stays on-loop in every option below.**

**What CAN be cleanly off-loaded** (pure-DB, no dict, no broker): `_terminalize_orphaned_active_intents` (+`_target_order_for_cancel_intent`), `_intent_setup_invalid_reason` (bar-history revalidation — the heaviest read), `_has_cached_schwab_ineligible_symbol` (read), and the **prologue read/record clusters** — *but only if the enclosing method is restructured so it isn't holding a session across them* (per the concurrency constraint).

---

## 2. Option A — the full phase-split (the "real" PR-D), specified

Restructure both hot methods so the session is **not held across the interleaved body**, enabling `_run_db` units:

**`process_trade_intent`:**
- **Phase 1 (off-loop `_run_db`, commits):** `ensure_strategy` → `ensure_broker_account` → `create_trade_intent` → `record_risk_check` → the early reject reads (`_has_cached_schwab_ineligible_symbol`, `find_open_exit_order`, `get_virtual_position`, `get_account_position`, `get_open_exit_reserved_quantity`). Returns a plain **decision packet** (`intent_id`, `strategy_id`, `broker_account_id`, `request_quantity` or a reject reason, order-request fields). **Commits the intent + risk-check before submit.**
- **Phase 2 (on-loop):** the pre-submit broker cancels (`_cancel_native_stop_guard_before_sell`, `_cancel_open_exit_orders_before_hard_stop`), the `_refresh_broker_position_quantity` broker call when cached qty ≤ 0 (with a follow-on read unit for reserved qty), the `_apply_orb_quote_priced_entry` **dict read**, and the **`submit_order`**.
- **Phase 3 (on-loop, IRREDUCIBLE braid):** `_record_order_reports` + `_process_stop_reject_market_fallback` + `_rearm_native_stop_from_registry` — **stays on-loop** (dict + broker). Then a small off-loop write unit could persist any trailing bookkeeping, but the braid's own DB is inseparable from its dict/broker steps.

**`sync_broker_orders`:**
- **Phase 1 (off-loop `_run_db`):** snapshot open orders + lookups (`list_open_orders`, account/strategy maps) → plain per-order snapshots.
- **Phase 2 (on-loop):** per order, `fetch_order_update` (broker) → the **irreducible braid** (writes + registry dict + native-stop broker) **stays on-loop**; `_intent_setup_invalid_reason` becomes an off-loop read unit *only because* by this point we could avoid holding a session (but the braid around it holds one — so in practice it stays on-loop unless the whole per-order body is unwound).
- **Phase 3 (off-loop `_run_db`, post-loop):** `_terminalize_orphaned_active_intents`.

**What Option A actually off-loads:** the prologue validate/record/reads of `process_trade_intent`, the `list_open_orders` snapshot of `sync_broker_orders`, and the 3 cold-site helpers. **The post-submit/ per-order braid stays on-loop either way.**

**Costs of Option A:**
- **Atomicity change (P-ATOMICITY):** `process_trade_intent` currently commits once (L738); Phase-1 commits the intent + risk-check **before** the broker submit. New failure mode: submit fails after the intent is committed → a dangling `created` intent with no order. (Mitigation: the existing `_terminalize_orphaned_active_intents` already reaps orphaned active intents; and the order lifecycle already records the order AFTER submit today, so "order at broker, not yet in DB" is a pre-existing state the reconciler handles. Still, this must be explicitly proven, not assumed.)
- **ORM-reload overhead + complexity:** every off-loop unit re-loads by id; the decision packet must carry all primitives across phases.
- **Live-money risk:** the restructure touches the exact path that sizes sells, records fills, and arms stops. This is the highest-blast-radius code in the OMS.

**Benefit of Option A:** eliminates the ≤5s-bounded on-loop stall for the *prologue reads/writes* (fast small ops + a few position reads). **Does not** eliminate it for the irreducible braid (which stays on-loop).

---

## 3. Option B — terminalize-only (the PR-C leftover)

Off-load just `_terminalize_orphaned_active_intents` post-loop (clean, no concurrency). Marginal — this is what we already judged not worth a standalone deploy.

## 4. Option C — stop here (recommended)

Declare the SPOF off-load track **complete after #391 + PR-A**, and accept the remaining on-loop DB in `process_trade_intent` / `sync_broker_orders` as **bounded, low-probability, self-recovering**:
- #391 put a `statement_timeout=5s` / `lock_timeout=3s` / `connect_timeout=5s` / `pool_timeout=5s` + `pool_recycle` on **every** OMS DB call → the *unbounded* hang that caused the 07-01/02 multi-hour zombie is **structurally impossible** now.
- PR-A moved the **per-tick** DB (the highest-frequency driver) off-loop.
- The residual: a dead/stalled DB connection *coinciding* with an intent dispatch or a control-loop order-sync → a **≤5s-per-statement** loop stall that self-recovers (pool_recycle + pre_ping replace the dead connection). The liveness watchdog's 180s threshold won't false-alarm; the health system (framework #3) is the functional net.
- The post-submit braid is **irreducible** — the one part we'd most "want" off-loop can't be, so a full PR-D leaves it on-loop anyway. The marginal prologue off-load isn't worth restructuring the live-money stop-arming path.

**Recommendation: Option C**, optionally with **Option B** if we want to tidy the one clean site. Do **not** do Option A unless a real post-#391 bounded-stall event shows the intent/control path freezing long enough to matter — at which point Option A becomes justified and we do it with the full proof below.

---

## 5. Proof plan (required IF Option A is chosen)

Mirrors the #391 / PR-A resilience suite (`tests/unit/test_oms_spof_resilience.py`):
- **P-DECOUPLE (mandatory — `process_trade_intent` is on the hard-stop path via `_trigger_hard_stop` L2255):** with every off-loaded unit stubbed to raise `TimeoutError`, the protective hard-stop close still submits. (Extends `test_pra_armed_stop_fires_even_when_tickpath_db_units_stall`.)
- **P-NO-RACE:** thread-ident spy proving off-loaded units run on a worker thread while **every** `_armed_hard_stops` / `_managed_v2_symbols` mutation runs on the loop thread. (Extends `test_pra_v2_exit_db_runs_off_loop_while_dict_mutation_stays_on_loop`.)
- **P-ATOMICITY:** prove no harmful partial state when Phase-2 submit fails after Phase-1 commit — the dangling intent is reaped by `_terminalize_orphaned_active_intents` and never double-fills / never leaves a naked managed row.
- **P-BEHAVIOUR-IDENTICAL (by-name regression set the map identified):** `test_oms_risk_service.py` — pti fill/reject/cancel (L990/1042/1076/1132/1187/1331/1389), sync_broker_orders (L1444/1498/1567/1620/1687), working-order refresh (L1757–2117), **native-stop registry/arm** (L2165/2202/2208–2271/2351/2414/2665/2747/2845), exit sizing (L2962/3032), drift (L3180); `test_oms_managed_positions.py`, `test_v2_managed_exit.py`, `test_orb_oms_quote_priced_entry.py`, `tests/integration/test_strategy_oms_roundtrip.py`. All must stay green unchanged.

---

## 6. Cold-site accounting (unchanged — nothing dropped)

All 5 remain assigned to PR-D. Under **Option C**, they stay on-loop (Fix-1-bounded); under **Option A**, `_terminalize` + `_target_order` + (via restructure) `_intent_setup_invalid_reason` + `_has_cached_schwab_ineligible_symbol` off-load, and the schwab-ineligible **write** stays in the on-loop braid (`_record_order_reports`). Either way they are accounted for, not lost.

---

## 7. Rollout / rollback (IF Option A)
Flag-gated per-method (`MAI_TAI_OMS_PTI_OFFLOAD_ENABLED` / `..._SBO_OFFLOAD_ENABLED`), default off until validated; genuine-green CI (the StaticPool concurrency constraint means the restructure must fully unwind the held session — CI enforces this); attended quiet-window OMS-only deploy; rollback = flag off + restart (or revert + redeploy `0d0da1c`).
