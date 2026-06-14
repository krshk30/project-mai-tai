# Track 2 — v2 ↔ OMS exit management — DESIGN (Approach B)

**Status:** design for review. **No integration code yet.** Ships **dormant behind a flag** (like Track 1).
**Decision locked (operator, Step B):** Approach **B** — OMS-side exits, via a **neutral shared ExitEngine
library** both `strategy_core` and the OMS import. Rationale (confirmed against code): the factoring is
leaf-level/clean; v2 stays **entry-only** (strongest isolation — only Approach A re-merged v2 into the
engine); **one** sell-emitting code path (the OMS) vs Approach C's two; the OMS already imports
`strategy_core.time_utils` and already maintains a per-symbol quote cache.

**What this makes real:** the week's re-score proved the **exit ladder IS the edge** (MACD-Cross entries:
"breakeven" under a +10% lens → ≈ +2.5%/trade under the real ladder). Today a v2 paper position, once
opened, has **nothing to close it** — verified live: `paper:schwab_1m_v2` holds CAST 10 + VSME 10 from
Friday with **zero** OMS-side management (native_stop_guard all-time count for v2 = 0; §Q4 probe). This
design gives v2 the validated ladder without touching v2's process isolation.

All file:line refs below were verified against the working tree during the Step-A/Step-B probes.

---

## 0. Scope & non-goals

- **In:** the hard-stop + breakeven-floor + scale-out legs of the ladder, OMS-side, DB-backed, for
  `paper:schwab_1m_v2` positions, behind a default-OFF flag.
- **Fast-follow (deferred, per Q3):** the **tier MACD/stoch exits** (`check_exit`) — they need an
  indicators dict the OMS doesn't compute. Shipped after, not in v1.
- **Out:** any change to the momentum bots' exit behavior (hard constraint — §1); v2 emitting sells
  (B keeps v2 entry-only); real-account routing (v2 stays paper); tick-precision exits (Track 3).
- **Cadence (Q3, accepted):** quote-driven legs to start. Honest caveat: a −1.5% hard stop evaluated
  per-quote (not per-tick) can slip past −1.5% intrabar on these pennies — the known degradation vs the
  idealized backtest that Track-3 ticks later close. Acceptable to start; not equivalent.

---

## 1. Shared-library extraction + existing-bots-unchanged proof (the hard constraint)

**What moves.** The exit ladder is two pure things plus one stateful collection:
- `strategy_core/exit.py::ExitEngine` — imports only `logging` + `TradingConfig`; operates on a
  duck-typed `position` + an `indicators` dict. Methods: `check_hard_stop(position, price)` (`exit.py:105`),
  `check_intrabar_exit(position)` → floor+scale (`exit.py:14`), `check_exit(position, indicators)` → tier
  (`exit.py:43`).
- The **per-position math** on `position_tracker.py::Position` — `update_price` (peak/profit), the
  floor ratchet (`_calculate_floor_pct`), tier upgrades, `get_scale_action`/`is_floor_breached`. Pure
  functions of (entry, price, peak, tier, scales_done, TradingConfig).
- `PositionTracker` (the **collection manager**) — file-persists to CSV/JSON. This is the part that does
  **not** move (it's the momentum bots' state home; see §2 for why the OMS uses the DB instead).

**The extraction.** Create a neutral leaf package `src/project_mai_tai/exit_logic/` containing:
- `exit_logic/engine.py` ← the verbatim `ExitEngine`.
- `exit_logic/position.py` ← a pure `Position` value object (the peak/tier/floor/scale math), with **no
  file I/O** (the CSV/JSON persistence stays behind in `PositionTracker`).
- `exit_logic/config.py` ← re-home or re-export `TradingConfig` (already a pure dataclass).

Verified clean: the cluster `{exit, position_tracker, trading_config, time_utils}` imports only stdlib +
each other — **nothing** from `strategy_engine_app`, `schwab_streamer`, `bar_builder`, `indicators`,
`polygon_30s`, the DB, or the gateway. The OMS already imports `strategy_core.time_utils`
(`oms/service.py:41`), so the OMS→exit_logic edge is incremental, not new.

**`strategy_core` keeps working unchanged:** `strategy_core/exit.py` and `position_tracker.py` become thin
re-exports / thin wrappers that import from `exit_logic` — so `StrategyBotRuntime` (`strategy_engine_app.py:290-291`)
constructs the **same** ExitEngine + PositionTracker, running **byte-identical** logic. This is a **pure
relocation, no logic change**.

**Proof obligation (the hard constraint — momentum bots provably unchanged).** Follow the endorsed
behavior-identical-refactor + survival methodology:
1. **Characterization green on unmodified code:** run the full momentum-bot exit test suite
   (`test_strategy_core.py`, the exit/position tests) on `origin/main` — capture the baseline.
2. **Pure move:** relocate the code with zero edits to the logic (only import paths change).
3. **Re-prove identical:** the same suite green; a **by-name regression diff vs `origin/main`** on the
   exit-decision outputs (feed identical positions/indicators through old vs new, assert identical
   signals); confirm `make_1m_variant`/`make_30s_variant` configs unchanged.
4. The momentum bots import the relocated logic via the `strategy_core` re-export → no behavior delta by
   construction. Any diff = a failed extraction, caught before the OMS work starts.

---

## 2. Single authoritative position owner = the OMS DB

**One owner, co-located with the exits (Q2).** For v2 positions, the OMS is the authoritative owner, and
the state lives in the **OMS DB** — not a file-persisted `PositionTracker` (which would be a parallel
store = the split-brain hazard Q2 names; the OMS's truth is already the DB).

**New table `oms_managed_positions`** (or extend `virtual_positions` with ladder columns) — OMS-owned,
one row per open managed position:

| column | source |
|---|---|
| `strategy_code`, `broker_account_name`, `symbol` | the fill |
| `entry_price`, `original_quantity`, `current_quantity` | open fill; decremented by scale fills |
| `peak_profit_pct`, `tier`, `floor_pct` | mutated each quote by `Position.update_price` |
| `scales_done` (jsonb list), `scale_pnl` | mutated as scale legs fire |
| `entry_path`, `entry_time`, `status` | the fill / lifecycle |

The OMS hydrates a pure `exit_logic.Position` from this row on each evaluation, runs the engine, and
writes back the mutated state in the same transaction as any emitted exit order — so state and the order
that changed it commit atomically (no torn state on restart). **Momentum bots are untouched** — they keep
their engine-side file-backed `PositionTracker`. Only v2 uses the OMS-DB owner.

---

## 3. The v2 fill → ladder feed (the binding v2 lacks today)

The missing link in §3 of the scoping doc: nothing feeds v2's fills to a position model. In Approach B
this lands **inside the OMS, which already sees the fill** (it executed it) — no new `order-events`
consumer needed.

- **Open:** when a fill report for `account=paper:schwab_1m_v2, intent_type=open, side=buy` is processed
  (the OMS's existing order-event path, near `oms/service.py` fill handling), **and the flag is on**, the
  OMS inserts an `oms_managed_positions` row (entry = fill price, qty = filled qty, path from metadata).
- **Evaluate (quote-driven legs):** the OMS already has `_handle_quote_tick_event` + `_latest_quotes_by_symbol`
  (`oms/service.py:1328`). Extend it: for each quote on a symbol with an open v2 managed row, hydrate the
  `Position`, `update_price(price)`, then `check_hard_stop` + `check_intrabar_exit`. If a signal fires, the
  OMS **builds and submits the close/scale order itself** (it already constructs broker orders), routed by
  account → the simulated adapter (§5), and writes back the new ladder state.
- **Scale / close fills** decrement `current_quantity` / close the row (reusing the OMS's existing
  fill-application path).
- **Re-entry coherence:** v2's `_position_poll_loop` (`schwab_1m_v2_bot.py:~671`) reads `virtual_positions`
  to gate re-entry. The OMS exit fills must keep `virtual_positions` in sync (they already do for the
  momentum bots) so v2 sees a position close and re-arms its cooldown — no double-open.

**Tier exits (fast-follow):** `check_exit` needs `{stoch_k, macd_cross_below, ema9, ...}`. The OMS doesn't
compute indicators. Deferred options (decide at fast-follow time): (a) v2 publishes its already-computed
indicators (it computes them for entries) on a side stream the OMS reads; (b) the OMS computes them from
bars. v1 ships the hard-stop/floor/scale risk core; tier exits follow.

---

## 4. Quote-coverage resolution (the §Q4-followup probe result)

**Probe finding:** the OMS's gateway-sourced quote cache covers v2's symbols **in practice but not by
guarantee.** Friday: v2 traded 17 symbols, **all 17** in the momentum-bot (gateway-driving) universe —
**zero gap.** But the mechanism allows divergence: v2's watchlist comes from the snapshot's
`top_confirmed`/`all_confirmed`/`watchlist` (`schwab_1m_v2_bot.py:864-882`), while the gateway subscribes to
the momentum bots' **retained** `active_symbols` (`strategy_engine_app.py:market_data_symbols()` 5044-5052,
which **excludes v2** — v2 isn't in `self.bots`). Both derive from the same scanner pool, so they converge;
but a symbol v2 confirms-and-trades that no momentum bot retained would leave the OMS **price-blind** for
that position — unacceptable on a risk path.

**Resolution (reuses existing machinery, keeps v2 entry-only):** v2 **registers its active symbols as a
gateway subscription consumer.** The gateway already unions desired symbols across consumers
(`market_data_subscriptions` is keyed `service_name + symbol`; `gateway.py` `_desired_symbols_by_consumer`
→ union). v2 publishes a subscription event with `service_name="schwab-1m-v2"` and its watchlist (an `xadd`
analogous to its existing heartbeat / `strategy-state-isolated` publishes — it declares **interest**, it
does **not** emit market data, so the entry-only boundary holds). The gateway then streams v2's symbols →
the OMS cache (already consuming gateway quotes) covers them **with a guarantee**. No duplicate feed.
- *Rejected alternative:* bridge v2's own quotes to the OMS — duplicates the feed and has v2 publishing
  market data.
- **Safety backstop (mandatory):** if the OMS ever holds a v2 managed position with **no fresh quote**
  (coverage miss or stale feed), it must surface it **loudly** (heartbeat field + WARN) and treat the
  position as degraded-but-visible — **never** silently leave a risk position price-blind.

---

## 5. Paper-isolation proof, extended to OMS-placed v2 exit orders

The exit path emits **sells** — the §6-Q6 concern. Why it's safe by construction, and how we prove it:

- **Routing is by account, not emitter.** The OMS resolves the provider from the **account**:
  `provider_for_account("paper:schwab_1m_v2") → "simulated"` (P1 Phase 1 / #276: `broker_provider`
  defaults `simulated` + `configured_schwab_accounts` refuses `paper:schwab_1m_v2`). The OMS-placed exit
  orders carry `account=paper:schwab_1m_v2` → routed to the **simulated** adapter by the **same**
  resolution that governs v2's entry side. An exit sell **cannot** reach a real account.
- **B shrinks the surface:** because v2 stays entry-only, the OMS is the **single** sell-emitting code
  path for v2 — the same place the entry isolation already lives (vs Approach C, which would add a second
  sell emitter in the v2 process).
- **Proof obligations (tests + survival):**
  1. Assert `provider_for_account("paper:schwab_1m_v2") == "simulated"` (regression guard).
  2. Unit: an OMS-emitted v2 close/scale order resolves to the `SimulatedBrokerAdapter`, never `schwab`.
  3. **Survival test:** with the flag on, drive a v2 open fill → quote that breaches the floor/stop →
     assert the emitted exit **fills on the simulated adapter** and **no** schwab order is constructed;
     fault-inject a misconfig (e.g. account override) and assert the `configured_schwab_accounts` refusal
     still blocks it. (Mirrors the P1 Phase-1 survival discipline.)
  4. Re-affirm the OMS adapter remains a pure token reader (refresher is sole token owner) — the exit path
     adds no new token consumer.

---

## 6. Dormant flag + rollout (like Track 1)

- New flag `strategy_oms_v2_exit_management_enabled` (default **False**). **OFF = identical to today:** the
  OMS does **not** create `oms_managed_positions` rows for v2 fills, runs no eval loop, emits no v2 exits
  → v2 positions stay unmanaged exactly as now. The shared-lib extraction (§1) is behavior-neutral and can
  land independently (it changes only import paths).
- **Phasing:** (1) ship the shared-lib extraction, prove momentum bots unchanged, deploy dormant
  (no flag needed — neutral). (2) ship the OMS v2-exit machinery behind the OFF flag + the gateway-consumer
  bridge, deploy dormant. (3) attended flip on a flat/low-risk window → watch one v2 position get managed
  end-to-end (open → floor/scale/stop → close) on the simulated adapter. (4) tier-exit fast-follow.
- **Rollback:** flag → false + restart oms (and stop v2's subscription-consumer publish). No schema
  rollback needed (the table is inert when the flag is off).

---

## 7. Open questions for review (before integration code)

1. **Table choice:** new `oms_managed_positions` vs extending `virtual_positions` with ladder columns?
   (Lean: new table — keeps `virtual_positions` semantics clean and the ladder state OMS-owned.)
2. **Eval cadence:** evaluate on every quote tick, or throttle (e.g. ≥250ms/symbol) to bound OMS load
   when many v2 positions are open? (Quote-drift cancel already runs per tick, so per-tick is precedented.)
3. **Config:** a dedicated v2 `TradingConfig` variant vs reuse `make_1m_variant()` (Q5). (Lean: start with
   `make_1m_variant()` — same 1-minute cadence as the re-score modeled.)
4. **Tier-exit feed:** which fast-follow path (v2 publishes indicators vs OMS computes) — decide now or at
   fast-follow? (Can defer; v1 is risk-legs only.)
5. **Scale partials & the simulated adapter:** confirm the sim adapter fills partial sells (scale-outs)
   the way the re-score modeled (50%/25% legs) — needs a behavior check before the survival test.

---

## 8. Honest boundaries

- Design only; no integration code. The shared-lib extraction is a pure move whose **only** job is to be
  provably behavior-neutral for the momentum bots — that proof gates everything else.
- v1 is the **risk core** (hard-stop/floor/scale) on quote cadence; tier MACD/stoch exits and tick
  precision are explicit fast-follows, not in v1. The idealized re-score assumed both — so v1's live
  numbers will trail the backtest until those land. Stated, not hidden.
- "Per-quote" ≠ "per-tick": intrabar hard-stop slippage is real on these pennies (Track 3 closes it).
- This manages **paper** positions only; real-account routing stays a separate, later, gated step.
