# Track 2 — v2 ↔ OMS exits — STEP A SCOPING (read-only)

**Status:** read-only scoping. **No design, no recommendation, no code.** This document maps how
the momentum bots run the exit ladder today and itemizes exactly what `schwab_1m_v2` lacks, so the
integration can be **sized** before an approach is chosen. **The wiring decision is the operator's
next step** (Step B) — the candidate approaches in §5 are laid out neutrally with their integration
surface + tradeoffs, deliberately without a pick.

**Why this matters:** the week's re-score proved the **exit ladder IS the edge** (MACD-Cross entries
go from "breakeven" under a +10% scalp lens to ≈ +2.5%/trade under the real ladder). v2 emits entries
only and runs **no** managed exits — so a v2 paper position, once opened, has nothing to close it.
This is the TOP open item; it's what makes ANY v2 entry (Paths 1/2 **and** the new ATR-Flip path) real.
All file:line references below were spot-verified against the working tree.

---

## 1. How a momentum bot runs the exit ladder today (the wiring chain)

The exit ladder is **strategy-engine-side**, owned per-bot by a `StrategyBotRuntime` inside the
single `strategy_engine_app.py` process. The chain, end to end:

```
order-events (fill) ──▶ apply_execution_fill ──▶ PositionTracker.open_position
                                                         │
   trade tick / quote ──▶ _evaluate_position_price_intents ──▶ ExitEngine.check_hard_stop
                                                         │            ExitEngine.check_intrabar_exit (floor + scales)
   bar close          ──▶ _evaluate_position_at_bar_completion ──▶ ExitEngine.check_exit (tier MACD/stoch)
                                                         │
                                          signal dict ──▶ _emit_close_intent / _emit_scale_intent
                                                         │
                                          intent_type close|scale ──▶ Redis strategy-intents ──▶ OMS ──▶ broker
```

**Component inventory (verified):**

| Piece | File:line | Role |
|---|---|---|
| `ExitEngine.__init__(config: TradingConfig)` | `strategy_core/exit.py:11` | stateless evaluator; config-only |
| `ExitEngine.check_intrabar_exit(position)` | `exit.py:14` | floor breach + scale points (per-tick) |
| `ExitEngine.check_exit(position, indicators)` | `exit.py:43` | tier-gated MACD/stoch exits (per-bar) |
| `ExitEngine.check_hard_stop(position, price)` | `exit.py:105` | −1.5% fixed hard stop (per-tick) |
| `PositionTracker.open_position(...)` | `position_tracker.py:253` | creates a `Position` on a buy fill |
| `PositionTracker.close_position(...)` | `position_tracker.py:279` | books P&L + streaks on a close/flat |
| `PositionTracker.update_all_prices(map)` | `position_tracker.py:332` | feeds fresh prices → recomputes peak/floor/tier |
| `Position` per-position state | `position_tracker.py:20` | entry/peak/tier/floor/`scales_done`/qty |
| `ExitEngine` + `PositionTracker` instantiation | `strategy_engine_app.py:290-291` | **one pair per `StrategyBotRuntime`** |
| Per-tick exit loop | `strategy_engine_app.py:~669-719` | `update_price` → `check_hard_stop` → `check_intrabar_exit` |
| Per-bar exit loop | `strategy_engine_app.py:~1851-1903` | `check_exit` (tier MACD/stoch) |
| Close/scale intent emit | `strategy_engine_app.py:~2569 / ~2634` | builds `intent_type=close|scale`, side=sell |
| Fill → tracker binding | `strategy_engine_app.py:1569` (`apply_execution_fill`) | open buy → `open_position`; sell → qty/close |
| Order-events consumption | `strategy_engine_app.py:~6580-6613` | reads `order-events`, routes fills by strategy_code |

**The exit-ladder parameters** live in `strategy_core/trading_config.py` (`TradingConfig`): hard-stop
(`stop_loss_pct`, ~line 14), floor ratchet thresholds (~20-23), scale tiers (`scale_*_pct` /
`scale_*_sell_pct`, ~255-264), exit stoch filters (~168-170). Config is **per-bot** — each
`StrategyBotRuntime` is built with its own `definition.trading_config` (`make_1m_variant()` for
schwab_1m, `make_30s_variant()` for the 30s bots). So a v2 variant could have its own exit config.

**Which bots get this:** `StrategyBotRuntime` (and therefore the ExitEngine + PositionTracker pair)
is built only for the strategies registered in `strategy_engine_app.py`:
`macd_1m` (3901), `schwab_1m` (3918), `tos` (3946), `macd_30s` (3973), `polygon_30s` (4025),
`macd_30s_probe` (4056), `macd_30s_reclaim` (4075), `macd_30s_retest` (4094). **`schwab_1m_v2` is not
in this list** — verified. It runs as a **separate process** (`services/schwab_1m_v2_bot.py`),
structurally outside the exit infrastructure.

---

## 2. The OMS-side stop (the one exit-ish thing that does exist)

There is a **second, much dumber** stop path on the OMS side, independent of the ExitEngine:
`oms/service.py` `_arm_or_rearm_native_stop_guard(...)` (~739-800) posts a **sell limit order** at
`entry_price * (1 − stop_loss_pct/100)` tagged `native_stop_guard="true"`, armed at open-fill time;
a strategy close cancels it first (~614-661). It is a **static price-level failsafe** — no peak,
no tier, no scale-outs, no floor ratchet. **Whether it currently arms for v2's paper fills at all is
an open question (§6, Q4)** — v2 routes to the simulated provider, and the guard lives in the OMS
order path; this needs confirming, not assuming.

So the real choice is not "v2 has nothing" vs "full ladder" — it's: **what manages a v2 position
between the static OMS failsafe (if it even fires for v2) and the full peak-aware ExitEngine ladder.**

---

## 3. What v2 lacks (the gap inventory)

`services/schwab_1m_v2_bot.py` + `strategy_core/schwab_1m_v2.py` confirm v2:

1. **No `ExitEngine`** — never imports it; never calls `check_exit` / `check_intrabar_exit` /
   `check_hard_stop`. `on_bar` evaluates **entry only** and returns open intents (now also ATR-Flip).
2. **No `PositionTracker`** — no `Position` with entry/peak/tier/floor/`scales_done`. v2's
   `_position_poll_loop` (`schwab_1m_v2_bot.py:~671-700`) polls the DB every 5s **only for a
   flat/has-position qty gate** to block re-entry — it does not track entry price, peak, or scale
   progress. This is a re-entry guard, not a position model.
3. **No fill→tracker binding** — v2 has **no consumer of the `order-events` stream**. Even if it had
   a PositionTracker, nothing would populate it with fills. (The strategy-engine's order-events loop
   only routes to its registered bots; v2's code isn't among them.)
4. **No exit config** — v2 has no `TradingConfig`; none of the scale/floor/hard-stop/tier params
   exist in its settings.
5. **No per-bar / per-tick exit evaluation** — no `_evaluate_position_at_bar_completion` or
   `_evaluate_position_price_intents` equivalent; v2's bar handler emits opens and persists bars.
6. **Not registered with the engine** — absent from the `StrategyBotRuntime` registration block
   (§1), so it gets none of the above by construction.

**Net:** every link in the §1 chain is missing for v2 — the position model, the fill binding, the
evaluation loops, the config, and the registration. The ladder is not "disabled" for v2; it was
never wired.

---

## 4. Sizing axes (what the Step-B decision must weigh)

Independent of approach, the integration must answer:

- **Where does the position state live?** A new PositionTracker for v2 (in the v2 process, or in the
  engine) vs reuse the DB `virtual_position` rows v2 already polls vs OMS-side state.
- **What feeds it fills?** v2 needs to learn its own fills (open price, scale fills, closes). Today
  nothing does. Source = the `order-events` stream (a new consumer) or the OMS directly.
- **What drives evaluation cadence?** The ladder needs per-tick (hard stop + floor + scale) **and**
  per-bar (tier MACD/stoch) evaluation against live prices. v2 has a bar poll + quote poll already;
  per-tick precision is weaker without the Track-3 tick feed.
- **Who emits close/scale intents, and as what?** The OMS already understands `intent_type=close|scale`,
  side=sell (that's how the momentum bots exit). v2 would emit those — but v2's `SchwabV2Strategy`
  comment explicitly says it *never* emits close/scale today, so that's a deliberate boundary to cross.
- **Config ownership.** A v2 `TradingConfig` variant (its own scale/floor/stop) vs borrowing
  `make_1m_variant()`.
- **Live-money isolation must be preserved.** v2 is structurally paper (P1 Phase 1: simulated
  provider + account-hash refusal). Any exit wiring must NOT become a backdoor that routes v2 sells
  to a real account. The exit path emits sells — this is exactly where a routing mistake would bite.

---

## 5. Candidate wiring approaches (neutral — for the operator to choose in Step B)

The build plan named three. Sizing each against §3/§4, **without recommending**:

### Approach A — Register v2 with the existing engine-side ExitEngine
Give `schwab_1m_v2` a `StrategyBotRuntime` (or an equivalent) inside `strategy_engine_app.py`: its own
ExitEngine + PositionTracker + TradingConfig, wire its `order-events` fills into `apply_execution_fill`,
and run the per-tick + per-bar exit loops for it.
- **Reuses:** the entire validated ladder verbatim (the proven peak/tier/floor/scale logic) and the
  re-score's modeled behavior — highest fidelity to the backtest.
- **Costs / risks:** v2 is a *separate process* by design (isolation was deliberate — its own token
  path, REST client, deploy lifecycle). Pulling its position management into the engine partially
  dissolves that isolation, and couples v2's exits to the engine's health (the SPOF history makes
  shared-fate a real concern). Largest surface in `strategy_engine_app.py` (the 14k-line file).
- **Open sub-question:** engine drives exits while the *v2 process* drives entries → a split-brain on
  position state unless one side is authoritative.

### Approach B — OMS-side exit management for v2 positions
Keep v2 isolated; have the OMS manage v2 exits beyond the static `native_stop_guard` — i.e. teach the
OMS (or an OMS-adjacent component) to run scale/floor/tier logic for v2's positions.
- **Reuses:** v2's isolation stays intact; the OMS already sees v2's fills (it executes them) and
  already has the native-stop scaffolding + the cancel-before-close handshake.
- **Costs / risks:** the peak-aware ladder logic currently lives in `strategy_core` (ExitEngine /
  PositionTracker), **not** the OMS — so this means either porting/duplicating that logic OMS-side
  (drift risk vs the validated ExitEngine) or factoring ExitEngine into a shared library. The OMS
  becoming price-aware/stateful per position is a meaningful expansion of its role.

### Approach C — v2 emits its own scale/close intents
v2 gains a PositionTracker + ExitEngine **inside its own process** and emits `intent_type=close|scale`
sells itself (the OMS already consumes those).
- **Reuses:** v2's process isolation; the OMS sell path is unchanged (it already routes close/scale
  for the momentum bots); can import `ExitEngine`/`PositionTracker` as libraries.
- **Costs / risks:** v2 must consume its **own** `order-events` (a new loop it doesn't have) to feed
  the tracker, and per-tick exit precision depends on v2's quote cadence (or the Track-3 ticks).
  Directly contradicts the current `schwab_1m_v2.py` boundary ("we never emit close/scale/cancel") —
  a deliberate but real change. **Crosses the live-money line most directly** (v2 would emit sells),
  so the paper-isolation guard (§4) must be re-proven for the sell path.

*(These are not exhaustive and not mutually exclusive — e.g. a shared ExitEngine library consumed by
either the engine, the OMS, or the v2 process is a cross-cutting option. Left open for Step B.)*

---

## 6. Open questions for the operator (Step-B inputs)

1. **Isolation vs fidelity** — is preserving v2's process isolation (its own token path / deploy
   lifecycle, the SPOF lesson) a hard constraint, or acceptable to relax for ladder fidelity?
2. **Position-state authority** — one authoritative position model, and where (v2 process / engine /
   OMS)? Split entry-here/exit-there is the main correctness hazard.
3. **Tick precision** — is per-bar / per-quote exit cadence acceptable for v2 initially, or is the
   Track-3 tick feed a prerequisite for the hard-stop/floor precision?
4. **Does the OMS `native_stop_guard` currently arm for v2's simulated fills?** (Determines the
   real starting point — "static stop already exists" vs "literally nothing.") Needs a direct probe,
   not an assumption.
5. **Exit config** — a dedicated v2 `TradingConfig` variant, or reuse `make_1m_variant()`?
6. **Paper-isolation re-proof** — whichever approach emits v2 sells, how do we re-prove (test +
   survival) that they cannot reach a real account, given the exit path is sell-side?

---

## 7. Honest boundaries of this scoping

- Read-only; line numbers spot-verified for the load-bearing claims (ExitEngine/PositionTracker
  interfaces, the per-bot instantiation, v2's absence from the registration list). The deep
  `strategy_engine_app.py` loop line numbers (~669/~1851/~2569/~6580) are approximate within a
  14k-line file — exact ranges to be pinned during Step-B design, not relied on for the decision.
- No approach is recommended here by design. §5 sizes; §6 is the decision the operator owns.
- This does not estimate effort/time — it sizes the **integration surface**. Effort follows the
  chosen approach.
