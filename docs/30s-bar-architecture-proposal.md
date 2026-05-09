# 30s Schwab Bar Architecture Proposal

## Why this exists

The recent `macd_30s` drift investigation showed that we have two separate questions mixed together:

1. Are we closing and persisting bars correctly inside our runtime?
2. Are we using the right Schwab source to build canonical 30-second bars in the first place?

The first question produced real bugs and real fixes:

- subscription continuity
- live-bar and tick-source mixing
- synthetic quiet-bar replacement
- close-grace mismatch
- synthetic gap-fill persistence

Those fixes were necessary. They do not fully settle the second question.

## Current local architecture

Today, our Schwab-native 30-second bars are built from `LEVELONE_EQUITIES` updates, not from an official 30-second Schwab candle feed:

- [src/project_mai_tai/market_data/schwab_streamer.py](C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\src\project_mai_tai\market_data\schwab_streamer.py)
  - `LEVELONE_EQUITIES_FIELDS = "0,1,2,3,4,5,8,9,35"`
  - trade extraction uses:
    - `3` last price
    - `8` cumulative volume
    - `9` last size
    - `35` trade time millis
- [src/project_mai_tai/strategy_core/schwab_native_30s.py](C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\src\project_mai_tai\strategy_core\schwab_native_30s.py)
  - `SchwabNativeBarBuilder.on_trade(...)` aggregates custom 30s bars from those updates
  - `_resolve_volume_delta(...)` reconstructs per-update bar volume from cumulative-volume deltas
  - `fill_gap_bars` can synthesize quiet bars
  - `close_grace_seconds` delays bar closure to catch late same-bucket prints
- [src/project_mai_tai/services/strategy_engine_app.py](C:\Users\kkvkr\OneDrive\Documents\GitHub\project-mai-tai\src\project_mai_tai\services\strategy_engine_app.py)
  - `macd_30s` uses `SchwabNativeBarBuilderManager(interval_secs=30, close_grace_seconds=..., fill_gap_bars=False)`
  - completed bars are persisted directly into `StrategyBarHistory`
  - `hydrate_historical_bars(...)` seeds runtime memory; it does not rebuild persisted history

This means our persisted `macd_30s` history is currently acting as both:

- runtime trading state
- canonical historical record

That coupling is the main design risk.

## What Schwab and community clients suggest

The available streaming primitives look split by purpose:

- `LEVELONE_EQUITIES`
  - quote and last-trade style updates
- `CHART_EQUITY`
  - official one-minute OHLCV bars
- `TIMESALE_EQUITY`
  - time-and-sales stream

Useful references:

- [schwab-py streaming docs](https://schwab-py.readthedocs.io/en/stable/streaming.html)
  - documents minute `chart_equity_subs(...)`
  - documents `LevelOneEquityFields` including `LAST_PRICE`, `TOTAL_VOLUME`, `LAST_SIZE`, `TRADE_TIME_MILLIS`
- [TD Ameritrade stream schema mirror](https://kaizhu256.github.io/tdameritrade-dev-mirror/developer.tdameritrade.com/content/streaming-data.html)
  - `CHART_EQUITY`: chart candle for equity, `All Sequence`
  - `QUOTE`: level 1 equity, `Change`
  - `TIMESALE_EQUITY`: time and sale for equity, `All Sequence`
- [dhonn/schwab-python-api](https://github.com/dhonn/schwab-python-api)
  - community examples stream `CHART_EQUITY` for minute candles and `level_one_quotes` separately
- [slimandslam/schwab-client-js developer reference](https://github.com/slimandslam/schwab-client-js/blob/main/docs/DeveloperReference.md)
  - shows supported service names including `LEVELONE_EQUITIES` and `CHART_EQUITY`

The common pattern is:

- use chart streams when you want official minute candles
- use quote streams for live quote state
- use time-and-sales if you need custom sub-minute trade-built bars

What is not common is treating level-one quote updates as canonical trade tape for persisted 30-second history.

## Recommended target architecture

### 1. Split canonical bars from runtime continuity bars

Persist one canonical bar series per symbol and interval, and keep any synthetic continuity layer separate.

Canonical bars should be:

- built only from observed market data
- never created just to keep indicators continuous
- safe to compare against archive rebuilds and external references

Continuity bars should be:

- runtime-only by default
- or persisted to a separate derived-history store if we need restart continuity
- clearly marked as synthetic and excluded from canonical validation

Practical rule:

- `StrategyBarHistory` should not store synthetic gap bars for canonical Schwab 30s validation.

### 2. Separate source-of-truth from strategy-consumption format

We should treat the market-data source and the strategy-ready series as different layers:

1. raw source events
2. canonical observed bars
3. derived strategy bars

Suggested meaning:

- raw source events
  - `LEVELONE_EQUITIES`, `CHART_EQUITY`, and eventually `TIMESALE_EQUITY` if available
- canonical observed bars
  - persisted bars built only from observed events
- derived strategy bars
  - any synthetic fill, indicator continuity, restart smoothing, or fallback overlay the strategy needs

That lets us answer:

- "what did Schwab actually send us?"
- separately from
- "what series did the strategy consume to keep indicators stable?"

### 3. Prefer time-and-sales for canonical 30s if available

If `TIMESALE_EQUITY` is available and usable in our stack, it is the best candidate for canonical 30-second bar construction.

Why:

- it is the closest stream to a trade tape
- it avoids reconstructing trade volume from quote-style updates
- it reduces ambiguity around last-size and cumulative-volume semantics

If we adopt time-and-sales:

- canonical 30s persistence should come from time-and-sales aggregation
- level-one quote updates should still feed live price state and health checks
- `CHART_EQUITY` 1m can remain a sanity/control stream

### 4. If time-and-sales is not available, downgrade the claim on canonical accuracy

If we must stay on `LEVELONE_EQUITIES`, we should treat those 30s bars as quote-derived bars, not exact trade-tape bars.

That means:

- OHLC may still be useful
- volume and trade-count may be approximate
- validation should classify:
  - price-structure mismatches
  - volume-only mismatches
  - missing-bar mismatches

In that world, a clean result means:

- no missing persisted bars
- no synthetic canonical bars
- stable OHLC and close alignment

It does not necessarily mean:

- exact parity with a true trade tape on every volume field

### 5. Add provenance to persisted canonical bars

If we keep using `StrategyBarHistory` as the persisted store, we should eventually track how each bar was produced.

Minimum useful provenance:

- source type
  - `timesale`
  - `levelone`
  - `chart_equity`
  - `synthetic`
- finality
  - observed final
  - runtime derived
- restart epoch or session seed marker

That would make later audits much easier, especially around restart boundaries.

## Short implementation plan

### Phase 1

- Keep `fill_gap_bars=False` for canonical `macd_30s` persistence.
- Keep `close_grace_seconds` enabled for live same-bucket trade capture.
- Continue rebuilt-vs-persisted validation on live morning names.
- Add mismatch classification to emphasize:
  - missing bars
  - OHLC drift
  - volume-only drift
  - trade-count drift

### Phase 2

- Prototype a `TIMESALE_EQUITY` capture path for one symbol.
- Persist raw time-and-sales samples or a compact archive for replay.
- Compare:
  - time-and-sales-built 30s
  - current level-one-built 30s
  - persisted `StrategyBarHistory`

### Phase 3

- Introduce explicit canonical-vs-derived separation in persistence.
- Keep strategy continuity bars out of canonical history.
- Let the runtime still consume derived continuity bars if indicators need them.

## Recommendation for the next coding pass

The next meaningful research or implementation step should be:

1. verify whether our Schwab connection can subscribe to `TIMESALE_EQUITY`
2. wire a narrow capture experiment for one or two active morning names
3. compare that output to our current `LEVELONE_EQUITIES` 30s builder

If `TIMESALE_EQUITY` is unavailable or unreliable, we should explicitly redefine the current Schwab 30s persisted series as quote-derived and stop expecting perfect tape-level volume parity from it.
