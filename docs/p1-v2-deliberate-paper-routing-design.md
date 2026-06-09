# Design (P1): make schwab_1m_v2's "paper" deliberate + structural — it must not be able to reach the real account

**Status: DESIGN — read-only, for review before any code.** Design-first, same discipline as the
rest. This closes a **latent-live gap on a real brokerage account** and the operator ranks it the
top urgency — above the token work (P0). v2 stays PAPER; this makes that *structural*, not accidental.

---

## The problem (confirmed from code 2026-06-09)

v2's "paper" safety is **accidental** and it is pointed at the **real** account:

- `provider_for_account("paper:schwab_1m_v2")` → `provider_for_strategy("schwab_1m_v2")` →
  `strategy_schwab_1m_v2_broker_provider`, which **defaults to `"schwab"`** (settings.py:164) →
  the OMS routes v2's orders to the real `SchwabBrokerAdapter`, which resolves the account hash via
  the **shared real hash `2EE5A4…`**.
- v2 is **only NOT trading real money because no `configured_schwab_accounts` entry happens to
  exist** for `paper:schwab_1m_v2` → `submit_order` rejects "missing Schwab account hash". An
  *accidental* safety net on a real brokerage account, one config line from live.
- **The codebase already KNOWS this:** `settings.display_account_name` (settings.py:573) has
  `if provider == "schwab" and account.startswith("paper:"): return f'live:{…}'` — it renames v2's
  account to `live:` *for display honesty*. So the system is aware `paper:`+schwab = really live, but
  fixes it **only cosmetically (display), not structurally (routing).** That one line is the gap.

**Goal:** make v2 route to a genuine paper/simulated sink so it **cannot reach the real account hash
even if someone adds the `configured_schwab_accounts` entry** — deliberate and structural.

---

## Confirmed mechanics (read-only)

- **Routing lever:** `strategy_schwab_1m_v2_broker_provider`. Set it to `"simulated"` →
  `provider_for_account(v2)` returns `"simulated"` → the OMS `RoutingBrokerAdapter` sends v2's orders
  to `SimulatedBrokerAdapter`, **never** `SchwabBrokerAdapter`. Routing happens at the *provider*
  level, **before** any account-hash lookup — so a Schwab hash entry becomes irrelevant for v2.
- **The safe sink exercises fills:** `SimulatedBrokerAdapter.submit_order` returns `accepted`+`filled`,
  fills at `reference_price`, `_apply_fill` updates quantity/avg_price → positions. **The OMS already
  populates `reference_price`** in order metadata (oms/service.py:1464/1474/2247…). → routing v2 to
  sim **finally validates execution** (order → fill → position → P&L), which has *never* happened
  (v2 has 0 fills / 0 positions all along). No creds, no external venue, can't reach Schwab.
  - Alternative sink: `AlpacaPaperBrokerAdapter` = a *real* paper venue (more realistic fills/rejects)
    but needs `api_key`+`secret_key` (idle if absent). For immediate safety + deterministic execution
    validation, **SimulatedBrokerAdapter is the recommended sink**; Alpaca-paper is an optional later upgrade.
- **No `paper:` prefix enforcement exists today** — `"paper:"` is purely cosmetic (only the display
  rename references it). The structural guarantee has to be added.

---

## ⚠️ Critical interaction with P0 (token refresh) — drives the sequencing

The retired bots (`paper:macd_30s`, `paper:schwab_1m`) are **load-bearing for token refresh** (P0):
the OMS instantiates a `SchwabBrokerAdapter` (which incidentally refreshes the shared token) **only
because some account still has `provider="schwab"`**. So **how** we make v2 paper matters:

- **v2-scoped change (safe now):** flipping only `strategy_schwab_1m_v2_broker_provider` to
  `"simulated"` leaves the retired bots at `provider="schwab"` → the OMS still builds a
  `SchwabBrokerAdapter` → **token refresh keeps working**. P1 lands without disturbing P0.
- **Broad `paper:`→sim enforcement (would trigger the P0 SPOF):** forcing *every* `paper:` account to
  simulated removes the *last* `schwab`-provider account → the OMS drops `SchwabBrokerAdapter` →
  **nothing refreshes the token → v2's data path dies at next expiry.** This is the exact SPOF P0 fixes.

**Therefore the structural enforcement is sequenced, not all-at-once.**

---

## Proposed design — layered, sequenced

### Phase 1 (NOW — P1, safe before P0): make v2 deliberately paper, two structural layers — both v2-SCOPED
1. **Route v2 to simulated by changing the DEFAULT.** `strategy_schwab_1m_v2_broker_provider`
   default `"schwab"` → `"simulated"` (safe-by-default; not just an env override that can be lost).
   v2's orders → `SimulatedBrokerAdapter`. Real-Schwab becomes an explicit opt-in at go-live. This is
   **v2-scoped** → does **not** disturb the retired-bot token-refresh dependency (P0 intact).
2. **Hash-side guard (defense-in-depth) — V2-SCOPED ONLY in Phase 1.** Make `configured_schwab_accounts`
   refuse to register **`paper:schwab_1m_v2` specifically** (not all `paper:` accounts — that's Phase 2).
   So even if v2's provider were later flipped back to `"schwab"`, v2's account can never bind a real
   hash → reject. Turns today's *accidental* missing-entry safety net into an *intentional, enforced* one.
   - **⚠️ Why v2-scoped, not broad (CONFIRMED 2026-06-09):** the incidental token refresh is triggered
     by the OMS broker-sync calling `SchwabBrokerAdapter.list_account_positions` on a **configured**
     schwab account — that makes the `_authorized_request_json` REST call → `_get_access_token` →
     refresh. `list_account_positions` returns `[]` **without any REST call** when the account is
     **not** in `accounts_by_name`. So a **broad** guard that removes the retired bots
     (`paper:macd_30s`/`paper:schwab_1m`) from the map → their sync makes no REST call → **no token
     refresh → the P0 SPOF.** The retired bots are load-bearing **specifically because their
     position-sync triggers the refresh.** Hence: keep them registered; guard only v2's account in Phase 1.

→ Net after Phase 1: v2 orders fill in sim (execution mechanically validated — see scope note below),
and v2 **cannot reach the real account even if its entry is added** (provider routes to sim; and the
v2-scoped hash-guard refuses its bind). Both v2-scoped → retired bots untouched → P0 refresher intact.

### Phase 2 (WITH / AFTER P0): full `paper:`-prefix enforcement — BOTH routing AND hash-guard go broad
Once the **dedicated refresher (P0)** owns token freshness independent of any schwab-provider account,
broaden BOTH structural rules fleet-wide:
- **Routing:** a `paper:`-prefixed account **cannot resolve to a real broker provider** (force
  `simulated`, or raise).
- **Hash-guard:** `configured_schwab_accounts` refuses **all** `paper:` accounts (the broad version of
  the Phase-1 v2-scoped guard).

Both are safe to broaden **only after P0**, because each removes the retired bots' schwab footprint
that currently triggers the incidental refresh (routing drops the `schwab` provider; broad hash-guard
drops them from `accounts_by_name` → their sync makes no REST call). This turns the
`display_account_name` awareness into real enforcement fleet-wide.

### Go-live ceremony (deliberate, later — paper→real-money)
Real-money conversion becomes an explicit, attended rename: `paper:schwab_1m_v2` → `live:schwab_1m_v2`
(matching the existing `live:polygon_30s` convention), set provider back to `schwab`, add the
`configured_schwab_accounts` entry, account-flat + CYN + attended. The `paper:`→sim rule (Phase 2)
then *gates* go-live behind that deliberate rename — you can't be live while named `paper:`.

---

## Validation / test plan
- **Execution finally validated:** route v2 to sim → confirm orders **FILL** (broker_orders `filled`,
  `virtual_positions` open/close, realized P&L populated) — the first real exercise of v2's order path.
- **Structural safety proven:** with v2 on `simulated`, **add** a `configured_schwab_accounts` entry
  for `paper:schwab_1m_v2` and confirm v2 **still routes to sim** (the entry is inert) — i.e. it
  cannot reach the real account even with the entry present. And confirm the hash-guard **refuses**
  to register a `paper:` account.
- **P0 untouched:** confirm the OMS still builds a `SchwabBrokerAdapter` (retired bots still
  `provider=schwab`) → token still refreshes → v2 data path stays alive. (Re-run after Phase 2 only
  once P0's dedicated refresher is in place.)

---

## ⚠️ Sim fill model — honest scope (what Phase 1 actually proves)

`SimulatedBrokerAdapter` (full read 2026-06-09) uses an **idealized** fill model:
- **Instant, full fill at the exact `reference_price`** (`fill_price = reference_price`,
  `filled_quantity = request.quantity`). **No slippage, no partial fills, no latency.**
- **No market/liquidity/halt/buying-power rejections** — the only rejects are a cancel intent
  (nothing to cancel) and missing `reference_price`. **Sells never short** (sell = `min(held, qty)`,
  flattens at 0).
- Positions tracked (weighted-avg cost); realized P&L is derived downstream from fill prices.

**So Phase 1 proves "THE PIPE WORKS END-TO-END" — signals → orders → route → fill → position → P&L
computes — which is real and more than we have now (v2 has had 0 fills ever). It does NOT prove v2's
execution is viable under realistic conditions.** Idealized fills will make paper-sim P&L
**optimistic vs reality**, especially for the **illiquid penny stocks v2 trades**, where real
slippage / partial fills / rejections are severe. Realistic execution validation still waits for
go-live (real Schwab fills) or a future slippage-modeling sink. **Do not over-trust paper-sim P&L at
the real-money conversation** — same scope discipline as the signals-vs-fills note (handoff P1).

---

## Recommendations (for implement-review — the two that affect Phase 1)

- **Sink → `SimulatedBrokerAdapter`.** Phase 1's job is safety (can't reach real account) + mechanical
  execution validation; the simulated sink achieves both with **zero creds, zero external dependency,
  zero risk**, and routing happens before any hash lookup. `AlpacaPaperBrokerAdapter` adds creds + an
  external venue + open questions on whether Alpaca's paper venue even *covers/fills v2's illiquid
  penny stocks* + a different data feed — more surface for fills that **still aren't Schwab**. Since
  realistic execution is inherently a go-live question, don't over-invest now. (Flag AlpacaPaper or a
  slippage-modeling sim as a *future* "more realistic paper" upgrade if pre-go-live realism is wanted.)
- **Mechanism → default-flip** (`strategy_schwab_1m_v2_broker_provider` default `"schwab"`→`"simulated"`
  in settings.py), **plus** set the VPS env explicitly for a self-documenting running config. Rationale:
  the dangerous `"schwab"` default *is* the latent-live bug — fixing the default removes the trap at the
  source (safe even on a fresh deploy / lost env), which an env-override-only does not. Go-live then
  becomes an explicit opt-in. (Implementation check: confirm no existing VPS env override masks the new
  default — earlier grep showed none set, so the default currently governs.)

## Open decisions for review
1. **Sink:** → **`SimulatedBrokerAdapter`** (recommended above).
2. **Phase-1 mechanism:** → **default-flip** (recommended above).
3. **Phase-2 timing:** SETTLED — both broad routing AND broad hash-guard wait for P0 (each removes the
   retired bots' refresh trigger). P1-Phase-1 (v2-scoped) lands first.
4. **Go-live ceremony:** confirm `paper:`→`live:` rename + provider→schwab + add hash, attended, is
   the canonical real-money path.

---

## Constraints
PR #227 stays · #238 untouched · streamer flag ON · retired bots dormant **AND not removed**
(load-bearing for token refresh until P0) · CYN untouched · polygon parked · v2 PAPER — **made
deliberately/structurally so by this change.** No code written; design-first for review.
