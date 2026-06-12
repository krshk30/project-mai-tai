# Design — v2 sim-fill fix: emit `reference_price` + persist reject reasons

**Status:** design for review. No code yet. Deploy AFTER CLOSE (needs v2 + oms restart),
bundled with the PR #282 after-close slot. **No entry/exit-rule change, no OMS routing change.**

## Problem (pinned to code, 2026-06-12 pre-flight)

P1 routed v2 to the simulated provider, but the **first live sim order rejected**, not filled:
- MASK intent `11:17:07` → routed to sim (`broker_order_id=sim-order-…`) → **risk passed**
  (`risk_checks.outcome=pass`) → broker_order **`status=rejected`**.
- `SimulatedBrokerAdapter.submit_order` (`broker_adapters/simulated.py:41-54`) **requires
  `metadata["reference_price"]`**; if absent/empty it returns an `event_type="rejected"` report
  with `reason="missing reference_price"`, then fills at `Decimal(reference_price)`.
- **v2's intent emitter** (`strategy_core/schwab_1m_v2.py` `_evaluate_completed_bar`) sets
  `entry_price`, `vwap`, `macd_*` — **but not `reference_price`.** So every v2 sim order rejects.
- **Why P1's test missed it:** `test_simulated_sink_fills_v2_order_and_opens_position` hand-set
  `metadata={"reference_price":"5.00"}` — it validated the adapter, not v2's *real* emitted
  metadata. That injected field is exactly the masking gap this design closes.

Flow confirmed: `TradeIntentDraft.metadata` → `SchwabV2IntentEmitter.emit` (`TradeIntentPayload`)
→ OMS `OrderRequest(metadata=dict(event.payload.metadata))` (`oms/service.py:459`) → adapter. So
adding the field in the emitter reaches the adapter unchanged.

## Fix 1 — emitter sets `reference_price`

In `_evaluate_completed_bar`'s returned `TradeIntentDraft.metadata`, add:
```python
"reference_price": f"{cur.close:.4f}",   # == entry_price (signal bar close)
```

**Source choice — recommend the signal bar close (= `entry_price`).** Honest scope note (consistent
with the recorded sim-fill scope, the "optimistic upper bound"):
- **Bar close (recommended):** deterministic, reproducible, identical to the value already recorded
  as `entry_price`, no dependency on quote freshness. It is the **most idealized** choice — the sim
  fills at the exact signal price with zero latency/slippage. Correct for **pipe validation**; it is
  NOT realistic execution.
- **Live ask (alternative, flagged):** more realistic for a buy, but (a) v2's `last_quote` may be a
  few seconds stale at bar close, (b) it's *still* idealized (no partials/queue), and (c) it couples
  fill price to the quote loop. Marginal realism gain for added coupling.
- **Realism does not come from this field at all** — it comes from the future slippage-modeling sink
  (or real Schwab fills). So pick the simplest faithful value now: **bar close.**

⚠️ **Operator decision point:** bar close vs live ask. Recommendation: bar close. (No strategy
*decision* changes either way — `reference_price` is execution metadata consumed by the sink, not a
gate; the entry rule is untouched.)

## Fix 2 — persist the adapter's reject reason

`broker_orders` has no `reject_reason` column; the reason belongs in `payload` (jsonb). Today
`order.payload = dict(report.metadata)` (`oms/store.py:323` create, `:390` update) — it never
includes `report.reason`, so a rejection's reason is only in `broker_order_events`, not the order
row. That's why this morning's `broker_orders.payload->>'reject_reason'` was empty and slowed the
diagnosis.

**Fix (centralized in the store):** in `record_order` and `update_order_from_report` — both already
receive `report` — when `report.event_type == "rejected"` and `report.reason`, persist
`order.payload = {**dict(metadata), "reject_reason": report.reason}`. One locus covers every order
path (open / cancel / stop / sync). Small, and it pays for itself the next time anything rejects.

## Tests — close the masking gap (no hand-injected metadata)

1. **Real-emit-path sim-fill (the regression guard):** drive a genuine signal through
   `SchwabV2Strategy` — feed a ≥135-bar warmup sequence engineered to produce a MACD/VWAP cross so
   `on_bar` returns a real `TradeIntentDraft` — then take **`draft.metadata` verbatim**, build the
   `OrderRequest` the way the OMS does (`metadata=dict(payload.metadata)` with the emitter's
   `TradeIntentPayload` shape), submit to `SimulatedBrokerAdapter`, and assert **`filled`** + a
   position at `reference_price`. **No `reference_price` is hand-set anywhere** — it must come from
   the strategy's own emit code. If the emitter ever stops setting it, the adapter rejects and this
   test fails. This is the test P1 should have had.
2. **reject_reason persistence:** a rejected `ExecutionReport` (reason set) → `update_order_from_report`
   / `record_order` → assert `broker_orders.payload["reject_reason"]` equals the report reason.
3. Keep P1's existing tests green (they still validate the adapter + the hash-guard).

## Scope / non-goals

- **No entry/exit-rule change** — `reference_price` is execution metadata, not a strategy gate; the
  v2 decision logic is byte-unchanged.
- **No OMS routing change** — provider stays `simulated`; the hash-guard is untouched.
- v2-scoped emitter change + a generic (all-strategy-benefiting) reject_reason persistence fix.

## Deploy (AFTER CLOSE, attended — bundle with the #282 slot)

- **Restarts:** v2 (emitter runs in the v2 bot process) **+ oms** (reject_reason fix runs in
  oms-risk). Same oms+v2 bundle as the P1 deploy; after-close, account-flat. No mid-session deploy.
- **Today:** v2 signals reject-but-record. The replay study runs on **signals + bars** (both already
  verified), not fills — so nothing is lost by waiting one more fill-less day.
- **Verify on the next natural signal (tomorrow premarket, realistically):** v2 signal → sim
  **FILLED** (not rejected) → a `virtual_positions` row opens at the signal close; and a forced/observed
  rejection now shows `reject_reason` in `broker_orders.payload`.
- **Rollback:** revert the PR + restart v2/oms → identical to today (reject-but-record).
