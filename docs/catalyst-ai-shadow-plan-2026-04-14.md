# Catalyst AI Shadow Plan

## Goal

Improve `Path A` news handling without changing live scanner behavior during market hours.

The immediate problem is not that `Path A` and `Path B` are designed badly. The bigger issue is that some fast movers arrive with:

- late provider headlines
- wording the current rule engine does not recognize
- generic roundup coverage before the company-specific article shows up

This local-only change adds an **AI shadow evaluator** beside the existing rule engine.

## What The New Layer Does

For each ticker with available catalyst articles:

1. The current rule engine still runs first.
2. The new AI shadow evaluator reviews up to a small set of recent articles.
3. It returns:
   - `direction`
   - `category`
   - `confidence`
   - `has_real_catalyst`
   - `is_generic_roundup`
   - `is_company_specific`
   - `path_a_eligible`
   - `reason`
   - `headline_basis`
   - `positive_phrases`
4. The result is stored as **shadow metadata** on the scanner row.

## Safety Model

Default mode is **shadow only**.

That means:

- live `Path A` still uses the current deterministic rule engine
- AI does not change scanner confirmation by default
- scanner UI can show both:
  - rule-engine result
  - AI shadow interpretation

There is also a future `promote` mode, but it is still disabled by default and should not be turned on until we validate enough live cases.

## New Settings

Added local settings:

- `MAI_TAI_NEWS_AI_SHADOW_ENABLED`
- `MAI_TAI_NEWS_AI_PROMOTE_ENABLED`
- `MAI_TAI_NEWS_AI_PROVIDER`
- `MAI_TAI_NEWS_AI_API_KEY`
- `MAI_TAI_NEWS_AI_MODEL`
- `MAI_TAI_NEWS_AI_BASE_URL`
- `MAI_TAI_NEWS_AI_REQUEST_TIMEOUT_SECONDS`
- `MAI_TAI_NEWS_AI_MAX_ARTICLES`
- `MAI_TAI_NEWS_AI_MAX_SUMMARY_CHARS`

Recommended first rollout:

- shadow enabled
- promote disabled
- small model
- short timeout
- max 3 articles

## Expected Scanner Readout

The catalyst cell can now show an extra AI shadow line such as:

- `AI shadow: bullish · DEAL/CONTRACT · 91% · PATH A ready · openai/gpt-4.1-mini`

Plus a short explanation, for example:

- `AI sees a fresh company-specific partnership catalyst.`

This gives us immediate miss diagnostics without changing live trading behavior.

## How We Should Validate It

Use live movers like:

- `ROLR`
- `BTBD`
- future fast small-cap news runners

For each one, compare:

1. official PR timing
2. Alpaca/Benzinga timing
3. rule-engine Path A result
4. AI shadow verdict
5. actual Path B fallback timing

## What To Decide Later

After enough shadow examples, decide one of these:

1. Keep AI as diagnostics only
2. Let AI promote only narrow categories like clear deal/contract or merger catalysts
3. Blend AI with a better primary source later, such as direct Benzinga or a press-release feed

## Current Status

- implemented locally
- not deployed
- safe for after-hours review only
