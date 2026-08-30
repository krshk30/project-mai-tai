# Schwab 30s Bot Entry Criteria

Last reviewed: 2026-06-16

This documents entry-side behavior only for the Schwab 30-second bot (`macd_30s`). Exit logic, scale-out logic, floor logic, and OMS execution handling are intentionally out of scope.

## Current Production Status

The strategy code path is implemented and registered as `macd_30s`, but the current VPS service environment has:

```text
MAI_TAI_STRATEGY_MACD_30S_ENABLED=false
MAI_TAI_STRATEGY_MACD_30S_BROKER_PROVIDER=schwab
MAI_TAI_STRATEGY_MACD_30S_DEFAULT_QUANTITY=10
```

No Schwab 30s config override JSON is set in the service env file at review time:

```text
MAI_TAI_STRATEGY_MACD_30S_COMMON_CONFIG_OVERRIDES_JSON=<empty>
MAI_TAI_STRATEGY_MACD_30S_CONFIG_OVERRIDES_JSON=<empty>
```

The rules below are therefore the implemented `make_30s_schwab_native_variant(quantity=10)` rules.

## Runtime Data Path

The Schwab 30s bot uses the `SchwabNativeEntryEngine` with `entry_logic_mode="schwab_native_30s"`.

Bars are built from Schwab `LEVELONE_EQUITIES` trade ticks through `SchwabNativeBarBuilderManager` at a 30-second interval. Current settings:

| Setting | Value |
| --- | --- |
| Bar interval | `30s` |
| Trade stream service | `LEVELONE_EQUITIES` |
| Live aggregate bars | `false` |
| Live aggregate fallback | `true` |
| Tick bar close grace | `7.5s` |
| Gap-fill bars | `false` for the 30s builder |

Indicator calculations are based on real, non-synthetic bars. The indicator engine requires at least `35` real bars before entries can be evaluated.

Regular VWAP is anchored to `09:30-16:00 ET`. The Schwab native 30s indicator path exposes only this regular-session VWAP for entry rules.

## Base Entry Config

| Rule | Value |
| --- | --- |
| Quantity | `10` |
| Trading window | `07:00 <= time < 18:00 ET` |
| Dead zone | Off (`00:00` to `00:00`) |
| Confirmation enabled | Yes |
| Confirmation bars | `1` |
| Base minimum score | `4/6` |
| P3 minimum score | `5/6` |
| Global minimum bar volume | `volume > 2,500` |
| Cooldown after exit | `10` bars |
| Warmup | `35` real bars |
| Intrabar entry | Off |
| P4 previous-bar intrabar entry | Off |
| Chop regime | On |

## Entry Flow

On each completed 30-second bar, the runtime checks entry in this order:

1. Runtime blockers outside the entry engine.
2. Entry-engine hard gates.
3. Warmup.
4. Pending confirmation, if a prior setup is waiting.
5. New path evaluation in strict priority order: `P1`, `P2`, `P3`, `P4`, `P5`.
6. Optional P3-specific hard-stop pause and P3 Stoch cap.
7. Confirmation or immediate BUY signal.

Only one path can win per bar. Higher-priority paths suppress lower-priority paths where the lower path checks `p1_available`, `p2_available`, or `p3_available`.

## Runtime Blockers Before Entry Engine

These are checked by `StrategyBotRuntime` before or around the entry engine.

| Blocker | Effect |
| --- | --- |
| Data halt | Blocks entries when the runtime marks symbol data as halted or critical. |
| Gap recovery active | Blocks new entry and records warning while the symbol is in gap-recovery mode. |
| Schwab entry freshness guard | Blocks entries if completed bars or trade ticks arrive too late versus the 30s interval. |
| Manual stop | Blocks entries for operator-stopped symbols. |
| Schwab ineligible cache | Blocks entries for symbols cached as Schwab-ineligible. |
| Bot lifecycle cooldown | Blocks entries while feed-retention lifecycle says the symbol needs recovery. It can reactivate only on qualifying signal behavior, especially P4 or VWAP/EMA20 reclaim. |
| Position tracker capacity | Blocks if max positions, daily loss limit, symbol entry cap, ticker loss pause, or hard-stop pause is active. |
| Existing position or pending open | Blocks duplicate entries for the same symbol. |

### Schwab Entry Freshness Guard

The freshness limit is:

```text
max(15 seconds, interval_secs * 0.5)
```

For 30s, this is `15s`.

It blocks entries when:

| Condition | Block reason |
| --- | --- |
| Completed bar received more than `15s` after bar close | `completed bar arrived ... after close` |
| History replay fills a missing live bar late | `completed live bar missing from Schwab stream` |
| Trade tick timestamp is more than `15s` stale | `trade tick arrived ... after exchange timestamp` |
| Bar builder detects live tick/bar stall | `bar builder stalled` |
| Bar builder detects late-trade revise storm | `late-trade revise storm` |

This is an entry safety guard. It prevents buying on stale or corrupted live bar state.

## Entry-Engine Hard Gates

These are evaluated before any path:

| Gate | Rule |
| --- | --- |
| Already in position | Block if `position_tracker.has_position(ticker)` |
| Same-bar dedup | Block if the bot already bought this ticker on this bar |
| Exit cooldown | Block until `10` bars have passed after the last exit |
| Rejected-open cooldown | Block until rejected-open cooldown expires |
| Trading hours | Block outside `07:00-18:00 ET` |
| Dead zone | Off by default |

After hard gates, the bot blocks until at least `35` real bars are available.

## Common Gates For P1/P2/P3

The engine computes a shared gate state:

```text
vol_ok = current volume > 2,500
ema_gate_ok = close > EMA20
stoch_gate_ok = StochK < 90
ema9_gate_ok = EMA9 distance < 8%
vwap_gate_ok = VWAP distance < 10% during regular session only
```

`P1` and `P2` require:

```text
p1p2_ok = ema_gate_ok and stoch_gate_ok and ema9_gate_ok and vwap_gate_ok
```

`P3` requires:

```text
p3_ok = common_ok and (vwap_gate_ok or p3_high_vwap_ok)
```

The news/momentum override branch exists in shared code but is disabled for Schwab 30s:

```text
p3_allow_momentum_override = false
```

### P3 High-VWAP Continuation Override

P3 can bypass the normal regular-session VWAP distance gate only when all are true:

| Rule | Value |
| --- | --- |
| Normal VWAP gate failed | Required |
| VWAP distance | `< 30%` |
| Close | `close > EMA9` |
| EMA trend | `EMA9 > EMA20` |
| EMA9 distance | `<= 2%` |
| EMA9 trend | Rising over recent bars |

This override affects only P3. It does not help P1 or P2.

## Quality Score

A setup score is calculated from six checks:

| Score item | Pass condition |
| --- | --- |
| `hist` | Histogram growing |
| `stK` | StochK rising |
| `vwap` | Price above VWAP |
| `vol` | Volume > `2,500` |
| `macd` | MACD increasing |
| `emas` | Price above EMA9 and EMA20 |

P1, P2, P4, and P5 use base minimum score `4/6` when confirmation is required. P3 uses `5/6`.

Important distinction:

P5 is immediate in code and does not go through the confirmation score check. It still returns a score for the signal payload, but its own internal P5 rules are the effective setup filter.

## Confirmation Rules

Current config:

```text
schwab_native_use_confirmation = true
confirm_bars = 1
```

P1, P2, P3, and classic P4 wait for one confirmation bar. P5 fires immediately.

During confirmation:

| Rule | Effect |
| --- | --- |
| Chop lock blocks path | Pending setup is cancelled |
| MACD cross below | Pending setup is cancelled |
| Stoch cross below exit level | Pending setup is cancelled |
| Not enough bars waited | Remains pending |
| Classic P4 confirmation failure | Pending setup is cancelled |
| Score below required | Pending setup is cancelled |
| Volume below `2,500` | Pending setup is cancelled |

Classic P4 has extra confirmation checks:

| P4 confirmation rule | Value |
| --- | --- |
| Next open max breakdown | Current open cannot be more than `1.0%` below setup close |
| Break setup high | Required |
| Close above setup close | Required |
| Close top band | Close must finish in top `50%` of bar range |

## Path Priority

Path priority is:

```text
P1_CROSS -> P2_VWAP -> P3_SURGE -> P4_BURST -> P5_PULLBACK
```

## P1: MACD Cross

P1 triggers when all raw P1 rules pass:

| Rule | Value |
| --- | --- |
| MACD cross | MACD crosses above signal on this bar |
| Prior below-signal duration | At least `3` real bars below signal before cross |
| Common gates | `p1p2_ok` |
| Global volume | `volume > 2,500` |
| Time allowed | `07:00-18:00 ET` |
| Chop | P1/P2 not blocked by chop |
| Volume ratio | Current volume >= `1.25 x vol_avg20` |
| Absolute volume | Current volume >= `7,500` |
| Dollar volume | `close * volume >= 25,000` |

If P1 passes raw path checks, it enters one-bar confirmation.

## P2: VWAP Breakout

P2 triggers when all raw P2 rules pass:

| Rule | Value |
| --- | --- |
| VWAP cross | Price crosses above VWAP on this bar |
| MACD state | MACD above signal |
| MACD trend | MACD increasing |
| Common gates | `p1p2_ok` |
| Global volume | `volume > 2,500` |
| Time allowed | `07:00-18:00 ET` |
| Chop | P1/P2 not blocked by chop |

P2 does not have the extra P1 volume-ratio, absolute-volume, or dollar-volume filters. It still uses the global `volume > 2,500` gate and confirmation volume check.

If P2 passes raw path checks, it enters one-bar confirmation.

## P3: MACD Surge

P3 triggers when all raw P3 rules pass:

| Rule | Value |
| --- | --- |
| MACD state | MACD above signal |
| Fresh cross exclusion | Current bar is not a MACD cross-above bar |
| MACD delta | `macd_delta >= 0.001` |
| MACD acceleration | `macd_delta > macd_delta_prev` |
| Histogram floor | Histogram >= `0.01` |
| Price location | Price above EMA9 |
| Absolute volume | Current volume >= `10,000` |
| Dollar volume | `close * volume >= 35,000` |
| Volume ratio | Current volume >= `1.5 x vol_avg20` |
| Common/P3 gates | `p3_ok` |
| Cross age | Last MACD cross-above within `4` bars |
| Recent runup cap | Recent runup <= `8%` over `8` bars |
| Global volume | `volume > 2,500` |
| Time allowed | `07:00-18:00 ET` |
| Chop | P3 not blocked by chop, unless extreme P3 override is active |
| P3 Stoch cap | StochK < `80` after path selection |

If P3 passes raw path checks and P3-specific blockers, it enters one-bar confirmation with required score `5/6`.

### P3 Hard-Stop Pause

If a prior `P3_SURGE` trade exits by hard stop, P3 is paused for `30` minutes for that ticker. This blocks only future P3 entries, not P1/P2/P4/P5.

## P4: Burst

P4 is enabled and uses classic P4 behavior. Current config has:

```text
p4_classic_requires_confirmation = true
p4_prev_bar_entry_enabled = false
```

Raw classic P4 requires:

| Rule | Value |
| --- | --- |
| Higher-priority paths | P1, P2, and P3 are not available |
| Candle color | Close > open |
| Body/range expansion | Body >= `4%` from open OR full range >= `5%` from open |
| Close location | Close in top `35%` of range |
| Volume | Current volume >= `1.5 x vol_avg20` |
| Breakout | Current high > highest high of prior `3` bars |
| EMA9 | Close > EMA9 |
| Time allowed | `07:00-18:00 ET` |
| P4 enabled | True |

Classic P4 then waits for one confirmation bar because `p4_classic_requires_confirmation=true`.

## P5: Pullback

P5 is immediate and does not use confirmation.

The engine first tracks spike anchors. A spike anchor is a green bar whose high is at least `2.5%` above EMA9.

P5 requires:

| Rule | Value |
| --- | --- |
| Spike anchor age | At least `2` bars ago and no more than `15` bars ago |
| Near session high | Current close no more than `20%` below session high |
| Giveback | Current open at least `2%` below spike-anchor high |
| EMA9 support touch | Open near EMA9 within `1%`, or low near EMA9 within `1%`, or low <= EMA9 |
| Resume candle | Close > open |
| Resume above EMA9 | Close > EMA9 |
| Not a burst body | Body < `3.5%` from open |
| Close position | Close ratio >= `0.50` inside bar range |
| Volume | Current volume >= `0.90 x vol_avg5` |
| Mini-breakout | Close > highest high of prior `3` bars |
| EMA9 not crashing | EMA9 >= prior EMA9 * `0.995` |
| Prior upmove | Close at least `3%` above recent low over `12` bars |
| Time allowed | `07:00-18:00 ET` |

## Chop Regime

Chop regime is enabled for Schwab 30s. It is evaluated only when the bar is in regular session, VWAP is valid, ATR is valid, and enough history exists.

Chop can block P1/P2 and P3. P4/P5 are not blocked by chop in the same way.

### Chop Detection Inputs

| Input | Value |
| --- | --- |
| ATR length | `14` |
| EMA20/VWAP compression | `abs(EMA20 - VWAP) < ATR * 0.25` |
| EMA20 flat lookback | `5` bars |
| EMA20 flat threshold | `abs(current EMA20 - EMA20 5 bars ago) < ATR * 0.35` |
| Cross lookback | `10` bars |
| Whipsaw threshold | At least `3` EMA20/VWAP crosses |
| Clean-side lookback | `10` bars |
| Clean-side threshold | At least `7` bars on one clean side |
| Chop trigger | At least `2` chop hits |

The four chop hit labels are:

| Label | Meaning |
| --- | --- |
| `COMPRESS` | EMA20 and VWAP are compressed |
| `EMA20_FLAT` | EMA20 is flat |
| `WHIPSAW` | Price has crossed EMA20/VWAP too often |
| `NO_CLEAN_SIDE` | Price lacks a clean bullish or bearish side |

When chop becomes active:

| Path | Effect |
| --- | --- |
| P1 | Blocked |
| P2 | Blocked |
| P3 | Blocked unless extreme P3 override is true |
| P4 | Not directly blocked by chop |
| P5 | Not directly blocked by chop |

### Chop Restart

Once active, chop lock clears only when a long restart is ready:

| Restart rule | Value |
| --- | --- |
| VWAP closes | Last `5` bars close above VWAP |
| EMA20 trend | EMA20 rising for last 3 bars |
| Pullback held | Within last `5` bars, a low touched EMA20 or VWAP and closed back above both |
| Breakout | Current close breaks above highs from prior `5` bars |

### Extreme P3 Override During Chop

P3 can bypass chop only when the extreme momentum override passes:

| Rule | Value |
| --- | --- |
| Range expansion | Current range >= ATR * `1.20` |
| Volume | Current volume >= `1.80 x vol_avg20` |
| MACD | MACD above signal |
| MACD delta | `macd_delta >= surge_rate * 2.00` |
| Histogram | Histogram growing and >= max(hist floor, hist abs avg * `1.25`) |
| Clear above structure | Close > EMA20 and VWAP, and distance from max(EMA20, VWAP) >= ATR * `0.10` |

## Position Tracker Entry Blockers

The position tracker can reject an otherwise valid signal before an open intent is emitted.

| Blocker | Current config |
| --- | --- |
| Max positions | `10` |
| Daily loss limit | `-500` |
| Per-symbol session entry cap | Off (`0`) |
| Consecutive-loss pause | Off for Schwab 30s default (`ticker_loss_pause_streak_limit=0`) |
| Hard-stop pause | After `2` hard-stop losses, pause ticker for `60` minutes |

The P3-specific hard-stop pause is separate from the position tracker hard-stop pause.

## Signal Payload

When an entry signal is emitted, the signal includes:

```text
action=BUY
ticker
path
quantity
price
score
score_details
macd
signal
histogram
stoch_k
ema9
ema20
vwap
bar_volume
```

For Schwab native 30s, `extended_vwap` and `decision_vwap` are set to the same regular-session VWAP value in the signal payload.

## Source Files

| Area | File |
| --- | --- |
| Schwab 30s native bar builder, indicator engine, and entry engine | `src/project_mai_tai/strategy_core/schwab_native_30s.py` |
| 30s config defaults and Schwab-native variant | `src/project_mai_tai/strategy_core/trading_config.py` |
| Runtime blockers, freshness guard, lifecycle blocker, and bot wiring | `src/project_mai_tai/services/strategy_engine_app.py` |
| Position tracker blockers | `src/project_mai_tai/strategy_core/position_tracker.py` |

