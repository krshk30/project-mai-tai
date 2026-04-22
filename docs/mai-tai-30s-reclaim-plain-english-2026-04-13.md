# Mai Tai 30s Reclaim Guide

## What This Bot Is

`Mai Tai 30s Reclaim` is a paper-trading bot that looks for a stock to:

- make a fast move up
- pull back in a controlled way
- hold support
- reclaim strength before the next leg

This is not a breakout-chasing bot.
It is trying to buy a cleaner second-chance entry after a pullback.

## What Feeds This Bot

The bot does not pick stocks by itself.

The flow is:

1. the momentum scanner confirms a symbol
2. the strategy engine puts that symbol on the reclaim watchlist
3. the reclaim bot builds 30-second bars
4. the reclaim bot checks whether the reclaim setup is good enough
5. if it passes, the bot opens a paper trade

So when you see a symbol on the reclaim page, it means:

- the scanner sent it
- reclaim is watching it
- reclaim may still block it if the setup is weak

## Current Live Paper Setup

This is the current live paper-trading shape in the project right now.

- bot name: `Mai Tai 30s Reclaim`
- bar size: `30 seconds`
- execution mode: `paper`
- default total size: `100 shares`
- starter size: `25 shares`
- confirm/add size: `75 shares`
- hard stop: about `1.5%` below entry

Current reclaim excluded symbols:

- `JEM`
- `CYCN`
- `BFRG`
- `UCAR`
- `BBGI`

## What Reclaim Is Looking For

In simple terms, the reclaim bot wants to see:

- a recent push up
- a real pullback, not just random noise
- a touch or interaction near support
- price getting back above support cleanly
- enough momentum to suggest the move is turning back up

Support here mainly means:

- `EMA9`
- `VWAP`
- with trend help from `EMA20`

## Main Reclaim Checks

The bot checks these groups:

### 1. Warmup

The bot needs recent 30-second bar history before it can judge reclaim shape.

After the recent restart fix, same-day historical bars are reused on restart, so reclaim should not have to sit through a fake fresh warmup again.

Current warmup requirement:

- at least `14` closed 30-second bars
- about `7 minutes` if the symbol is truly brand new and there is no same-day history available yet

### 2. Pullback

The bot wants a real pullback from a recent high.

It checks:

- percent pullback from the recent high
- retrace of the recent impulse leg
- whether the stock held enough of the move

Current live reclaim values:

- reclaim lookback window: `8` bars
- minimum pullback from recent high: `0.5%`
- maximum pullback from recent high: `15%`
- leg retrace gate enabled: `yes`
- minimum retrace fraction of impulse leg: inherited default `20%`
- maximum retrace fraction of impulse leg: `1.2`

Currently not used as hard blockers in the live reclaim variant:

- hard higher-low rule
- hard held-move rule
- hard pullback-absorption rule

### 3. Touch

The bot wants the pullback to interact with support.

That usually means:

- price touched or came near `EMA9`
- or touched or came near `VWAP`

Current live reclaim values:

- touch lookback window: `8` bars
- touch tolerance: `1%`
- current bar touch is allowed: `yes`

### 4. Location

This is one of the most important filters.

The bot wants the reclaim bar to be in the right place relative to:

- `EMA9`
- `VWAP`
- `EMA20`

If the stock is too weak or too stretched, reclaim blocks it.

Current live reclaim values:

- maximum extension above `EMA9`: `2%`
- maximum extension above `VWAP`: `4%`
- allowed pullback below `EMA9` support line: `1%`
- touch-recovery location branch: `off`
- single-anchor recovery branch: `off`

Plain English:

- if price is still below support, reclaim blocks it
- if price is too stretched above support, reclaim blocks it
- if price is not sitting in a healthy reclaim zone, reclaim blocks it

### 5. Momentum

The bot checks whether the bar is actually starting to turn back up.

This includes:

- MACD behavior
- histogram behavior
- whether the move looks like real follow-through

Current live reclaim values:

- momentum gate: `on`
- MACD near-signal allowance: `0.12 x ATR`

### 6. Candle Quality

The bar should not look too weak.

The bot checks things like:

- body size
- close position in the bar
- upper wick size

Current live reclaim values for a full starter bar:

- minimum body: `30%`
- minimum close position in bar: `60%`
- maximum upper wick: `30%`

Current live reclaim values for a softer armed bar:

- minimum body: `15%`
- minimum close position in bar: `45%`
- maximum upper wick: `45%`

### 7. Volume

The bot still measures reclaim bar volume, but in the current live reclaim variant it is not being used as a hard blocker.

Current live reclaim values:

- reclaim relative-volume threshold on the config: `1.10`
- hard volume requirement in the live reclaim variant: `off`

### 8. Trend

The reclaim should still be happening inside a usable trend, mainly:

- price above `EMA20`
- `EMA9` not collapsing below `EMA20`

Current live reclaim values:

- trend gate: `on`

### 9. Stoch

This is an overbought health check.

Current live reclaim values:

- system stoch cap: `90`
- hard stoch requirement in the live reclaim variant: `off`

## Current Reclaim Score

The reclaim score is not a giant all-purpose score.
Right now it mainly counts three things:

- reclaim break
- momentum
- volume

Current live reclaim score threshold:

- minimum score required: `2`

Because volume is not a hard blocker in the live reclaim variant, the score still matters as a quality check.

## Reclaim State Machine

This is the easiest way to understand how the bot moves from “watching” to “trading.”

### State 1. Watchlist

The scanner sends a symbol.
Reclaim starts watching it.

### State 2. Warmup

Reclaim waits until it has enough recent closed 30-second bars.

### State 3. Candidate Check

On each closed 30-second bar, reclaim checks:

- pullback
- touch
- location
- momentum
- candle
- trend
- score

### State 4. Starter Ready

If reclaim gets:

- reclaim break
- valid location
- valid starter candle

it can enter immediately with a starter buy.

Starter path name:

- `PRETRIGGER_RECLAIM`

### State 5. Armed

If reclaim is promising but not strong enough for an instant starter, it can arm instead of buying.

Armed path name:

- `PRETRIGGER_RECLAIM_ARMED`

Current armed lookahead:

- `1` bar

That means reclaim can wait one more bar for a clean break.

### State 6. Armed Break Entry

If the next bar breaks the armed reclaim level cleanly, reclaim can enter.

Path name:

- `PRETRIGGER_RECLAIM_BREAK`

### State 7. Confirm/Add

If the starter works and follow-through confirms, reclaim can add.

Current confirm path:

- `R1_BREAK_CONFIRM`

Decision label:

- `PRETRIGGER_ADD_R1_BREAK_CONFIRM`

### State 8. No Confirm / Fail

If the starter does not work, reclaim can exit quickly.

Current live values:

- no-confirm / failed-break lookahead: `4` bars
- fail cooldown after failure: `4` bars

The two main failure labels are:

- `PRETRIGGER_NO_CONFIRM`
- `PRETRIGGER_FAIL_FAST`

## Entry Rules By State

### A. What Is Needed Before Any Entry

The bot usually needs all of this to line up:

- enough recent bars
- valid pullback
- valid touch
- valid location
- valid momentum
- valid trend
- enough reclaim score

Then it decides between starter now or armed first.

### B. Immediate Starter Entry

Reclaim can buy immediately if:

- reclaim break is ready
- starter location is valid
- candle quality is valid

Current starter quantity:

- `25 shares`

### C. Armed Then Break Entry

Reclaim can arm instead of buying immediately if:

- support / location is close enough
- softer candle is acceptable
- full immediate starter bar is not ready yet

Then it waits `1` bar for the armed break.

### D. Confirm/Add Entry

After the starter, reclaim can add only if the follow-through is good enough.

In simple terms it wants:

- break above the starter/reclaim level
- valid location
- valid momentum
- valid trend
- acceptable candle

Current add quantity:

- `75 shares`

## Exit Rules

Once reclaim is in a trade, exits are handled by the normal position and exit layer.

### 1. Hard Stop

The hard stop is:

- about `1.5%` below entry

Label:

- `HARD_STOP`

### 2. Hold Floor / Dynamic Floor

There are two floor ideas:

- reclaim starter hold floor used during the pretrigger phase
- dynamic position floor once the trade is open and moving

Dynamic floor logic:

- if peak profit reaches `+1%`, floor moves to breakeven
- if peak profit reaches `+2%`, floor moves to `+0.5%`
- if peak profit reaches `+3%`, floor moves to `+1.5%`
- if peak profit reaches `+4%`, floor trails at peak minus `1.5%`

Label when broken:

- `FLOOR_BREACH`

### 3. Scale Outs

Current scale values:

- `FAST4`: at `+4%`, sell `75%`
- `PCT2`: at `+2%`, sell `50%`
- `PCT4_AFTER2`: after `PCT2`, at `+4%`, sell `25%`

### 4. MACD Bear Exits

MACD exits depend on the trade tier.

Tier logic:

- Tier 1 starts at entry
- Tier 2 starts once peak profit reaches `+1%`
- Tier 3 starts once peak profit reaches `+3%`

Exit labels:

- Tier 1: `MACD_BEAR_T1`
- Tier 2: `MACD_BEAR_T2`
- Tier 3: `MACD_BEAR_T3`

## What The Common Blocked Reasons Mean

### `pretrigger warmup`

The bot does not yet have enough recent bars to judge reclaim shape safely.

This should now mostly happen only for truly new symbols, not because of restart.

### `pretrigger reclaim pullback not ready`

The stock did not pull back in the shape the bot expects.

### `pretrigger reclaim touch not ready`

The pullback did not interact with `EMA9` or `VWAP` clearly enough.

### `pretrigger reclaim location not ready`

This is the generic version.
The bot now tries to give more specific location messages where possible.

Examples of clearer live reasons:

- `pretrigger reclaim below VWAP`
- `pretrigger reclaim below VWAP and EMA9 support`
- `pretrigger reclaim too extended from EMA9/VWAP`
- `pretrigger reclaim no fresh anchor touch`
- `pretrigger reclaim recovery candle too weak`
- `pretrigger reclaim single-anchor candle too weak`

### `pretrigger reclaim momentum not ready`

The reclaim bar does not yet show enough strength turning back up.

### `pretrigger reclaim candle not ready`

The reclaim bar shape is weak.

Examples:

- close too low in the bar
- body too small
- upper wick too large

## What To Look At On The Screen

When you check the reclaim bot page, focus on:

- the symbol on watchlist
- recent decisions
- the exact reason a symbol was blocked
- indicator snapshot
- whether price is above or below `EMA9`, `EMA20`, and `VWAP`

The most important quick read is:

- if reclaim is blocked because of `warmup`, that is a setup-history issue
- if reclaim is blocked because of `location`, that is a real setup-quality issue on the current bar

## What This Bot Is Good For

This bot is better for:

- strong momentum names
- names that spike, pull back, and try to go again
- second-leg style entries

This bot is not trying to catch every move.
It is trying to find cleaner second-chance entries.

## Current Practical Reality

This bot is live as a paper bot, not as a real-money bot.

That means:

- it is actively watching scanner-confirmed names
- it can place paper trades
- we are still improving it from live paper behavior

## What We Are Still Improving

The biggest active tuning area is quality, not quantity.

We are mainly trying to:

- reduce bad reclaim trades
- keep the cleaner reclaim entries
- improve the blocked reasons so the screen is easier to understand
- keep the model selective instead of turning it into a high-frequency chaser

## Short Summary

If you want the simplest mental model:

- scanner finds the stock
- reclaim waits for a pullback
- reclaim wants support touch plus recovery
- reclaim wants price back in the right location
- reclaim wants enough momentum to turn back up
- it starts with a smaller starter
- it adds only if follow-through confirms
- if it looks weak, it blocks with a reason
- if it breaks support or fails to confirm, it gets out
