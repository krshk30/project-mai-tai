# Dual-broker v2 — design doc (dual-execution BROKER BAKE-OFF)

**Purpose (revised 2026-07-10, operator):** this is **not** a foreign-name router — it is a **broker A/B
evaluation.** Mirror every v2 trade to **BOTH** broker accounts (Schwab + Webull) **at the same instant**, run each
leg's CW exit independently, and record per-broker execution (fill price, slippage, submit→fill latency, P&L). After
~a month of head-to-head on the **US names both brokers accept**, decide which broker executes better and **retire
the loser** (operator expects to keep Schwab/TOS and close Webull if its latency is as bad as suspected — but wants
it *measured*, not assumed).

**2× exposure is intentional and accepted** ("don't worry about 2× win/lose — I'm evaluating the broker"). The
strategy's own profitability (net-negative at real latency, per the broker-aware backtest) is a **separate**
question; even a losing strategy cleanly reveals which broker fills better. So the enable is a deliberate,
cost-accepted evaluation run — SAFETY-gated (plumbing works), not profitability-gated.

**Status:** BUILD approved flag-off. Enable = a separate, staged, safety-gated decision (§11). Target: code-complete
+ validated by end of next week.

---

## 1. What already exists (why the OMS can carry this)
- **Per-account everything.** Routing (`_adapter_for_account`, service.py:3854 via `RoutingBrokerAdapter`),
  managed positions (`oms_managed_positions.broker_account_name`), armed stops (`oms_armed_stops.broker_account_name`),
  and F2 rehydrate all key on the broker account. Adapters incl. **webull** (`_build_provider_adapter`, 2765).
- **The unique open-row constraint is per-account:** `uq_oms_managed_positions_open_symbol (broker_account_name,
  symbol)`. Two *different* accounts holding the **same symbol** = two separate rows → **NO collision.** This is what
  makes mirroring the same trade to both brokers legal in the schema.
- **The OMS already multiplexes exits by strategy per leg** (`_armed_hard_stops` = ORB trail; `_managed_v2_symbols`
  → `_evaluate_v2_managed_exit` = v2 CW ladder). Two v2 legs of the same symbol on two accounts each run their **own**
  CW ladder on their **own** broker, independently. → **The OMS can run the same trade on two brokers at once.**

## 2. Routing = FAN-OUT (mirror to both, parallel — no round-trip)
When `WEBULL_MIRROR_ENABLED`, a single v2 open intent is submitted to **BOTH** accounts **simultaneously**:
- `live:schwab_1m_v2` (Schwab) AND `live:v2_webull` (Webull), fired in parallel — **same instant, no try-then-wait.**
  This is what fixes the latency worry: Webull isn't waiting on a Schwab reject; both get the order at once.
- **US names:** both accept → **two legs** = the true head-to-head (same signal, same time, compare fills).
- **Foreign / Schwab-ineligible names:** Schwab rejects (must-use-a-broker) → **Webull-only leg.** No comparison
  there, but it still trades. → **The comparison dataset is the US names both brokers fill.**
- **No eligibility guessing, no double-fill trap** — we *want* both to fill on US names; that's the test.

Flag OFF = no fan-out; v2 submits only to Schwab exactly as today (byte-identical).

## 3. Two managed legs per trade
Each fill writes its own managed row (`broker_account_name` = the Schwab acct OR the Webull acct), its own armed
stop, its own CW ladder state. Exits route per-row to the correct adapter. Schema already supports it (§1). Each
leg rehydrates independently on restart from its own account rows.

## 4. Comparison telemetry — the actual deliverable
The point of the whole build is the month-end verdict, so we capture per-broker, per-trade:
- **Fill price vs signal reference** (slippage) — the headline broker-quality metric.
- **Submit→fill latency** — measured, not assumed (Webull's real number vs Schwab's).
- **Per-leg realized P&L** and fill/reject/partial counts.
Sources already tag the broker (`fills.broker_account_id`, `broker_orders`), so this is largely a **reporting
script** (`scripts/broker_ab_report.py`) that diffs the two legs of each mirrored trade. Deliver a weekly/monthly
Schwab-vs-Webull comparison → the retire-the-loser decision.

## 5. Separate `live:v2_webull` account (NOT `live:orb`)
Mirroring needs v2's Webull leg on its **own** account: sharing `live:orb` collides (ORB + v2 same universe → the
per-account unique-open row), and entangles reconciliation / protected symbols / ORB's cap. **OPS STEP (before
ENABLE, not merge):** provision `live:v2_webull` + wire credentials/hash into the routing map. Build (flag-off)
doesn't need it to exist.

## 6. Schema — GREP-VERIFIED: NO new field; routing PR earns the #404 rehydrate test
`broker_account_name` already exists on `oms_managed_positions` AND `oms_armed_stops`, and F2
`_rehydrate_armed_hard_stops` already reads it (`_hard_stop_key(strategy, account, symbol)` → re-arms
`ArmedHardStop(broker_account_name=...)`). → **No new field.** Because it IS read on rehydrate, the routing PR
earns a **#404-style rehydrate/survival test**:
1. **Flag OFF = byte-identical:** no fan-out → v2 legs only ever on `live:schwab_1m_v2` → rehydrate/exit values
   unchanged vs today. Prove it.
2. **Flag ON = both legs rehydrate:** a mirrored trade arms two legs (Schwab + Webull) → persists → **restart OMS**
   → **both** rehydrate on their own accounts → each re-arms its CW stop on the correct adapter → both exit
   lifecycles intact.

## 7. Exits on Webull — the load-bearing risk
The v2 CW **multi-leg** ladder (partial +2% / floor / 2% trail / −5% hard stop / bar-close flip) must run on the
Webull adapter for the Webull leg. Reuse ORB's proven fixes (#386 STOP→STOP_LOSS, #375 fill polling, #374 4-dec,
limit+session EH). ORB only proved a *single* stop-exit on Webull — the multi-leg ladder (partial fills,
cancel-of-siblings, EH) is where surprises live. This is what the qty-1 test (§10) shakes out.

## 8. Reconciliation / capital / risk — per-account, 2× accepted
- Reconciler runs per-account; verify it enumerates `live:v2_webull`.
- **Capital 2×:** each account holds a full-size position on a mirrored US trade. Operator-accepted for the eval.
  Sizing = per-order qty (currently 4) on each broker; no cross-account netting.
- Protected symbols per-account (v2-Webull has no manual holdings).
- Scoping invariant already per-broker — each leg is a v2-owned position on its account.

## 9. Flag + merge gate
- `MAI_TAI_..._WEBULL_MIRROR_ENABLED` default **False** → byte-identical off (no fan-out).
- **Merge gate:** byte-identical-off proven (behavioural + value-identical) + the #404 rehydrate test (§6).
- Genuine-green CI, no admin. Attended flag-off deploy, fleet-flat.

## 10. qty-1 Webull test — exercises EVERY ladder leg (gates ENABLE, not merge)
On `live:v2_webull`, drive/verify **every** CW exit leg on Webull (not one fill): entry → +2% partial → +2% floor
→ 2% trail → −5% hard stop → bar-close flip, plus fill-poll / 4-dec / EH sessions; each leg's submit→fill→
cancel-of-siblings confirmed. **Do not enable until every leg passes.**

## 11. Enable — STAGED, safety-gated (NOT profitability-gated)
1. **qty-1 plumbing test** (§10, every leg) passes on `live:v2_webull`.
2. **Flag ON → after-hours live smoke:** one real mirrored trade in a **slow / extended-hours** market; watch BOTH
   legs' full entry→CW-exit lifecycle on real money.
3. **Then RTH:** run the mirror during regular hours → accrue the Schwab-vs-Webull comparison.
4. **Run ~1 month** on US names → §4 report → **retire the loser.**
Note: the confirmed-window strategy is net-negative at real latency, so the eval runs at a (2×, accepted) P&L cost.
That's the price of measuring the brokers; the strategy-profitability decision (mean-positive) runs in parallel and
is separate from the broker verdict.

## 12. Sequence
1. Design (this) → review.
2. **Fan-out routing PR** (flag-off): mirror v2 opens to both accounts; two managed legs; byte-identical-off + #404
   rehydrate test.
3. **Webull v2-exit adapter PR** (flag-off): CW ladder on Webull, reusing #386/#375/#374/EH.
4. **Comparison report** `scripts/broker_ab_report.py` (§4).
5. **qty-1 harness** (§10).
6. **Enable** (separate): ops provisions `live:v2_webull` → qty-1 passes → after-hours smoke → RTH → 1-month eval.

## 13. Open questions
- `live:v2_webull` account handle + credentials (ops, before enable).
- Confirm the fan-out submits are independent (one broker rejecting/erroring must NOT block the other leg).
- Comparison metric weights for the retire decision (slippage vs latency vs fill-rate) — operator to weight at
  month-end.
