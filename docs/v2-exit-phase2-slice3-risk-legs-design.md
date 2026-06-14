# Track 2 Phase 2 — Slice 3: per-quote risk legs + OMS-emitted sells — DESIGN

**Status:** DESIGN ONLY — **no sell-emitting code until operator review.** Third of 4 Phase-2 slices.
**This is the consequential slice: it emits SELLS.** It carries the **paper-isolation survival re-proof**,
which is the **deploy gate** — nothing activates until that proof is green.
**Builds on:** slice 1 (`oms_managed_positions`, #305), slice 2 (gateway quote bridge, #306), the
production-verified `exit_logic/` lib (Phase 1). All behind the single flag `oms_v2_exit_management_enabled`
(default OFF). File:line refs verified against the working tree.

---

## 0. Scope

Three pieces, one flag:
1. **Risk-leg eval on the OMS quote path** — hydrate an `exit_logic.Position` from the managed row,
   `update_price` per quote, run `check_hard_stop` + `check_intrabar_exit` (floor + scale); **co-locate
   the quote→Position state-update here** (the piece deferred from slice 1 — the hot path is touched once).
2. **OMS-emitted sells** — build/submit close + scale orders internally (mirroring `_arm_or_rearm_native_stop_guard`),
   `reference_price` per leg.
3. **The paper-isolation survival re-proof** — v2 exit sells provably route to `simulated`, can NEVER
   construct a real order. **The deploy gate.**

Out: tier MACD/stoch exits (slice 4 — needs v2's indicator publish); any real-account routing.

---

## 1. Risk-leg eval on the quote path

**Hook:** `oms/service.py::_handle_quote_tick_event` (1398) — async, in-memory cache update + hard-stop
eval + drift cancel; opens no session itself. After the existing cache update, add (gated):

```
if flag ON and symbol in self._managed_v2_symbols:
    with self.session_factory() as session:
        await self._evaluate_v2_managed_exit(session, symbol, quote)
        session.commit()
```

**`self._managed_v2_symbols` (in-memory set)** — the hot-path guard. A DB query per quote tick is too
expensive; instead keep a set of symbols with an OPEN v2 managed row, so a session is opened ONLY for
those. Maintained by the **single writer**: add on create (slice-1 `_apply_managed_position_after_fill`),
remove on close; rehydrated from `oms_managed_positions WHERE status='open'` at OMS startup. (When the
flag is OFF the set stays empty → the quote path is byte-identical to today.)

**`_evaluate_v2_managed_exit(session, symbol, quote)`** — the ladder, per quote:
1. `row = store.get_open_managed_position(...)`; if None → drop symbol from the set, return.
2. **Hydrate** `exit_logic.Position` from the row: `entry_price`, `quantity=current_quantity`,
   `peak/tier/floor/scales_done` from the row, **floor params from `make_v2_variant()`** (the §7-a config).
3. `position.update_price(eval_price)` — see the **price-source decision** below.
4. **Precedence hard > floor > scale** (the documented ladder), **at most one action per quote:**
   - `check_hard_stop(position, eval_price)` → if `HARD_STOP`: emit a **CLOSE** sell for the full
     `current_quantity`; mark row closed; drop from the set.
   - else `check_intrabar_exit(position)`:
     - `FLOOR_BREACH` → emit **CLOSE** for full `current_quantity`; close row; drop from set.
     - `SCALE` (`level`, `sell_qty`) → emit a **SCALE** sell for `sell_qty`; `position.apply_scale(...)`;
       row stays open with decremented `current_quantity` + appended `scales_done`.
     - else → no exit.
5. **Always persist** the updated `Position` state back to the row via
   `store.update_managed_position_from_position(session, row, position)` — **this is the co-located
   quote→Position state-update** (deferred from slice 1). One row write per quote; plus the exit order
   if one fired. All in the one transaction (sole-writer, atomic).

**Idempotency / no double-close:** emitting a CLOSE flips the row to `closed` + drops the symbol from the
set in the same transaction → the next quote finds no open row → no double-close. `scales_done` prevents
re-firing the same scale tier. The fill of the OMS-emitted sell is also processed by the slice-1
`_apply_managed_position_after_fill` sell branch (decrement/close) — so the row converges whether the
state-write or the fill lands first; both are OMS-single-writer, no race.

**Independence from native_stop_guard:** v2 registers no `_armed_hard_stops` and carries no
`stop_guard_enabled` (proven in slice-1/Q4). So this is a NEW parallel path — it never collides with the
momentum bots' native-stop machinery.

### Decision for review — the eval price source (and `reference_price`)
The quote cache holds `{bid, ask, received_at}`. Two honest options:
- **(A, recommended) eval + fill on the BID.** `eval_price = bid` (the realizable sell price); each emitted
  exit order sets `reference_price = bid` (the SimulatedBrokerAdapter fills at `reference_price`). Most
  honest available idealization — the price actually quoted on the sell side. Still idealized (full qty,
  no partial, no slippage — §7-c).
- **(B) fill at the leg LEVEL** (stop_price / floor_price / scale-trigger). Matches the re-score's
  exact-trigger fills → live-paper ≈ the backtest by construction, but optimistic for stops/floors
  (assumes you fill exactly at the level the price already crossed).
**Recommendation: A (bid).** It's the defensible "what we'd actually get" price; the re-score parity (B)
is a comparability nicety that overstates fills. Either way it's idealized pipe-validation, not a track
record — Track-3 ticks + a real broker give true fills. **Operator picks A or B.**
**Quote freshness:** skip eval if `now − received_at > oms_quote_drift_cancel_tolerance`-style staleness
window (reuse the native-stop freshness convention, `quote_max_age_ms`) so a stale quote never triggers
an exit.

---

## 2. OMS-emitted sells (close + scale)

Mirror **`_arm_or_rearm_native_stop_guard` (739-827)** — the proven internal-order-emit template:

1. Build a `TradeIntentEvent(source_service=SERVICE_NAME, payload=TradeIntentPayload(strategy_code,
   broker_account_name=ROW.broker_account_name, symbol, side="sell", quantity, intent_type="close"|"scale",
   reason="oms_v2_managed_exit:<HARD_STOP|FLOOR_BREACH|SCALE_xxx>", metadata={...}))`.
2. `intent = store.create_trade_intent(session, ...)` (persist the intent).
3. `_record_internal_risk_pass(session, intent, ..., reason="oms_v2_managed_exit")` (internally validated —
   not a strategy intent off the stream).
4. `request = OrderRequest(client_order_id=_build_client_order_id(event), broker_account_name=ROW.broker_account_name,
   strategy_code, symbol, side="sell", intent_type, quantity, reason, metadata, order_type="market",
   time_in_force="day")`. **`metadata["reference_price"]` = the leg price (§1 decision).** Plus a marker
   `metadata["oms_v2_managed_exit"]="true"` for auditability.
5. `reports = await self.broker_adapter.submit_order(request)` → `_record_order_reports(...)`.

**THE LOAD-BEARING INVARIANT:** the exit order's `broker_account_name` is **always** the managed row's
account (`paper:schwab_1m_v2`) — never derived from anywhere else. Routing is **by account** (§3), so
pinning the account to the row's account is what guarantees simulated routing. A single helper
`_emit_v2_managed_sell(session, row, *, intent_type, quantity, reference_price, reason)` centralizes this
so there is exactly ONE place that builds a v2 sell, and it can't use any other account.

`close`/`scale` already flow through the same `broker_adapter.submit_order(request)` as opens — routing is
account-name-based, not intent-type-based (verified `service.py:330-463`) — so v2's OMS-emitted exits
inherit v2's paper routing identically.

---

## 3. Paper-isolation survival re-proof — THE DEPLOY GATE

Three layers; the third is the new survival test that gates the deploy.

**Layer 1 — config (existing, re-asserted):** `provider_for_account("paper:schwab_1m_v2") == "simulated"`
(`settings.py:601`), and `paper:schwab_1m_v2` is **refused** by `configured_schwab_accounts`
(`broker_adapters/schwab.py:31` — `if account_name == …schwab_1m_v2_account_name: return`). Already covered
by `tests/unit/test_p1_v2_paper_routing.py` — re-assert in the slice-3 suite.

**Layer 2 — routing (by construction):** a v2 exit order carries `broker_account_name="paper:schwab_1m_v2"`
→ `RoutingBrokerAdapter._adapter_for_account` → `provider_by_account["paper:schwab_1m_v2"]="simulated"` →
`SimulatedBrokerAdapter`. Assert the helper `_emit_v2_managed_sell` always sets the account to the row's
account, and that resolution returns the simulated adapter.

**Layer 3 — survival / fault-injection (the gate):** build the OMS with **v2 (simulated) AND a live Schwab
bot enabled + a real `schwab_account_hash`** so the `RoutingBrokerAdapter` holds BOTH a simulated and a
real Schwab adapter in one process. Then drive a **full open→quote→exit cycle** for a v2 position
(managed row → quote breaches the −1.5% stop) and assert:
- (a) the emitted exit **fills on the SimulatedBrokerAdapter**;
- (b) **`SchwabBrokerAdapter.submit_order` is NEVER called** (spy/patch it, assert zero calls) — and
  ideally `SchwabBrokerAdapter` is never even constructed for the v2 order;
- (c) **fault injection:** force-attempt a misroute — e.g. a v2 sell with the account tampered toward a
  schwab-bound account, or a config where a stray hash entry would exist — and assert the
  `configured_schwab_accounts` refusal + `provider_for_account=simulated` **still** block it (a real order
  is never constructed). This proves the isolation is *structural*, not incidental to the happy path.
- (d) repeat for a **SCALE** sell (partial), not just the full close — both legs must route to simulated.

**Gate rule:** slice 3 does **not** deploy until Layer-3 is green. The survival test is the verdict, exactly
like the P0 token-refresher and P1 Phase-1 survival proofs.

---

## 4. Flag / dormancy

Same single flag `oms_v2_exit_management_enabled` (default OFF). OFF →
`self._managed_v2_symbols` stays empty (slice 1 doesn't populate it when OFF), the quote path never calls
`_evaluate_v2_managed_exit`, and no sell is ever emitted — **byte-identical to today.** When the operator
flips it ON (attended, after the survival proof), the whole v2-exit system activates coherently (slice-1
rows created, slice-2 gateway registration, slice-3 eval + sells). **Activation is the deploy gate event**
— attended, after-close or flat, with the survival proof green.

---

## 5. Tests (the build will carry these)

- **Ladder eval (unit, fake quote + in-memory row):** stop breach → one CLOSE for full qty, row closed;
  floor breach → CLOSE; scale tier → SCALE for `sell_qty`, row decremented + `scales_done` appended;
  precedence hard>floor>scale; one-action-per-quote; quote→Position state persists each quote (the
  co-located update); stale-quote → no exit.
- **No-double-close:** a second quote after a CLOSE emits nothing (row closed, symbol dropped).
- **Emit routing (the invariant):** every `_emit_v2_managed_sell` order has `broker_account_name=` the row
  account; `reference_price` set per §1.
- **Dormant when OFF:** flag OFF → quote eval never runs, zero sells, `_managed_v2_symbols` empty.
- **Paper-isolation survival (Layer 3 — THE GATE):** the open→quote→exit cycle with Schwab live in-process
  fills on simulated, Schwab adapter never called; fault-inject misroute still blocked; scale leg too.
- Existing OMS + v2 suites unchanged (gated branch; no behavior when OFF).

---

## 6. Open decisions for operator review (before build)

1. **Eval/fill price source — A (bid, recommended) vs B (leg level / re-score parity).** §1.
2. **Eval cadence guard** — evaluate every quote for managed symbols, or throttle (e.g. ≥250ms/symbol)?
   (Per-quote is precedented by the quote-drift cancel; managed symbols are few, so per-quote is fine —
   recommend per-quote, no throttle initially.)
3. **Hard-stop session-bounded?** The momentum native stop only arms during RTH. Should v2's quote-driven
   hard stop also be RTH-gated, or run extended-hours too (v2 trades pre/after-market)? **Recommend: run
   whenever a fresh quote exists** (v2's edge is pre/after-market) — but flag it for your call.

---

## 7. Honest boundaries

- This is the slice that **emits sells** — hence the survival proof is the gate, not a formality.
- Idealized fills (full qty at `reference_price`, no slippage/partials) → slice-3 live-paper is **pipe
  validation, not a track record**; real fills need Track-3 ticks / a real broker. The hard-stop on a
  per-quote (bid) basis can still slip vs a true intrabar tick.
- Tier MACD/stoch exits are **slice 4** (v2 publishes its indicators); v1 of the ladder is hard-stop +
  floor + scale — the risk core.
- Nothing deploys until Layer-3 survival is green and you give the attended go.
