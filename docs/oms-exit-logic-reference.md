# OMS / Exit-Ladder Logic — Reference (extraction only, no changes)

Faithful extraction of the system's **exit ladder** so the replay can model it exactly.
Source of truth (read, not memory): `strategy_core/exit.py` (`ExitEngine`),
`strategy_core/position_tracker.py` (`Position` — the ladder/floor/tier state),
`strategy_core/trading_config.py` (`TradingConfig` — the values),
`services/strategy_engine_app.py` (invocation/cadence/precedence + intent emission),
`oms/service.py` (execution + the broker-side native stop guard).

> **⚠️ SCOPE CAVEAT (critical for re-scoring).** This ladder runs in **strategy_engine_app.py for
> the momentum bots (`macd_30s`, `schwab_1m`, `polygon_30s`)**. **`schwab_1m_v2` does NOT run it** —
> the v2 bot service imports neither `ExitEngine` nor `PositionTracker`, and its open intents carry
> **no** exit/stop metadata (only `path/entry_price/reference_price/macd_*/...`). So for the
> apples-to-apples test, we model **"what the system's real exit ladder would do"** over the Path 1/2
> and Path 3 entries — NOT what v2 does today (today v2 emits opens only; exit management is
> aspirational). Document this in the re-score report.

---

## 1. Components & where each lives

| Concern | Implementation |
|---|---|
| Exit decisions | `ExitEngine` — `strategy_core/exit.py` |
| Ladder/floor/tier state per position | `Position` — `strategy_core/position_tracker.py` |
| All thresholds | `TradingConfig` — `strategy_core/trading_config.py` |
| Invocation (cadence/precedence) + intent emission | `StrategyEngineService` — `services/strategy_engine_app.py` |
| Order execution + broker-side native stop | `OmsService` — `oms/service.py` |

Per-position state (`Position.__init__` / `update_price`): `entry_price`, `quantity` (current,
decremented by scales), `original_quantity`, `current_price`, `current_profit_pct`,
`peak_profit_pct` (running max), `tier` (1→3, ratchets up only), `floor_pct`/`floor_price`
(ratchets up only), `scales_done[]`, `bars_since_entry`, `scale_profile` (NORMAL|DEGRADED).

---

## 2. Scale-out ladder  (`Position.get_scale_action`, values in `TradingConfig`)

Sell quantity = `int(CURRENT_qty × sell_pct/100)` — **the % is of the CURRENT (remaining) quantity
at that tier, not the original.** Each level fires once (tracked in `scales_done`).

### NORMAL profile (default — healthy symbols)
| Order | Level | Trigger (current profit) | Sell | Guard |
|---|---|---|---|---|
| 1a | `FAST4` | **≥ +4%** | **75%** | only if neither FAST4 nor PCT2 done (ripped to +4% before +2% triggered) |
| 1b | `PCT2` | **≥ +2%** | **50%** | only if neither PCT2 nor FAST4 done |
| 2 | `PCT4_AFTER2` | **≥ +4%** | **25%** | only if PCT2 already done (the +4% follow-on after the +2% scale) |

So a typical winner: +2% → sell 50%; then +4% → sell 25% of the remaining (≈12.5% of original);
≈37.5% rides the floor/trail. A fast runner that hits +4% first → sell 75%; 25% rides.

### DEGRADED profile (symbol flagged degraded; `degraded_enabled` + lifecycle `degraded_mode`)
| Order | Level | Trigger | Sell |
|---|---|---|---|
| 1 | `PCT1` | **≥ +1%** | **25%** |
| 2 | `PCT2` | **≥ +2%** | **25%** |
| 3 | `FAST4` | **≥ +4%** | **75%** |

Profile selection: `_scale_profile_for_symbol` — NORMAL unless `degraded_enabled` AND the symbol's
lifecycle state is `degraded_mode`. (`TradingConfig`: `scale_normal2_pct=2.0/sell 50`,
`scale_fast4_pct=4.0/sell 75`, `scale_4after2_pct=4.0/sell 25`, `scale_degraded1_pct=1.0/sell 25`,
`scale_degraded2_pct=2.0/sell 25`.)

---

## 3. Floor / breakeven ratchet  (`Position._calculate_floor_pct`, `is_floor_breached`)

Floor is set from the **peak** profit and **only ratchets UP** (`if new_floor_pct > floor_pct`).
A close fires (`FLOOR_BREACH`) when `current_price ≤ floor_price` (`floor_price = entry × (1 +
floor_pct/100)`).

| Peak profit reached | Floor locks at | Behaviour |
|---|---|---|
| ≥ +1% | **+0.0%** (breakeven) | once you've seen +1%, you can't lose (default; some presets +0.25%) |
| ≥ +2% | **+0.5%** | locks in +0.5% |
| ≥ +3% | **+1.5%** | locks in +1.5% |
| ≥ +4% | **peak − 1.5%** (TRAILING) | above +4% the floor trails 1.5% under the running peak |
| < +1% | none (−999) | no floor yet → only the hard stop governs |

`TradingConfig`: `profit_floor_lock_at_1pct_peak_pct=0.0`, `…2pct=0.5`, `…3pct=1.5`,
`profit_floor_trail_buffer_over_4pct_pct=1.5`.

---

## 4. Hard stop  (TWO layers, same level)

- **Strategy-side** (`ExitEngine.check_hard_stop`): `stop_price = entry × (1 − stop_loss_pct/100)`;
  fires `HARD_STOP` close when `current_price ≤ stop_price`. **Fixed %, does NOT move.**
  `TradingConfig.stop_loss_pct = 1.5` (default; some presets 1.0).
- **Broker-side native stop guard** (`oms/service.py _arm_or_rearm_native_stop_guard`): a **resting
  `order_type=STOP` sell order** at the same `stop_price`, qty = position qty, **session-bounded**
  (`ex = _seconds_until_session_end`). Cancelled before any sell (scale/close) then re-armed
  (`_cancel_native_stop_guard_before_sell` / `_rearm_native_stop_from_registry`). Redundant
  protection if the strategy-engine lags/dies. Config metadata: `stop_guard_enabled`,
  `stop_loss_pct`, `stop_guard_quote_max_age_ms=2000`, `stop_guard_initial_panic_buffer_pct=0.5`.

The hard stop is the LOOSEST protection early (−1.5%); once peak ≥ +1% the floor (breakeven+)
supersedes it as the effective stop.

---

## 5. Tier-based momentum exits  (bar-close only — `ExitEngine.check_exit`, needs indicators)

`tier` advances on **peak**: tier 1 at entry → tier 2 at peak ≥ +1% → tier 3 at peak ≥ +3%
(ratchets up only). The exit gets **stricter as the position runs** (let winners run):

| Tier | Exit conditions (any → CLOSE) |
|---|---|
| 1 | stoch exit (`STOCHK_TIER1`) **OR** `macd_cross_below` (`MACD_BEAR_T1`) |
| 2 | (stoch exit **AND** `not price_above_ema9`) (`STOCHK_TIER2`) **OR** `macd_cross_below` (`MACD_BEAR_T2`) |
| 3 | `macd_cross_below` only (`MACD_BEAR_T3`) |

Stoch exit (`_should_take_stoch_exit`): `stoch_k_below_exit AND stoch_k_falling`, AND — if
`exit_stoch_health_filter_enabled` (**default OFF**) — `not _stoch_momentum_healthy` (rising-two-bars
AND above-D AND slope ≥ `exit_stoch_min_slope=2.0` AND not rolling-over-from-overbought ≥
`exit_stoch_overbought_level=80`). With the filter off, any stoch_k_below_exit + falling triggers.

---

## 6. Time-based / EOD exits — NONE found

No automatic end-of-day flatten / max-hold / max-bars / time-stop exists in the exit ladder. The
only session-time coupling is the **native stop guard order's** session-bounded expiry
(`_seconds_until_session_end`) — that expires the resting broker STOP, it does not flatten the
position. **Implication for re-scoring: positions are held until a scale/floor/hard-stop/MACD/stoch
exit fires; do NOT assume an EOD flat unless modeling the broker/session explicitly.** (`_finalize_
flattened_position` is reconciliation of externally-flattened positions, not an exit rule.)

---

## 7. Precedence & cadence  (`strategy_engine_app.py`)

**On every QUOTE tick** (`_evaluate_position_quote_intents`, live, intrabar): `position.update_price`
→ **1) hard stop** (`check_hard_stop`; if fired, emit close + RETURN) → **2) intrabar**
(`check_intrabar_exit`): **floor breach → CLOSE**, else **scale → SCALE**.

**On BAR CLOSE** (`check_exit`): runs `check_intrabar_exit` first (floor/scale), THEN the tier MACD/
stoch exits.

**Net precedence on a bar where several could fire:** `HARD STOP > FLOOR BREACH > SCALE > tier
MACD/stoch`. Hard-stop/floor/scale evaluate on **quotes AND bar close**; tier MACD/stoch only on
**bar close** (indicator-dependent).

Guards (don't double-fire): `pending_close_symbols`, `_has_pending_scale_for_symbol`,
`_is_exit_retry_blocked`, `_is_scale_retry_blocked`. Exits are emitted as `close`/`scale`
`TradeIntentEvent`s (`_emit_close_intent`/`_emit_scale_intent`) → OMS executes.

---

## 8. Sizing & partial fills

- Entry qty = the open intent's `quantity` (`Position.quantity = original_quantity = quantity`).
  v2 default = `strategy_schwab_1m_v2_default_quantity` (100); momentum bots have their own.
- Scale: `apply_scale` appends the level, adds realized `scale_pnl += (exit_price−entry)×sell_qty`,
  `quantity −= sell_qty` (floored at 0). Subsequent tiers compute their % off the **reduced** qty.
- OMS handles partial fills on execution (`record_fill_if_needed`, incremental qty); the ladder's
  state is driven by the strategy-side `Position`, reconciled against broker fills.

---

## 9. Faithful-replay checklist (what the re-score must model)

1. Per entry: track `current/peak profit`, `tier` (peak-driven), `floor_pct` (peak-driven, ratchet-
   up), `scales_done`. 2. **Scale ladder** (NORMAL by default): +2%→50%, +4%-after-2%→25% (of
   remaining), or fast +4%→75%; sell % of CURRENT qty. 3. **Floor**: 1%→BE, 2%→+0.5%, 3%→+1.5%,
   4%+→trail peak−1.5%; breach → close remainder. 4. **Hard stop** entry−1.5%, fixed. 5. **Tier
   MACD/stoch exits** on bar close (need recomputed indicators; stricter at higher tier). 6.
   **Precedence** hard-stop>floor>scale>tier; quote-vs-bar cadence. 7. **No EOD flat** (decide the
   session-close assumption explicitly). 8. Realized P&L = Σ scale fills + final exit on the
   remainder. Model fills at the trigger price (idealized) — flag slippage as the Phase-2 upgrade,
   same as the scalp study.
