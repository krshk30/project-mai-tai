# Restart-while-holding safety — v2 (Schwab) + ORB (Webull)

**Status:** DESIGN-FIRST (no code until operator approves). Live-money position safety.
**Author:** session 2026-07-07. Grounded in a read-only code map of `/home/trader/project-mai-tai`.
**Context:** restart-while-holding was *unsafe to attempt at all* before #391 (the OMS could zombie on the DB flush during a restart-era sync). #391 removed that. Restarting while holding is now mechanically safe — but the **position-protection behaviour across a restart has never been tested**, and the map below shows it is **not** safe for ORB.

---

## 0. OMS scoping invariant (operator, load-bearing — governs this whole design)

> **The OMS only tracks, protects, manages, and acts on positions IT placed. It NEVER sells, stops, or acts on a position it did not open (the operator's manual Webull/Schwab trades). Manual positions are outside the OMS's universe — it does not protect them and must be structurally incapable of selling them. The only reconciler concern worth acting on is the OMS's OWN position going missing/mismatched at the broker — NOT the presence of a position the OMS didn't place.**

**This is already structurally enforced on the act/sell path** (verified 2026-07-07): every sell in `process_trade_intent` clamps to `strategy_available_quantity = get_virtual_position(strategy_id, broker_account_id, symbol)` — the OMS's own per-strategy ledger (`service.py:544-554`) — and rejects with `"no strategy position available to sell"` if that ledger is ≤ 0 (`:556-566`); final size is `min(intent.qty, strategy_available_quantity, remaining_account_quantity)` (`:614`). A manual holding has no `virtual_positions` row for a bot `strategy_id`, so the OMS cannot sell into it. Arm/manage paths are likewise gated (v2 managed rows to `strategy_code=="schwab_1m_v2"`; ORB stops arm only from a buy-open fill of an ORB-*emitted* intent). The protected-symbols list is a belt over this structural guard, not the guard.

**Consequence for this design:** rehydration, protection, and boot reconciliation must key **strictly off OMS-owned records** — a `virtual_positions`/`oms_managed_positions` row or an armed-stop entry whose provenance is an OMS-placed order/fill. A broker holding with **no OMS provenance is out of universe**: never rehydrated, never armed, never sold, never alerted-as-naked. "Ownership" is decided by **order/fill provenance** (does the OMS have an order or fill record for this symbol on this account/strategy?), never by mere broker presence or a symbol list.

## 1. The invariant to prove

> **On an OMS restart while an OMS-PLACED position is open, that position stays TRACKED, PROTECTED (a stop is armed/evaluating), and BROKER-CONSISTENT across the restart — never dropped, never left naked, never double-counted. (Manual positions are untouched, by §0.)**

Decomposed into four testable properties:
- **P-TRACKED:** after boot, the OMS knows about every bot-held broker position (a managed row and/or an armed-stop entry exists).
- **P-PROTECTED:** for each tracked position, a working protective mechanism is active before the OMS begins serving live ticks — never a window where the OMS holds but no stop can fire.
- **P-CONSISTENT:** OMS state matches the broker — a broker-flat symbol is closed in OMS state; a broker holding is reflected; quantities agree.
- **P-NO-DUP:** rehydration never creates a second managed row or double-arms / double-counts a position already represented.

---

## 2. Ground truth — what survives a restart today

**Boot sequence** (`oms/service.py:188-231`): install signal handlers (SIGTERM → `stop_event.set` only) → `seed_runtime_metadata` → **`_rehydrate_managed_v2_symbols()` (`:198`) — the only position rehydration** → start tick consumer + control loop. Both stream readers start at offset `"$"` (`:150-153`) → **no replay of pre-restart ticks**; the OMS waits for the next live tick per symbol.

**Shutdown** (`run()` finally, `:216-231`): sets stop-event, cancels the tick task, publishes a "stopping" heartbeat, closes Redis. **No order cancellation** — a broker-resident order (Schwab working exit, a native STOP) survives.

| | **v2 (`live:schwab_1m_v2`)** | **ORB (`live:orb`, Webull)** |
|---|---|---|
| Durable position record | `oms_managed_positions` row (create gated to `strategy_code=="schwab_1m_v2"`, `:1107`) | **None.** `_apply_managed_position_after_fill` early-returns for non-v2 (`:1107`). |
| Protection | OMS exit ladder + 1.5% hard stop, per-quote (`_evaluate_v2_managed_exit`, `:1213`) | In-memory trailing stop in `_armed_hard_stops` (`_evaluate_hard_stop_market_event` `:1958` + `_ratchet_trailing_stop` `:2020`). Webull native STOP is rejected (417) → **in-memory trail is the SOLE net.** |
| Rehydrates on boot? | **YES** — `_rehydrate_managed_v2_symbols` (`:198`) restores `_managed_v2_symbols`; `_hydrate_v2_position` (`:1179`) restores peak/tier/floor/scales from the DB row on the next quote. | **NO** — `_armed_hard_stops` inits empty (`:155`) and is written **only when a brand-new buy-open fill is observed** (`sync_broker_orders` → `_update_hard_stop_registry_from_fill`, `:1619-1630`). An already-filled entry is terminal, not in `list_open_orders`, and its fill was already recorded → the registry is never repopulated. |
| Post-restart state | Tracked + protected + consistent (ladder continues). Minor: brief gap until the first post-boot quote. | **NAKED** — held at Webull, OMS has zero stop state and no managed row. Nothing re-arms it, ever, for that lot. |

**Boot-time broker reconciliation does NOT exist.** The first control-loop iteration runs `sync_broker_positions` (`:1475`) → `store.sync_account_positions` (`store.py:619`), which only updates the `account_positions` **mirror table**. It does **not** create/repair `oms_managed_positions`, does **not** arm any stop. The separate reconciler service (`reconciliation/service.py:102`) is **detect-only** — it writes `position_quantity_mismatch` findings and never remediates.

---

## 3. Gaps (where the invariant breaks)

1. **🔴 ORB naked-after-restart (critical, P-PROTECTED + P-TRACKED fail).** In-memory-only stop registry, no durable record, Webull native STOP rejected → any OMS restart while ORB holds leaves the Webull position with no OMS protection indefinitely. This is the load-bearing "don't restart OMS while ORB holds" operating rule, as a code gap.
2. **🟡 No boot-time protection reconciliation (both, P-CONSISTENT).** A broker holding the OMS doesn't know about (or a managed row for a symbol the broker has since flattened) is not repaired at boot — only mirrored / reported.
3. **🟢 v2 boot gap (minor, P-PROTECTED).** v2 is unprotected only between boot and the first live quote for the symbol (no tick replay). Broker-resident working exits persist, so exposure is small — but real if the symbol is quiet at boot.
4. **⚪ Double-count untested (P-NO-DUP).** v2 idempotency (`get_open_managed_position` + fill-gated close, `:1115-1153`) looks correct but is not proven across an actual restart with a working exit in flight.

---

## 4. Design

Two workstreams. The ORB fix (§4a) is the substance; v2 hardening (§4b) is defensive; the boot ordering (§4c) is what actually makes "never naked" true.

### 4a. ORB — durable, rehydratable stop protection

The root cause is that ORB's protective state lives only in a process-memory dict. Make it durable and rebuild it on boot.

**Persist the armed-stop registry.** Add a durable store for `_armed_hard_stops` entries (proposed: a new `oms_armed_stops` table keyed by `(broker_account_name, symbol)`, columns: entry_price, quantity, trail_pct, stop_price/current_stop_level, peak_price, side, armed_at, updated_at, strategy_code). Written at two points that already exist:
- **on arm** (`_update_hard_stop_registry_from_fill`, `:2233/2266`) — a DB write already adjacent; add the persist there.
- **on ratchet** (`_ratchet_trailing_stop`, `:2020`) — persist **only when the stop level actually moves up**, not every tick, to bound write frequency; route through the off-loop `_run_db` helper (ties into Framework 1 — this write must not re-introduce on-loop blocking DB on the tick path).

**Rehydrate on boot.** Add `_rehydrate_armed_hard_stops()` alongside `_rehydrate_managed_v2_symbols` at `:198`: load persisted entries into `_armed_hard_stops`. The peak/stop-level come back with it, so the trail resumes where it was (not reset looser). *Alternative if per-ratchet persistence is deemed too heavy:* persist only entry+trail_pct and **re-derive a conservative stop on boot** from current price (documented as "may be looser than pre-restart by up to one ratchet step" — operator to choose; per-ratchet persist is preferred for fidelity).

**Native-stop belt (already fixed, keep):** the Webull `STOP→STOP_LOSS` map (#386) means a broker-resident STOP now *can* rest and survives a restart. Where a native stop is successfully armed, it is a second, broker-speed net that needs no rehydration. The in-memory trail rehydration above covers the case where the native stop was rejected/absent.

### 4b. v2 — defensive boot reconciliation (OMS-owned only, per §0)

v2 already rehydrates. Add defense for P-CONSISTENT **scoped to OMS-owned positions**: at boot, after `_rehydrate_managed_v2_symbols`, for each **existing managed row** verify the broker still backs it — close the row fill-gated if the broker reports that symbol flat (mirroring `_apply_managed_position_after_fill`). The actionable alert is the §0 concern only: **an OMS-owned v2 position (has a managed row / bot fill provenance) whose broker quantity is missing or mismatched.** A broker holding with **no** managed row and **no** bot fill provenance is a manual position → **out of universe, no alert, no action** (this is exactly the CYN/CELZ case). No happy-path change; it only repairs drift on the OMS's own rows.

### 4c. Boot ordering — the "never naked" guarantee (for OMS-placed positions)

Today the tick consumer (`:213`) and control loop (`:215`) start immediately; the first broker sync is lazy on the control loop. To guarantee **protected-before-serving** for **OMS-owned** positions, restructure boot to:

1. rehydrate v2 managed set (existing) + **armed-stop registry (new, ORB — provenance = OMS-placed fills only)**,
2. **one synchronous-at-boot `sync_broker_positions`** (off-loop via `_run_db`) to learn actual broker holdings,
3. reconcile **OMS-owned records against the broker** (not broker against everything): each rehydrated managed row / armed-stop entry must still be backed by a broker position of matching quantity. Mismatch on an **OMS-owned** record → loud RED alert (its own position drifted/vanished). A broker holding with **no OMS provenance → ignored** (manual; §0 — never armed, never flagged naked, never flattened),
4. *then* start serving ticks.

This closes the window where the OMS is live and holding **its own** position with no stop wired, without ever reaching toward a manual position. The extra boot latency is a few seconds, acceptable for an attended restart.

---

## 5. Test plan — prove the invariant

New file `tests/unit/test_oms_restart_while_holding.py` (reuse the `test_oms_spof_resilience.py` harness: SQLite `StaticPool`, `_bare_service()`), driving an actual `_rehydrate_*` + boot-reconcile sequence, not steady state:

- **T-V2-REHYDRATE (P-TRACKED/PROTECTED):** seed an open v2 `oms_managed_positions` row mid-ladder (peak/tier/floor/scales set) + broker adapter reporting the holding → run boot rehydrate → feed one quote → assert the ladder resumes from the persisted state (not reset) and the hard-stop evaluates. Proves what is believed but untested today.
- **T-ORB-REHYDRATE (P-PROTECTED — the critical one):** seed a persisted ORB armed-stop entry + broker reporting the Webull holding → boot → assert `_armed_hard_stops` is repopulated and `_evaluate_hard_stop_market_event` can fire on the next tick. Fails on today's code (the gap); passes after §4a.
- **T-ORB-LOST-RECORD (P-PROTECTED negative, OMS-owned):** boot with a broker ORB holding that **has OMS fill provenance** (a bot fill/order record exists) but a **missing** persisted armed-stop entry → assert a RED alert (an OMS-owned position lost its protection). This is the actionable case.
- **T-MANUAL-IGNORED (§0 invariant — critical):** boot with a broker holding that has **no** OMS provenance (no bot order/fill) on either account → assert the OMS **does nothing**: no armed stop, no managed row, no sell, no naked-alert. Proves manual positions stay out of universe (the CYN/CELZ/FCUV case) across restart.
- **T-BROKER-FLAT-CLOSE (P-CONSISTENT):** an **OMS-owned** managed/armed record exists, broker reports that symbol flat → assert the record is closed on boot, no phantom.
- **T-NO-DUP (P-NO-DUP):** rehydrate twice / rehydrate then observe the original fill again → assert exactly one managed row and one armed entry (idempotent).
- **T-ORDERING (P-PROTECTED):** assert ticks are not served until rehydrate+reconcile completes (protected-before-serving).
- **P-DECOUPLE regression:** the new boot `sync_broker_positions` and the per-ratchet persist must use `_run_db` — reuse the #391 stop-decoupling proof to confirm a hung boot-sync cannot block startup forever nor a persist block the tick loop.

Live verification: the ultimate verdict is an **attended restart-while-holding on a real organic ORB fill** (never a hand-injected position) — restart OMS while ORB holds, confirm the Webull position stays protected (armed stop present in registry post-boot, native stop resting) and un-double-counted. This is the belt-and-suspenders empirical gate that the code proof anticipates.

---

## 6. Rollout / rollback

- Flag-gate the new persistence + boot-reconcile (`MAI_TAI_OMS_ARMED_STOP_PERSISTENCE_ENABLED`, `MAI_TAI_OMS_BOOT_RECONCILE_ENABLED`), default off until validated → single-env-var rollback.
- Schema migration for `oms_armed_stops` is additive.
- Genuine-green CI; attended quiet-window deploy; OMS-only; fleet-flat at the restart moment; then the live restart-while-holding verdict on the next organic ORB fill.
- Interacts with Framework 1 (the per-ratchet persist must be off-loop) — sequence after or alongside PR-A of the off-load work.

---

## 7. Open questions for operator

1. **Trail-peak fidelity vs write cost:** persist the ratcheting stop level on every upward move (full fidelity, more writes — off-loop) vs persist entry+trail_pct only and re-derive a conservative stop on boot (fewer writes, stop may be up to one step looser)? Recommend full fidelity via off-loop `_run_db`.
2. **Should ORB get a first-class `oms_managed_positions` row** (generalising the v2-only gate at `:1107`) instead of a separate `oms_armed_stops` table? Cleaner long-term but a bigger change; the separate table is the lower-risk path. Your call.
3. ~~Protected-unknown policy~~ — **RESOLVED by the §0 scoping invariant.** A boot-time broker holding with no OMS provenance is manual → the OMS does nothing (never flatten, never arm). No open question here; auto-flatten / arm-fresh are forbidden (they would act on a manual position). The only alert is for an **OMS-owned** position that drifted/vanished at the broker.
4. **Reconciler scoping (structural follow-on, feeds Framework 3):** the reconciler currently emits `position_quantity_mismatch` critical findings for *every* broker holding vs virtual — including manual ones (CYN/CELZ/BJDX), which is noise per §0. Should we scope actionable reconciler findings to **OMS-owned** positions (provenance-based), making the presence of a manual holding a non-finding and retiring the protected-symbols list as the belt? Recommend yes; it's the structural end-state the invariant implies.
