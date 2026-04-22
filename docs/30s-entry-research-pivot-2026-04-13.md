# 30s Entry Research Pivot (2026-04-13)

This note captures the external research pass done with Perplexity and OpenAI after the reclaim/location sweeps.

## API Validation

- Perplexity API access validated successfully.
- OpenAI Platform API access validated successfully.

Research artifacts:

- [perplexity_research_30s.txt](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/perplexity_research_30s.txt)
- [openai_research_30s.txt](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/openai_research_30s.txt)

## Main Takeaway

The external research agrees with the replay evidence:

- broad reclaim loosening increases bad trades
- location relaxation is not fixing the core problem
- the goal should be fewer, cleaner entries, not more reclaim attempts

The likely issue is not just reclaim tuning. It is setup shape.

## Most Promising Alternative 30s Entry Shapes

1. Breakout retest
   First clean retest of a key level after breakout, then enter only on confirmed hold or next-bar break.

2. Micro-range compression break
   Tight 2-5 bar pause after the initial surge, then enter only on a strong break with volume.

3. VWAP pinch and go
   Price compresses around VWAP, then expands with momentum and volume.

4. Volume ignition / HOD continuation
   New high-of-day break only when volume expands sharply and the move is not already overextended.

## What This Means For Mai Tai

For the current small-cap runner universe, reclaim should stay frozen as a research baseline.

The next bot logic should probably not be:

- "more permissive reclaim"

It should more likely be:

- "first breakout retest with confirmation"
or
- "post-impulse micro-compression continuation"

## Filter Design Guidance

Structural filters:

- key level exists
- strong runner context exists
- setup occurs in the preferred time window
- trend context is still intact

Confirmational filters:

- strong candle quality
- volume expansion or relative-volume threshold
- next-bar hold/break confirmation
- no obvious overhead failure shape

Risk-only filters:

- stop placement
- size
- daily loss cap
- fail-fast exit handling

Important principle:

- risk controls should not define the setup
- setup shape should define the setup

## Practical Recommendation

Next implementation research should pivot to a new selective 30s archetype:

### Recommended first archetype

`30s breakout retest`

High-level idea:

- identify a breakout through a clear intraday reference level
- require a shallow retest or hold
- do not buy the first reclaim touch blindly
- only buy if the retest bar is clean or the next bar confirms through the reclaim high

Why this is the best next move:

- it matches the user goal of fewer trades with better quality
- it naturally reduces random reclaim entries
- it uses setup definition rather than location loosening
- it can reuse much of the current armed-break infrastructure

## Recommended Next Work

1. Keep `macd_30s_reclaim` frozen as the current research baseline.
2. Do not widen reclaim further for now.
3. Build a new research path around breakout-retest entry logic.
4. Compare it against reclaim on the same combined replay universe.
5. Judge success by win rate and cleanliness first, not trade count.

## Implementation Started

The first implementation slice for this pivot is now in the codebase:

- a new research-only 30s bot path: `macd_30s_retest`
- separate config, settings, and runtime wiring
- conservative entry shape:
  - identify breakout history
  - wait for a clean retest bar
  - arm the setup instead of buying immediately
  - buy only if the next bar confirms through the retest high

Current status:

- unit-tested at the entry-engine level
- runtime/service wiring added
- replay comparison harness updated to recognize the new variant

Not done yet:

- broad replay evaluation on the combined universe
- threshold tuning for the retest path
- decision on whether retest is better than reclaim for the current runner universe

## First Replay Result

The first broad replay pass has now been run across the combined universe:

- original April 1-2 recovered set
- April 8 top gainers

Compared variants:

- `macd_30s`
- `macd_30s_reclaim`
- `macd_30s_retest`

Result summary:

- `macd_30s`
  - `15 good`
  - `33 bad`
  - resolved good rate `0.3125`

- `macd_30s_reclaim`
  - `22 good`
  - `19 bad`
  - resolved good rate `0.5366`

- `macd_30s_retest`
  - `0 good`
  - `4 bad`
  - resolved good rate `0.0000`

What this means:

- the retest bot is currently very selective
- but it is too strict and not productive enough yet
- frozen reclaim is still stronger than the first retest implementation

Top retest blockers in the first replay pass:

- `pretrigger retest breakout not ready`
- `pretrigger retest touch not ready`
- `pretrigger retest hold not ready`
- `pretrigger retest candle not ready`

## Updated Recommendation

Keep the retest pivot, but do not replace reclaim.

Best next step:

1. keep reclaim frozen as the strongest current research baseline
2. tune retest carefully
3. improve retest setup recognition without opening the floodgates
4. judge the next retest version by:
   - win rate
   - bad trade count
   - per-symbol cleanliness

## Retest Tuning Update

Deep research plus the first targeted replay sweep pointed to one cleaner retest direction:

- widen breakout context modestly
- use a slightly wider retest zone
- allow a slightly deeper pullback from breakout
- do not soften candle rules yet

That became the new retest research baseline:

- `pretrigger_retest_breakout_window_bars = 6`
- `pretrigger_retest_min_breakout_pct = 0.0025`
- `pretrigger_retest_max_pullback_from_breakout_pct = 0.04`
- `pretrigger_retest_level_tolerance_pct = 0.005`

Why this baseline was chosen:

- widening breakout context alone did nothing
- widening the retest zone finally made retest start producing good trades
- softer candle rules added more bad trades than we want for this path

So the retest goal stays the same:

- fewer trades than reclaim
- cleaner trades than reclaim
- enough activity to matter on a small daily stock list

## Combined-Set Research Comparison

The cleaner family comparison now uses the same combined universe but disables ticker-loss pause inside the research harness, so the result reflects setup quality instead of replay throttling.

Reference output:

- [compare_30s_research_family_nopause.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/compare_30s_research_family_nopause.json)

Result summary:

- `macd_30s_reclaim`
  - `52 good`
  - `65 bad`
  - resolved good rate `0.4444`

- `macd_30s_retest`
  - `1 good`
  - `5 bad`
  - resolved good rate `0.1667`

What this means:

- frozen reclaim is still the correct benchmark
- retest is alive as a research path, but it is still materially weaker than reclaim
- the next retest work should not be more zone loosening
- the next retest work should be a better breakout-context model

The dominant retest blockers on the combined set are still:

- `pretrigger retest breakout not ready`
- `pretrigger retest touch not ready`
- `pretrigger retest hold not ready`
- `pretrigger retest candle not ready`

That points to setup recognition, not trade management, as the next retest bottleneck.

## Breakout-Context Retest Pass

The next retest tuning pass changed only breakout recognition:

- require the chosen breakout bar to be bullish
- require it to break the level by high, not only by close
- require a strong close position on the breakout bar
- choose the strongest breakout candidate in the breakout window instead of the first qualifying bar

Validation passed:

- [test_decision_layer.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_decision_layer.py)
- [test_strategy_engine_service.py](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tests/unit/test_strategy_engine_service.py)

Retest-only combined-set replay:

- [compare_30s_retest_nopause.json](/C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/tmp_replay/compare_30s_retest_nopause.json)

Result:

- `1 good`
- `4 bad`
- `1 open`

Interpretation:

- this version is slightly cleaner than the prior retest pass
- but it is still not strong enough to challenge reclaim
- breakout context is better framed now, but retest still needs a different next lever

Most likely next lever:

- retest support/hold modeling
- specifically, how we define a valid retest hold after the breakout
