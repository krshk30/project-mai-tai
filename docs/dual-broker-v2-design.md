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

**Status (2026-07-10):** ✅ **CODE-COMPLETE, flag-off, NOT deployed** (build ledger §14, PRs #422–#430). Runs on the
operator's existing accounts (Schwab + Webull `live:orb`). **Next:** qty-1 harness on `live:orb` scheduled Mon
2026-07-13 (pre-market 07:15 ET + RTH 10:15 ET, §10) → then enable is a separate, staged, safety-gated decision (§11).

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

**⚠️ TWO capture requirements (verified during PR #2 — the experiment is only as good as these):**
1. **REAL broker fill time+price on BOTH legs.** `store.record_fill_if_needed` faithfully persists
   `price=report.fill_price`, `filled_at=report.reported_at`, `broker_fill_id`. Schwab's adapter sets
   `reported_at` from `closeTime` (real fill time). **BUT the Webull adapter (`webull.py` `fetch_order_update`)
   OMITS `reported_at`** → the Webull leg's `fills.filled_at` defaults to record/poll time, NOT the real broker
   fill timestamp. **REQUIRED FIX before ENABLE (a required experiment-validity item, not a flag-off blocker):**
   populate Webull `reported_at` from Webull's fill-time field (determine the exact field from a real fill during
   the qty-1 test / SDK — the adapter already extracts `filled_price`/`filled_qty` from `items[0]`). Without it,
   the fill-LATENCY half of the A/B is invalid for the Webull leg (the fill-PRICE half is fine).
2. **Webull REJECT is a RESULT, not just a safety event.** A Schwab-ineligible name rejects on Webull too; that
   reject IS the answer to "can Webull trade this." The mirror records rejected reports to `broker_orders`
   (status=rejected + reason) and swallows only the exception path — so the reject lands as data.
   `broker_ab_report` MUST read `broker_orders` rejects + reasons (not only `fills`) to surface
   "Webull rejected N% of confirmed names / with reasons X".

## 5. Account = the EXISTING `live:orb` Webull account (+ collision guard) — REVISED 2026-07-10
> **Superseded the original "separate `live:v2_webull` account" plan.** The operator has **only one Webull
> account** (ORB's) and one Schwab account — there is no second Webull account to provision, and building for a
> hypothetical one was a misread. The mirror therefore routes v2's Webull leg to the **existing `live:orb`** account
> (`strategy_schwab_1m_v2_webull_account_name` default `live:orb`; `configured_webull_accounts` confirms `live:orb`
> is the sole Webull account). No ops provisioning step remains.

The collision risk that motivated a separate account (ORB + v2 both touching the same Webull account) is handled by
a **collision guard inside the mirror** (PR #428): before mirroring a v2 open to `live:orb`, skip if that symbol is
already (a) armed in `_armed_hard_stops` for the account, (b) an open managed position, or (c) a non-zero account
position. So a symbol ORB already holds is never double-entered, and the OMS scoping invariant (OMS touches only
positions it placed) keeps the two strategies' positions separately owned on the shared account.

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

## 6b. FINDING (grep, during PR #1): the CW ladder was SINGLE-ACCOUNT-hardcoded
The v2 CW exit ladder (`_evaluate_v2_managed_exit`) read a hardcoded `strategy_schwab_1m_v2_account_name`, and
`_managed_v2_symbols` was a symbol-only `set[str]` — so only the ARMED-STOP backup was per-account (F2), NOT the
full ladder. Mirroring the open alone would leave the Webull leg with only the native −5% stop, no CW ladder.
**PR #1 (account-aware CW-exit refactor) fixes this:** `_managed_v2_symbols` → `set[(account, symbol)]`, a
`_v2_accounts()` helper (single account unless the mirror flag is on), and the eval/rehydrate/dispatch iterate
accounts. Byte-identical when single-account (flag off). This is the prerequisite for the fan-out PR.

## 7. Exits on Webull — the load-bearing risk
The v2 CW **multi-leg** ladder (partial +2% / floor / 2% trail / −5% hard stop / bar-close flip) must run on the
Webull adapter for the Webull leg. Reuse ORB's proven fixes (#386 STOP→STOP_LOSS, #375 fill polling, #374 4-dec,
limit+session EH). ORB only proved a *single* stop-exit on Webull — the multi-leg ladder (partial fills,
cancel-of-siblings, EH) is where surprises live. This is what the qty-1 test (§10) shakes out.

## 8. Reconciliation / capital / risk — per-account, 2× accepted
- Reconciler runs per-account; `live:orb` is already enumerated (ORB uses it).
- **Capital 2×:** the Schwab leg and the `live:orb` Webull leg each hold a full-size position on a mirrored US
  trade. Operator-accepted for the eval. Sizing = per-order qty on each broker; no cross-account netting.
- On `live:orb`, ORB and the v2 mirror **coexist**: the collision guard (§5) prevents double-entry of the same
  symbol, and the scoping invariant keeps each strategy's positions separately owned.
- Scoping invariant already per-broker — each leg is a v2-owned position on its account.

## 9. Flag + merge gate
- `MAI_TAI_..._WEBULL_MIRROR_ENABLED` default **False** → byte-identical off (no fan-out).
- **Merge gate:** byte-identical-off proven (behavioural + value-identical) + the #404 rehydrate test (§6).
- Genuine-green CI, no admin. Attended flag-off deploy, fleet-flat.

## 10. qty-1 Webull test — exercises EVERY ladder leg (gates ENABLE, not merge)
On `live:orb`, drive/verify **every** CW exit-ladder SHAPE on Webull (not one fill): marketable entry → STOP_LOSS
hard-stop (accept, no 417) → LIMIT scale/floor → flatten, plus fill-poll / 4-dec / real broker fill-time (#425);
each leg's submit→fill→cancel-of-siblings confirmed. **Do not enable until every shape passes.**

**Harness: `scripts/v2_webull_qty1_harness.py`** — direct-adapter, qty 1, `--confirm` required; `try/finally`
ALWAYS cancels resting orders + flattens + verifies FLAT. RTH mode = MARKET entry/flat; **`--session AM|PM`** =
extended-hours (marketable LIMIT + session token, LIMIT-only per #429) with **`--auto-price`** (live `massive`
snapshot → ±5% marketable limits). **SCHEDULED (2026-07-10): two one-off systemd timers on the box run it on
`live:orb` Mon 2026-07-13 — pre-market `@AM` 11:15 UTC (07:15 ET) + RTH `@RTH` 14:15 UTC (10:15 ET, deliberately
AFTER ORB's 09:30–10:00 window)** via `scripts/run_v2_webull_harness.sh` (systemd oneshot, inherits the OMS
`EnvironmentFile`, ntfy verdict to `mai-tai-preopen-28806a5a97b7`). Timers:
`project-mai-tai-webull-harness-{am,rth}.timer` → `project-mai-tai-webull-harness@{AM,RTH}.service`.

## 11. Enable — STAGED, safety-gated (NOT profitability-gated)
1. **qty-1 plumbing test** (§10, every leg) passes on `live:orb` (scheduled Mon 2026-07-13 pre-market + RTH).
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
6. **Enable** (separate): qty-1 passes on `live:orb` → attended flag-off deploy (git pull + one OMS restart) →
   flip `strategy_schwab_1m_v2_webull_mirror_enabled` → after-hours smoke → RTH → 1-month eval → retire the loser.

## 13. Open questions
- Confirm the fan-out submits are independent (one broker rejecting/erroring must NOT block the other leg) —
  handled: the mirror is an all-swallowing independent post-step after the primary commit.

## 14. BUILD LEDGER — code-complete, flag-off, NOT deployed (2026-07-10)
All merged to main; runs on the operator's existing accounts (Schwab `live:schwab_1m_v2` + Webull `live:orb`);
flag-off = byte-identical; deploys on the next attended OMS restart on operator GO.
- **#422** account-aware CW-exit refactor (`_managed_v2_symbols` → `set[(account,symbol)]`, `_v2_accounts()`).
- **#424** `_maybe_mirror_v2_open` fan-out (independent all-swallowing post-step; mirrors Schwab v2 open → Webull).
- **#425** Webull `reported_at` from the broker fill timestamp (`last_filled_time`), not poll time.
- **#427** `scripts/broker_ab_report.py` — Schwab-vs-Webull A/B comparison (read-only).
- **#426** qty-1 Webull plumbing harness (every ladder shape).
- **#428** default mirror account → `live:orb` + collision guard (§5).
- **#429** Webull adapter proper pre- AND post-market (extended-hours) support.
- **#430** harness extended-hours (AM/PM) mode + `--auto-price` + scheduled runner (§10).
- Comparison metric weights for the retire decision (slippage vs latency vs fill-rate) — operator to weight at
  month-end.
