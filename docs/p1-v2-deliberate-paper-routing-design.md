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

### Phase 1 (NOW — P1, safe before P0): make v2 deliberately paper, two structural layers
1. **Route v2 to simulated by changing the DEFAULT.** `strategy_schwab_1m_v2_broker_provider`
   default `"schwab"` → `"simulated"` (safe-by-default; not just an env override that can be lost).
   v2's orders → `SimulatedBrokerAdapter`. Real-Schwab becomes an explicit opt-in at go-live. This is
   **v2-scoped** → does **not** disturb the retired-bot token-refresh dependency (P0 intact).
2. **Hash-side guard (defense-in-depth):** make `configured_schwab_accounts` **refuse to register any
   `paper:`-prefixed account** (skip/raise). So even if v2's provider were later flipped back to
   `"schwab"`, a `paper:` account can never bind a real hash → reject. Turns today's *accidental*
   missing-entry safety net into an *intentional, enforced* one. Safe and independent of routing /
   refresher presence.

→ Net after Phase 1: v2 orders fill in sim (execution validated), and v2 **cannot reach the real
account even if the entry is added** (provider routes to sim; and the hash-guard refuses paper:
binds). Both are structural, not accidental.

### Phase 2 (WITH / AFTER P0): full `paper:`-prefix routing enforcement (the ultimate belt)
Once the **dedicated refresher (P0)** owns token freshness independent of any schwab-provider account,
add the structural rule: a `paper:`-prefixed account **cannot resolve to a real broker provider**
(force `simulated`, or raise). Safe to apply broadly only *after* P0, because it removes the incidental
refresher. This makes the `display_account_name` awareness into real enforcement fleet-wide.

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

## Open decisions for review
1. **Sink:** `SimulatedBrokerAdapter` (recommended — no creds, deterministic, validates fills) vs
   `AlpacaPaperBrokerAdapter` (real paper venue, needs creds).
2. **Phase-1 mechanism:** default-flip (recommended, safe-by-default) vs env-override-only.
3. **Phase-2 timing:** confirm full `paper:`→sim enforcement waits for P0 (it removes the incidental
   refresher). Agree P1-Phase-1 (v2-scoped + hash-guard) lands first/now, Phase-2 with-or-after P0.
4. **Go-live ceremony:** confirm `paper:`→`live:` rename + provider→schwab + add hash, attended, is
   the canonical real-money path.

---

## Constraints
PR #227 stays · #238 untouched · streamer flag ON · retired bots dormant **AND not removed**
(load-bearing for token refresh until P0) · CYN untouched · polygon parked · v2 PAPER — **made
deliberately/structurally so by this change.** No code written; design-first for review.
