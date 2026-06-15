# v2 ATR-Flip "fresh-flip" qualifier — design (design-first; nothing live)

**Status:** design only — no code changed, nothing live. Brings the qualifier design + the 7-week threshold
validation for operator review. ATR-Flip path ONLY.

## Motivation (the data)
The Track-B loser-signature analysis (read-only, v2's real engine over the rotating top-movers-per-day
7-week sample, 740 ATR-Flip entries) found ATR's single cleanest separator: **`atr_state_age`** — how late in
the short segment the flip fired.
- **Winners avg age 2.6; losers avg age 16.3** (+13.7 raw separation — 3× cleaner than P1/P2's best).
- Mechanism: winners are **fresh reversals** (V-bottom bounce ~2–3 bars in); losers are **late bounces in a
  long-declining short segment** (~16 bars = dead-cat-bounce / falling knife).
- Idealized win% (+2% scale before −1.5% stop, 30s bars): **46% baseline**.

### 7-week threshold validation (the data picks the level)
| keep `atr_age <` | kept (decided) | **kept win%** | screened | screened loss% |
|---|---|---|---|---|
| **5** | 436 (66%) | **63%** | 229 | **86%** |
| 8 | 459 (69%) | 61% | 206 | 86% |
| 12 | 486 (73%) | 59% | 179 | 88% |

Screened set is 86–88% losers at all thresholds → screening is beneficial throughout; tighter = higher win%.
**Recommended default: `atr_state_age < 5`** (lifts ATR 46% → ~63% win idealized while keeping 66% of entries).
Tunable; not frozen — confirm forward + on Schwab ticks.

## The qualifier
**The primary signal is ALREADY emitted** — `atr_signal["state_age"]` is what we measured (it becomes
`metadata["atr_state_age"]`). So NO new indicator/capture is needed for this gate.

New `SchwabV2Config` fields (defaults make it behavior-neutral OFF):
```
atr_flip_use_max_state_age: bool = False   # OFF -> exact current behavior (parity)
atr_flip_max_state_age: int = 5            # fresh-flip ceiling (screen age >= this)
```

Gate in `_maybe_atr_emit` (`schwab_1m_v2.py`), after the flag/fresh/vol-floor checks and the variant
entry determination, before building the draft:
```py
if self._atr_use_max_state_age:
    age = atr_signal.get("state_age")
    if age is not None and int(age) >= self._atr_max_state_age:
        return None   # fresh-flip-only: skip late-segment (dead-cat-bounce) entries
```
Wired into `_evaluate_completed_bar`'s ATR emit only → **ATR-Flip path ONLY; P1/P2 untouched.**

## Invariants
1. **ATR-only.** The gate lives in the ATR emit path; MACD/VWAP entries and (the deployed engine's) gates
   are not touched.
2. **Behavior-neutral when off.** `atr_flip_use_max_state_age=False` default → ATR fires exactly as today;
   the existing ATR tests (whose fixtures build long short-segments) stay green with the gate off.
3. **Config-driven / tunable** — ceiling is one config value; 5 is a starting point to confirm forward.
4. **No new capture** for the primary gate (state_age already in `atr_signal`/metadata).

## 🔴 Critical cross-path caution (measured, not assumed)
**Do NOT apply a stoch overbought cap to ATR.** ATR losers have *lower* stoch (78.7) than winners (87.1) —
overbought = *winner* (it's a reversal-into-strength path), the OPPOSITE of P1/P2. The P1/P2 overbought fix
is path-specific; applying it to ATR would screen ATR's winners. Gates are per-path.

## Tests
- **Parity (gate off):** an ATR touch that fires today still fires (existing ATR tests green).
- **Gate on, fresh:** a touch with `state_age < ceiling` → fires.
- **Gate on, late:** a touch with `state_age >= ceiling` → does NOT fire (the screen).
- **P1/P2 untouched:** a MACD/VWAP entry is unaffected by the ATR gate.

## Deploy + validate (when approved — design-first for now)
Ship dormant (flag off) → attended enable, v2-only restart (like the prior flips). Then: forward-test the
screened-ATR win% vs baseline; the **"improved ATR" (screened, ~63% idealized) — not raw ATR (46%) — is what
the eventual ATR-only Schwab-credential go-live rides on.** Schwab ticks remain the P&L arbiter (the
fresh-flip threshold + the idealized win% are starting points to confirm with real fills).

## Separate, lower-priority (its own design-first change): ATR metadata capture
ATR's emit metadata captures `atr_*` but NOT the **secondary** context (stoch / rel_vol / ema9_dist /
vwap_dist) — which is why Track B had to reconstruct them. Add those to the ATR intent metadata so future
ATR fires are diagnosable without reconstruction AND to enable the **secondary** qualifiers the analysis
flagged (additive, weaker than fresh-flip): a **rel-vol floor** (losers avg 3.1× vs winners 4.3×) and mild
**ema9_dist** screen. Small, isolated, design-first — separate from this gate.

## Caveats (carried)
Idealized (+2% before −1.5% on **30s bars**; ~10% both-hit ambiguous excluded); **Polygon feed ≠ v2's
Schwab feed** — directional; the fresh-flip threshold (5) is a starting point. Nothing built; any port is a
gated, attended, tested change validated wave-by-wave on forward + Phase-2 data.
