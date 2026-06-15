# Fix (a): ATR-Flip fires at its own warmup, not MACD's 135-bar settling

**Status:** design-first, PR held for operator review. Entry-side only. Live entry path.

## Problem (surfaced by the QTEX miss, 2026-06-15)

`SchwabV2Strategy._evaluate_completed_bar` (`strategy_core/schwab_1m_v2.py`) computes the ATR-Flip
signal every bar (line ~660) but then hard-bails at the MACD warmup-settling guard:

```py
if len(state.bars) < min_bars:        # min_bars = macd_slow(26)+macd_signal(9)+settling(100) = 135
    return None
```

The ATR-Flip **emit** lives *below* that guard, so ATR-Flip inherits MACD's 135-bar requirement even
though the ATR trailing stop is well-defined after its own short warmup (~2×period ≈ 10 bars). Result:
**freshly-scanned symbols (enter mid-session) and post-restart symbols are blind to ATR-Flip for ~135
live bars** even though the ATR indicator is warm and correct. QTEX (added 14:59Z, ~67 bars at the 17:17
touch) was suppressed here; v2's own algorithm over v2's own bars reproduces the TOS BUY at 17:17Z exactly.

This fix addresses **only** the ATR mis-gating. It does **not** fix the post-restart ~135-bar blackout for
MACD/VWAP — that is fix (b) (DB-seed `state.bars`). See [[project-mai-tai-v2-entry-warmup-gate]].

## Decoupling proof (why this is safe)

The ATR path reads **only OHLCV + its own `atr_*` state** — zero reads of the MACD/stoch/VWAP/EMA values
that the 135-bar settling protects:
- `_update_atr_state` (450–577): `cur.high/low/close/volume` + `atr_*` fields only.
- `_maybe_atr_emit` (579–639): `atr_signal`, `cur.volume/close`, variant config only.
- ATR intent **metadata** (620–638): `atr_*`/`cur` fields only — no `macd_*`/`stoch`/`vwap`/`ema`
  (contrast the MACD path, which embeds `macd_value` etc. at 971).

So firing ATR under-warmed can never read a half-warm MACD/stoch/VWAP value. The only couplings to the
gated region are the **shared flat + cooldown gates** (plain `state` fields, no indicators) and the
**precedence** `not(path_macd or path_vwap)` — which is moot under-warmed because MACD/VWAP are
definitionally silent below 135 bars.

## Design

Replace the line-676 bail with an under-warmed ATR attempt; leave the warm path byte-identical.

```py
if len(state.bars) < min_bars:
    # MACD/VWAP are under-warmed and definitionally silent here, but ATR-Flip
    # has its own short warmup (atr_signal is None until the trail is defined)
    # and reads only OHLCV + atr_* state, so it can fire now. Honor the SAME
    # entry gates as the warm path (flat + no cooldown) and tick the cooldown
    # so an ATR entry's cooldown elapses on schedule.
    if state.cooldown_bars_remaining > 0:
        state.cooldown_bars_remaining -= 1
    if state.position_qty > 0 or state.cooldown_bars_remaining > 0:
        return None
    if atr_signal is None:
        return None                      # ATR trail not defined yet
    cur_uw = state.bars[-1]
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    fresh = ((now_ms - cur_uw.timestamp_ms) / 1000.0) <= MAX_BAR_AGE_SECONDS_FOR_EMIT
    return self._maybe_atr_emit(state, cur_uw, atr_signal, fresh)
# >= min_bars: fall through to the existing warm flow (UNCHANGED)
```

## Invariants preserved

1. **Warm path byte-identical** (`len >= min_bars` skips the new block) → parity oracle + all 5 existing
   ATR tests stay green (they all use ≥135 bars).
2. **No emit on replayed history** — `_maybe_atr_emit` still gates on `bar_is_fresh` (line 593); a warmup
   replay (non-fresh bars) emits nothing even under-warmed.
3. **Flag-gating** — `_maybe_atr_emit` returns None when `_atr_enabled` is off (line 591).
4. **Precedence MACD>VWAP>ATR** — preserved: under-warmed MACD/VWAP cannot fire; warm path unchanged.
5. **Liquidity floor** — still the only ATR filter (`_maybe_atr_emit`, line 595).
6. **Cooldown** — now ticks once per new bar in BOTH the under-warmed and warm paths (new: under-warmed
   bars previously didn't tick, but no entry could fire there either; now one can, so it must tick).

## Tests

- Existing 5 (all warm-path) — unchanged, must stay green; the oracle test is the load-bearing pin.
- NEW `test_atr_fires_under_warmed_below_min_bars`: ~31-bar fixture (short segment + fresh touch, < 135)
  → with flag ON emits an "ATR Flip" intent (this is the QTEX scenario). Pre-fix it returned None.
- NEW `test_atr_under_warmed_respects_flat_and_cooldown`: same fixture with an open position / active
  cooldown → no emit.
- NEW `test_macd_vwap_still_silent_under_warmed`: a would-be MACD cross at < 135 bars still emits nothing
  (only ATR fires under-warmed; the MACD guard still protects MACD/VWAP).
- NEW `test_atr_under_warmed_no_emit_on_stale_bar`: under-warmed + stale final bar → no emit (fresh gate).

## Out of scope
- The all-paths post-restart blackout (fix (b), DB-seed `state.bars`).
- Any change to `_update_atr_state` math (the validated oracle is frozen).
