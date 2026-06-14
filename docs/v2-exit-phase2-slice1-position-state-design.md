# Track 2 Phase 2 — Slice 1: v2 position-state plumbing — DESIGN

**Status:** design for review. **No code yet.** First of 4 Phase-2 slices, each design→build→review,
each provable in isolation, all behind one default-OFF flag (dormant).
**Builds on:** the production-verified shared lib `project_mai_tai/exit_logic/` (Phase 1, deployed
`380b465`). **Overall Phase-2 design:** `docs/v2-oms-exit-management-design.md` (#299, Approach B).

**Slice 1 scope (only):** the OMS becomes the **single writer** of a new `oms_managed_positions` table,
creating/updating an `exit_logic.Position` from v2's own fills. **No exits, no exit orders, no emit** —
just authoritative position state for v2, so later slices have something to evaluate. Isolation-safe:
all new code + a new table; touches nothing that already works; dormant behind the flag.

---

## 0. The §7 settlements this slice assumes (operator to confirm)

Locked already: per-quote risk-leg cadence; new `oms_managed_positions` table; sole-writer discipline.
Slice 1 additionally depends on **§7-a (exit-config variant)** — recommendation below; the other two
(§7-b tier feed, §7-c sim partials) are slice-3/4 concerns and don't affect slice 1.

- **§7-a — dedicated v2 `TradingConfig` variant (recommended), NOT `make_1m_variant`.** Slice 1 hydrates
  a `Position` whose floor params come from a config; that config must reproduce the **re-score's
  validated ladder**. Fact: base `TradingConfig` = **1.5%** hard stop + base scale/floor (`exit_logic/
  config.py:15`) — which **is** exactly the re-score ladder (stop 1.5%; scale +2%→50% / +4%→25% / fast
  +4%→75%; floor 1%→BE / 2%→0.5 / 3%→1.5 / 4%→trail−1.5). But `make_1m_variant` overrides
  `stop_loss_pct=1.0` (`config.py:308`) — it **diverges** from the validated 1.5%. So reuse of
  `make_1m_variant` would silently run v2 exits at a tighter stop than the backtest that justified them.
  → A **dedicated `make_v2_variant()`** (base defaults: 1.5% stop, base scale/floor; `quantity=10`,
  `bar_interval_secs=60`) matches the re-score by construction, keeps v2's exit tuning **isolated** from
  the momentum bots (changing one never affects the other — v2's whole philosophy), and is independently
  tunable. If the operator prefers `make_1m_variant`, the only material delta is stop 1.0% vs 1.5% — call it.

---

## 1. The table — `oms_managed_positions` (OMS-owned)

One row per open v2-managed position. Mirrors `exit_logic.Position`'s state so a `Position` can be
hydrated/persisted each evaluation.

| column | type | source |
|---|---|---|
| `id` | uuid pk | gen |
| `strategy_code` | text | fill (`schwab_1m_v2`) |
| `broker_account_name` | text | fill (`paper:schwab_1m_v2`) |
| `symbol` | text | fill |
| `entry_price` | numeric | open fill price |
| `original_quantity` | int | open fill qty |
| `current_quantity` | int | decremented by scale/close fills (slice 3) |
| `entry_path` | text | fill metadata (`MACD Cross`/`VWAP Breakout`/`ATR Flip`) |
| `entry_time` | timestamptz | open fill time |
| `peak_profit_pct` | numeric | `Position.update_price` |
| `current_profit_pct` | numeric | `Position.update_price` |
| `tier` | int | `Position` (1/2/3) |
| `floor_pct` | numeric | `Position` ratchet |
| `floor_price` | numeric | `Position` ratchet |
| `scales_done` | jsonb (list) | slice 3 |
| `scale_pnl` | numeric | slice 3 |
| `config_name` | text | which variant hydrated it (`make_v2_variant`) — auditability |
| `status` | text | `open` / `closed` |
| `created_at` / `updated_at` | timestamptz | |

- **Unique:** `(broker_account_name, symbol)` where `status='open'` (one open managed position per symbol).
- Migration: a new Alembic revision chaining off head (additive; empty table; **inert when the flag is
  OFF** — no rows written). No change to `virtual_positions` or any existing table.

---

## 2. The fill → Position binding (slice 1's one behavior)

The OMS already consumes its own `order-events` and applies fills (this is the binding v2 lacks). When a
fill report is processed for **`strategy_code='schwab_1m_v2'` AND `intent_type='open'` AND `side='buy'`
AND filled** — **and the flag is ON** — the OMS:
1. hydrates an `exit_logic.Position(ticker=symbol, entry_price=fill_price, quantity=filled_qty,
   entry_time=..., path=..., scale_profile='NORMAL', **<floor params from make_v2_variant>)`;
2. inserts an `oms_managed_positions` row from `Position`'s state (`config_name='make_v2_variant'`).

**State updates (no emit):** when a quote tick arrives for a symbol with an open managed row (the OMS
already maintains `_latest_quotes_by_symbol`), the OMS hydrates the `Position` from the row,
`update_price(quote_price)`, and writes back `peak/tier/floor/current_profit_pct`. **Nothing is sold —
no `check_*`, no exit order.** This proves the state tracks correctly (peak ratchets, tier upgrades,
floor locks) while emitting nothing — the foundation slice 3 evaluates on.
*(Quote COVERAGE for v2's symbols is slice 2's job; in slice 1 the update simply runs whenever a quote
for that symbol happens to be in the cache — correctness of the state math is what slice 1 proves, not
coverage.)*

**Lifecycle / close:** if the OMS processes a `sell`/`close` fill that flattens the position (external
flatten, or slice-3 exits later), it sets the row `status='closed'`, `current_quantity=0`. Slice 1
itself emits no sells, so rows persist `open` until an external close — that's expected (v2 has no exits
until slice 3).

---

## 3. Sole-writer discipline (the locked rule)

**The OMS is the ONLY writer of `oms_managed_positions`.** No other service/process inserts or mutates
it. This avoids the dual-writer margin-race. Concretely:
- All writes happen inside the OMS's order-event / quote-tick handlers, **in the same DB transaction**
  as the triggering event, so state and the event that changed it commit atomically (no torn state on
  restart).
- `virtual_positions` remains the **separate** qty-truth that v2's re-entry poll reads — kept synced by
  the **existing** fill path (unchanged). `oms_managed_positions` is **additive ladder state**, not a
  competing qty source. Two rows, two concerns, **one writer (the OMS) for the managed row**; the
  existing `virtual_positions` writer is untouched.

---

## 4. Flag (dormant)

`strategy_oms_v2_exit_management_enabled` (default **False**) — the **single Phase-2 flag** across all
slices. OFF (default): the OMS does **not** create/update managed rows, behaves exactly as today. The
migration ships dormant (empty inert table). Slice 1 is fully behind it.

---

## 5. Tests (provable in isolation — slice 1)

- **Fill creates a correct row:** simulate a v2 open fill (account/strategy/qty/price) → assert an
  `oms_managed_positions` row with `entry_price`/`original_quantity`/`entry_path` from the fill and
  **floor params derived from `make_v2_variant`** (1.5%-stop family) — i.e. `Position` hydrated with the
  right config. Uses the in-memory/SQLite OMS store pattern (like `test_oms_store.py`).
- **Quote updates state, emits nothing:** feed a price path → assert `peak_profit_pct`/`tier`/`floor_pct`
  ratchet exactly per the `exit_logic.Position` golden behavior **and zero orders/intents are produced**.
- **Dormant when flag OFF:** same fill with flag OFF → **no row created**, OMS behavior byte-identical to
  today.
- **Sole-writer / atomicity:** the managed row is written only in the OMS handler; a restart mid-sequence
  leaves consistent state (row matches the last applied fill).
- **`make_v2_variant` parity:** assert `make_v2_variant()` reproduces the re-score ladder values
  (stop 1.5%, base scale/floor) — guards against a config drift from the validated backtest.
- **Momentum bots untouched:** existing OMS tests unchanged (this adds a gated branch, no change to the
  existing fill/quote paths when the flag is OFF).

---

## 6. Explicitly NOT in slice 1

- **No exits** — no `ExitEngine.check_*`, no close/scale orders, no sells. (Slice 3.)
- **No quote-consumer bridge** — slice 1 doesn't guarantee v2's symbols are in the OMS quote cache.
  (Slice 2.) Slice 1 proves the state MATH; coverage is slice 2.
- **No paper-isolation re-proof** — no sells emitted, so nothing to re-prove yet. (Slice 3 gates on it.)
- **No tier exits / indicator feed.** (Slice 4 / §7-b.)
- **No `make_v2_variant` tuning** beyond reproducing the re-score defaults — tuning is a later operator call.

---

## 7. Honest boundaries

- Slice 1 delivers **state, not behavior** — v2 positions still don't get exited by anything until slice
  3. Its value is an isolated, provable foundation (single-writer DB-backed `Position`) that the risk
  legs evaluate on, with zero risk to anything live (gated OFF, additive table).
- The `make_v2_variant` = re-score defaults choice is what makes slice-3 live numbers comparable to the
  backtest; reusing `make_1m_variant` (1.0% stop) would silently diverge — hence §7-a.
- Deploy (when slices are ready) is an **OMS** change → attended/after-close; but slice 1 alone is inert
  (flag OFF, no sells), so its risk is a migration + a gated code path, not exit behavior.
