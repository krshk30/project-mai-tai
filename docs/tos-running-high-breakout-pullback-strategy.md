# TOS Running-High Breakout + Pullback Resume Strategy

This document describes the latest Thinkorswim strategy script developed for the running-high breakout and pullback-resume idea.

The script is intended for visual validation and strategy/backtest-style analysis in Thinkorswim. It is not a guarantee of true tick-by-tick broker execution. Thinkorswim `AddOrder()` is strategy/backtest logic and should be treated as bar-based order simulation. The custom bubbles and alerts are used to make the strategy behavior easier to see while testing.

## High-Level Purpose

The strategy is designed for volatile momentum stocks where buying only the new high can be late. It has two independent buy paths:

- `P1_BREAKOUT`: buy a validated running-high breakout.
- `P2_PULLBACK`: buy a pullback-resume move after price pulls back from the segment high and starts climbing again.

Whichever path fires first while the script is flat can enter the trade.

The exit model is simple and deterministic:

- Sell at profit target.
- Sell at hard stop.
- Sell if the breakout fails quickly.
- Sell on ATR trail break after the early failure window.
- Optional time stop, disabled by default.

## Important Timing Assumptions

Thinkorswim strategy orders are not the same as an automated live execution engine.

Key behavior:

- `AddOrder()` is strategy/backtest order logic.
- Historical bars are evaluated after the bar exists.
- Live chart plots, labels, bubbles, and alerts can update during the forming candle.
- Strategy order labels can appear with next-bar behavior.
- The script uses a tracked internal trade state to align buy/sell bubbles with the strategy logic.

For real Mai Tai implementation, these rules should be ported explicitly into the bot runtime using either:

- bar-close confirmation mode, or
- intrabar partial-bar/tick evaluation mode.

## Default Trading Window

The script only allows entries during:

```text
7:00 AM ET to 6:00 PM ET
```

Rule:

```thinkscript
def inWindow = SecondsFromTime(0700) >= 0 and SecondsTillTime(1800) >= 0;
```

Exits are evaluated while a trade is active. The entry window does not force an end-of-day flatten by itself.

## ATR Trail Foundation

The strategy uses a modified ATR trailing stop as the trend state engine.

Inputs:

```text
ATRPeriod = 5
ATRFactor = 3.5
averageType = AverageType.WILDERS
```

The trail uses a modified true range calculation:

- `HiLo`: high-low range capped at `1.5 * average(high-low, ATRPeriod)`.
- `HRef`: upside reference range adjusted for gaps.
- `LRef`: downside reference range adjusted for gaps.
- `trueRange`: maximum of `HiLo`, `HRef`, and `LRef`.
- `loss`: `ATRFactor * MovingAverage(averageType, trueRange, ATRPeriod)`.

The ATR state can be:

- `init`
- `long`
- `short`

Only long-state entries are allowed.

The trail is plotted as points:

- Cyan when ATR state is long.
- Magenta when ATR state is short.

## Running Long Segment High

When ATR flips to long, a new long segment starts.

Definitions:

- `isLong`: ATR state is long.
- `justFlippedLong`: current bar is the first long bar after not being long.
- `segHigh`: running high of the current long segment.

Rule:

```text
If just flipped long: segHigh = current high
Else if still long: segHigh = max(previous segHigh, current high)
Else: segHigh = NaN
```

The breakout level is based on the previous bar's segment high:

```text
breakLevel = segHigh[1] * (1 + breakoutBufferPct / 100)
```

Default:

```text
breakoutBufferPct = 0.0
```

This keeps the actual price buffer at zero. The script relies on candle quality and volume validation instead of adding a fixed percentage above the high.

## Shared Candle Metrics

Both buy paths use some shared candle facts.

Definitions:

- `barRange = high - low`
- `body = AbsValue(close - open)`
- `closePosition = (close - low) / barRange`
- `greenBarOk = close > open`, unless disabled
- `volAvg20 = Average(volume, 20)`

The script uses close position to judge whether the bar is closing near the top of its range. On a live forming candle, `close` is effectively the current last price on the chart.

## P1 Buy Path: Running-High Breakout

P1 is the original breakout path.

Purpose:

- Buy when price breaks above the prior running high of the current ATR-long segment.
- Avoid weak wick-only breakouts by requiring candle/volume quality.

P1 inputs:

```text
confirmLookback = 3
closeTopPct = 35.0
minVolMult20 = 1.25
minVolMultRecent = 1.00
minBodyRangePct = 35.0
requireGreenBar = yes
requireCloseAboveBreak = yes
```

### P1 Prior High Rule

The script checks whether the current close is above the highest high of the prior `confirmLookback` bars:

```text
priorHigh = Highest(high[1], confirmLookback)
closedAboveRecentHigh = close > priorHigh
```

Default:

```text
confirmLookback = 3
```

If this is reduced to `2` or `1`, P1 can become easier to trigger. However, this does not affect P2.

### P1 Breakout Rule

P1 requires the current high to break the running-high breakout level:

```text
brokeRunningHigh = high > breakLevel
```

If `requireCloseAboveBreak = yes`, the current close/current last price must also be above the breakout level:

```text
closedAboveBreakLevel = close > breakLevel
```

This reduces false wick breakouts.

### P1 Close-Near-High Rule

Default:

```text
closeTopPct = 35.0
```

The close/current price must be in the top 35% of the candle range:

```text
closePosition >= 1 - closeTopPct / 100
```

With `closeTopPct = 35`, the close must be at or above the 65% point of the candle range.

Tuning:

- Lower value, such as `25`, is stricter.
- Higher value, such as `45`, is looser and may enter earlier.

### P1 Green Bar Rule

If `requireGreenBar = yes`, the current close must be above open:

```text
close > open
```

This prevents buying red breakout attempts.

### P1 Body Strength Rule

Default:

```text
minBodyRangePct = 35.0
```

The candle body must be at least 35% of the full candle range:

```text
AbsValue(close - open) / (high - low) >= 0.35
```

This avoids doji/long-wick bars with poor commitment.

Tuning:

- `25` to `30`: looser, more entries.
- `35`: balanced default.
- `45` or higher: stricter, fewer weak entries.

### P1 Volume Rule

P1 requires both:

```text
volume >= Average(volume, 20) * minVolMult20
volume >= Average(volume[1], confirmLookback) * minVolMultRecent
```

Defaults:

```text
minVolMult20 = 1.25
minVolMultRecent = 1.00
```

This means current volume must be at least:

- 1.25x the 20-bar average volume, and
- 1.00x the prior 3-bar average volume.

Tuning:

- If too few P1 entries: reduce `minVolMult20` to `1.10`.
- If too many weak entries: raise `minVolMult20` to `1.50`.

### P1 Full Rule

P1 buy setup is true when all are true:

```text
inWindow
ATR state is long
not first ATR long flip bar
breakLevel exists
high breaks breakLevel
close is above prior confirmLookback high
close is above breakLevel if requireCloseAboveBreak is yes
close/current price is near candle high
green bar if required
body is strong enough
volume is strong enough
```

The script intentionally does not buy the first ATR flip bar:

```text
!justFlippedLong
```

This avoids aggressive first-flip entries in volatile live markets.

## P2 Buy Path: Pullback Resume

P2 is the added path for the scenario where the first high breakout is already exhausted. It waits for price to pull back from the segment high, hold trend structure, and resume through short-term resistance.

Purpose:

- Avoid chasing the old high.
- Enter during the recovery/resume after a pullback.
- Capture the move before price fully retests the old segment high.

P2 inputs:

```text
enablePullbackEntry = yes
pullbackMinPct = 2.0
pullbackMaxPct = 12.0
resumeLookback = 3
resumeVolMult = 1.10
resumeCloseTopPct = 35.0
avoidOldHighPct = 0.25
requirePullbackHoldTrail = yes
```

### P2 Pullback Depth Rule

P2 measures pullback from the previous segment high to the current low:

```text
pullbackFromHighPct = (segHigh[1] - low) / segHigh[1] * 100
```

Default valid range:

```text
2.0% to 12.0%
```

Rules:

```text
pullbackFromHighPct >= pullbackMinPct
pullbackFromHighPct <= pullbackMaxPct
```

Tuning:

- Earlier/more sensitive: reduce `pullbackMinPct` to `1.5`.
- More conservative: keep `2.0` or raise to `3.0`.
- Avoid deep falling-knife recoveries: reduce `pullbackMaxPct`.

### P2 Mini-Resistance Resume Rule

P2 uses recent mini-resistance:

```text
recentMiniResistance = Highest(high[1], resumeLookback)
```

Default:

```text
resumeLookback = 3
```

P2 requires:

```text
close > recentMiniResistance
```

This means price must resume above the highest high of the previous `resumeLookback` bars.

Important:

- `confirmLookback` affects P1 only.
- `resumeLookback` affects P2.

If P2 is not entering earlier when changing `confirmLookback`, this is expected. For P2, adjust `resumeLookback`.

Tuning:

- Earlier P2: `resumeLookback = 2` or `1`.
- More confirmation: `resumeLookback = 4` or `5`.

### P2 Resume Volume Rule

P2 requires current volume to be stronger than recent pullback volume:

```text
volume >= Average(volume[1], resumeLookback) * resumeVolMult
```

Default:

```text
resumeVolMult = 1.10
```

Tuning:

- Earlier/more entries: reduce to `1.00`.
- Cleaner resumes: raise to `1.20` or `1.30`.

### P2 Resume Close-Near-High Rule

Default:

```text
resumeCloseTopPct = 35.0
```

The resume candle must close/currently trade in the top 35% of its candle range.

Tuning:

- Earlier/looser: `45`.
- Cleaner/stricter: `25` to `30`.

### P2 Trail Hold Rule

Default:

```text
requirePullbackHoldTrail = yes
```

P2 requires the pullback to respect the ATR trail enough:

```text
low >= trail or close > trail
```

This allows temporary wick pressure but requires the price to still be above trail by close/current price if the low pierced it.

If disabled, P2 can take deeper recovery trades, but this increases falling-knife risk.

### P2 Avoid Old High Rule

Default:

```text
avoidOldHighPct = 0.25
```

P2 avoids buying too close to the old segment high:

```text
close <= segHigh[1] * (1 - avoidOldHighPct / 100)
```

This keeps P2 distinct from P1. The intent is to buy the resume before price fully returns to the exhausted old high.

Tuning:

- Earlier and further from old high: increase to `0.50`.
- More permissive near old high: reduce to `0.10` or `0.00`.

### P2 Full Rule

P2 buy setup is true when all are true:

```text
P2 enabled
inWindow
ATR state is long
not first ATR long flip bar
segment high exists
pullback depth is between min and max
pullback holds trail, if required
close breaks above recent mini-resistance
close/current price is near candle high
green bar, if required
volume exceeds recent pullback average
price is not too close to old segment high
```

## Entry Selection

The script combines both paths:

```text
buySetup = p1BuySetup or p2BuySetup
```

While flat, if either path is true, the strategy can buy.

Path labeling:

- P1 order label: `BUY P1`
- P2 order label: `BUY P2`
- P1 bubble color: cyan
- P2 bubble color: light green

If both P1 and P2 are true on the same bar, P2 is not separately prioritized in the order label unless the state later records P2. In practice, P1 and P2 are designed to be different enough that both should not commonly fire together.

## Internal Trade State

The script tracks its own state because Thinkorswim strategy order labels can be visually offset.

State variables:

- `inTrade`: whether the script is tracking an active trade.
- `entryPrice`: entry price used for stop/target calculations.
- `entryBreakLevel`: breakout/resume level used for failed-breakout exit.
- `entryPath`: `1` for P1, `2` for P2.
- `barsInTrade`: number of bars since the tracked trade became active.
- `entryActive`: event used to show the visible BUY bubble and fire the buy alert.

The strategy order is emitted on the setup bar:

```text
buySig = yes when flat and buySetup is true
```

The visible tracked trade becomes active on the next bar when:

```text
inTrade[1] == 0 and buySetup[1]
```

This design was added to prevent sell/stop orders from appearing without a tracked buy.

## Exit Priority

The latest script uses this sell priority:

1. `TARGET`
2. `STOP`
3. `FAIL`
4. `TRAIL`
5. `TIME`

This matters when a 1-minute candle hits more than one condition. For example, if the same candle reaches both target and stop, the script marks it as `TARGET`.

This is a modeling choice for ambiguous 1-minute bars. It should be revisited if tick data is used later.

## Target Exit

Default:

```text
profitTargetPct = 2.0
```

Rule:

```text
high >= entryPrice * (1 + profitTargetPct / 100)
```

The target sell price is:

```text
entryPrice * 1.02
```

For low-priced stocks, the target can look visually close because 2% is only a few cents.

Example:

```text
Entry: 1.1200
2% target: 1.1424
```

Labels round to four decimals in the latest script.

Tuning:

- Faster scalping: keep `2.0`.
- Let winners breathe: raise to `3.0` or `4.0`.
- Future scale-out version could sell partial at 2% and trail the rest.

## Hard Stop Exit

Default:

```text
hardStopPct = 1.5
```

Rule:

```text
low <= entryPrice * (1 - hardStopPct / 100)
```

The stop sell price is:

```text
entryPrice * 0.985
```

Example:

```text
Entry: 4.3900
1.5% stop: 4.3242
```

Important:

- The stop only applies after the trade is active.
- A low before entry does not count.
- In historical 1-minute bars, exact intrabar sequence is unknown.

## Failed Breakout Exit

Default:

```text
failedBreakoutBars = 3
```

Rule:

```text
barsInTrade <= failedBreakoutBars
close < entryBreakLevel
```

Purpose:

- Exit quickly if the breakout or resume level fails soon after entry.
- Protects against fake breakouts and immediate rollovers.

For P1:

```text
entryBreakLevel = breakLevel at entry
```

For P2:

```text
entryBreakLevel = recentMiniResistance at entry
```

Tuning:

- Faster failure exit: `2`.
- More room: `4` or `5`.

## ATR Trail Exit

Rule:

```text
barsInTrade > failedBreakoutBars
close < trail
```

Purpose:

- After the early failure window ends, use the ATR trail as the trend exit.
- Allows runners to continue while still respecting trend deterioration.

This exit is intentionally delayed until after `failedBreakoutBars`, so the early trade is not immediately stopped by normal ATR noise.

## Optional Time Stop

Default:

```text
useTimeStop = no
timeStopBars = 8
timeStopMinProfitPct = 0.75
```

When enabled, the rule is:

```text
barsInTrade >= timeStopBars
close < entryPrice * (1 + timeStopMinProfitPct / 100)
```

Meaning:

```text
If the trade has been open for 8 bars and has not reached at least +0.75%, exit.
```

The time stop is disabled by default because it can prematurely exit volatile trades that later recover.

Use it only if testing shows too many dead trades consuming attention/capital.

## Labels, Bubbles, And Alerts

Display inputs:

```text
showTradeDebugLabel = yes
showBuyBubble = yes
showSellBubble = yes
enableAlerts = yes
```

### Buy Bubble

The visible buy bubble appears when the internal tracked trade becomes active:

```text
entryActive
```

Bubble text:

```text
BUY P1 <entryPrice>
BUY P2 <entryPrice>
```

Prices are rounded to four decimals:

```thinkscript
AsText(Round(entryPrice, 4))
```

### Sell Bubble

The visible sell bubble appears when `sellSig` is true.

Possible text:

```text
TARGET <sellPrice>
STOP <sellPrice>
FAIL <close>
TRAIL <close>
TIME <close>
```

Colors:

- `TARGET`: green
- `STOP`: red
- other exits: gray

### Debug Label

When in a tracked trade, the chart label shows:

```text
ENTRY <entryPrice> | PATH P<entryPath> | STOP <stopPrice> | TARGET <targetPrice> | BARS <barsInTrade>
```

Purpose:

- Confirms which path entered.
- Shows active stop and target.
- Shows bar count.
- Helps verify why a target/stop did or did not trigger.

### Alerts

The latest script uses:

```thinkscript
Sound.Ring
```

Alerts:

- Buy alert on `entryActive`.
- Sell alert on `sellSig`.

Alert mode:

```thinkscript
Alert.BAR
```

This avoids repeated sound spam inside the same bar.

## Plot Lines

The script plots:

### ATR Trailing Stop

```text
TrailingStop
```

- Cyan points when ATR state is long.
- Magenta points when ATR state is short.

### Breakout Level

```text
BreakoutLevel
```

- Yellow dashed line.
- Represents the prior running segment high plus optional breakout buffer.
- Used by P1.

### Pullback Resume Level

```text
PullbackResumeLevel
```

- Light green dashed line.
- Represents recent mini-resistance.
- Used by P2.

### Hard Stop Line

```text
HardStopLine
```

- Red dashed line.
- Only visible while in trade.

### Profit Target Line

```text
ProfitTargetLine
```

- Green dashed line.
- Only visible while in trade.

## Parameter Tuning Guide

### To Enter P1 Earlier

Adjust:

```text
confirmLookback: 3 -> 2 or 1
closeTopPct: 35 -> 45
minVolMult20: 1.25 -> 1.10
minBodyRangePct: 35 -> 25 or 30
```

Risk:

- More false breakouts.
- More wick traps.

### To Make P1 Cleaner

Adjust:

```text
confirmLookback: 3 -> 4 or 5
closeTopPct: 35 -> 25
minVolMult20: 1.25 -> 1.50
minBodyRangePct: 35 -> 45
```

Risk:

- Later entries.
- Misses faster moves.

### To Enter P2 Earlier

Adjust:

```text
resumeLookback: 3 -> 2 or 1
resumeVolMult: 1.10 -> 1.00
resumeCloseTopPct: 35 -> 45
pullbackMinPct: 2.0 -> 1.5
avoidOldHighPct: 0.25 -> 0.10
```

Important:

- Changing `confirmLookback` does not move P2.
- P2 uses `resumeLookback`.

### To Make P2 Cleaner

Adjust:

```text
resumeLookback: 3 -> 4 or 5
resumeVolMult: 1.10 -> 1.20 or 1.30
resumeCloseTopPct: 35 -> 25 or 30
pullbackMinPct: 2.0 -> 3.0
avoidOldHighPct: 0.25 -> 0.50
```

Risk:

- Later pullback entries.
- May miss sharp recoveries.

### To Reduce Quick Stops

Adjust:

```text
hardStopPct: 1.5 -> 2.0
failedBreakoutBars: 3 -> 2
```

Or make entry stricter so fewer weak pullback resumes enter.

### To Let Winners Run

Adjust:

```text
profitTargetPct: 2.0 -> 3.0 or 4.0
```

Future idea:

- partial exit at 2%
- trail remainder with ATR trail

### To Avoid Dead Trades

Enable:

```text
useTimeStop = yes
```

Start with:

```text
timeStopBars = 8
timeStopMinProfitPct = 0.75
```

Risk:

- Can exit before volatile names recover.

## Common Questions

### Why does changing `confirmLookback` not move a P2 buy?

Because `confirmLookback` is P1-only.

P2 uses:

```text
resumeLookback
```

### Why do buy and target sometimes look close together?

On low-priced stocks, 2% is only a few cents.

Example:

```text
1.12 entry
2% target = 1.1424
```

On the chart, that can visually look close. The latest labels round to four decimals to reduce confusion.

### Why can target fire quickly after buy?

If the next active bar's high is already above the target price, the strategy marks `TARGET`.

This is expected for volatile 1-minute candles.

### Why did the script prioritize target before stop?

Within a historical 1-minute bar, we do not know whether high or low happened first. The latest visual/backtest model gives target priority before stop to test the fast-scalp winner scenario.

For production tick-based implementation, this should be resolved using actual tick order.

### Why not use VWAP?

VWAP can block valid edge cases in fast low-float momentum names. The current design intentionally avoids VWAP and instead uses:

- ATR trend state
- running high
- pullback depth
- mini-resistance resume
- volume confirmation
- candle quality

### Is this live-executable?

The TOS script itself is best treated as a visual/backtest prototype.

For live Mai Tai execution, the logic must be implemented in the bot with explicit timing:

- bar-close mode, or
- intrabar partial-bar/tick mode.

## Suggested Default Configuration

Balanced defaults:

```text
ATRPeriod = 5
ATRFactor = 3.5
averageType = WILDERS
breakoutBufferPct = 0.0

confirmLookback = 3
closeTopPct = 35.0
minVolMult20 = 1.25
minVolMultRecent = 1.00
minBodyRangePct = 35.0
requireGreenBar = yes
requireCloseAboveBreak = yes

enablePullbackEntry = yes
pullbackMinPct = 2.0
pullbackMaxPct = 12.0
resumeLookback = 3
resumeVolMult = 1.10
resumeCloseTopPct = 35.0
avoidOldHighPct = 0.25
requirePullbackHoldTrail = yes

hardStopPct = 1.5
profitTargetPct = 2.0
failedBreakoutBars = 3
useTimeStop = no
timeStopBars = 8
timeStopMinProfitPct = 0.75

showTradeDebugLabel = yes
showBuyBubble = yes
showSellBubble = yes
enableAlerts = yes
```

## Current Latest Script

The latest script version includes:

- P1 breakout entry.
- P2 pullback-resume entry.
- target-first sell priority.
- stop/target/fail/trail/time exits.
- visible buy/sell bubbles.
- four-decimal labels.
- debug label.
- `Sound.Ring` alerts.

When implementing this in Mai Tai, preserve the path names:

```text
P1_BREAKOUT
P2_PULLBACK
```

and store enough diagnostics to explain why each path did or did not fire:

```text
is_long
just_flipped_long
seg_high
break_level
recent_mini_resistance
pullback_from_high_pct
close_near_high
green_bar
body_ok
volume_ok
held_trail
not_buying_old_high
entry_path
entry_price
stop_price
target_price
exit_reason
```

