# Live Broker Comparison Sheet - 2026-04-01

Use this sheet during the Wednesday, April 1, 2026 U.S. equities session to
compare Mai Tai fills against the external broker trade by trade.

Primary goal:
- compare each Mai Tai trade against the external broker's corresponding trade
- identify whether differences come from trigger timing, routing/fill quality,
  or strategy-selection drift

Important framing:
- treat this as a controlled validation session
- judge parity per trade, not just by end-of-day P&L
- keep all timestamps in `ET`
- for `30s` trades, be especially careful with entry timing

## Session Header

- Date: `2026-04-01`
- Operator:
- Market session:
- External broker/platform:
- Mai Tai commit:
- Mai Tai deployment checked at:
- External broker clock checked at:
- Notes:

## Pre-Market Readiness

- [ ] Confirm [project-mai-tai.live](https://project-mai-tai.live) is reachable behind basic auth.
- [ ] Confirm `/health` is green.
- [ ] Confirm `/api/overview` shows:
  - `oms_adapter = alpaca_paper`
  - `strategies = 4`
  - `broker_accounts = 3`
- [ ] Confirm no open `virtual_positions`.
- [ ] Confirm no open `account_positions`.
- [ ] Confirm no pending intents.
- [ ] Confirm reconciliation findings are `0`.
- [ ] Confirm scanner can move from `snapshot -> top_confirmed -> watchlist -> subscriptions`.
- [ ] Confirm both Mai Tai and the external broker will be reviewed in `ET`.
- [ ] Confirm how you will pair trades:
  - by symbol + side + nearest timestamp
  - by symbol + side + strategy intent window
- [ ] Confirm whether comparison should use:
  - first fill only
  - average fill
  - full fill lifecycle

Pre-market notes:

## Live Comparison Rules

- Compare each trade one at a time before drawing broader conclusions.
- Capture both the trigger context and the execution result.
- If a trade does not have a clean counterpart on the external broker, mark it
  as `unpaired` instead of forcing a match.
- If a Mai Tai trade looks early or late, capture the chart bar and the exact
  timestamp before judging the strategy.
- If fills differ but trigger time matches, treat it as an execution-quality
  issue first, not a signal-parity issue.

## Quick Status Snapshot

- Mai Tai scanner status:
- Mai Tai watchlist at open:
- Mai Tai active subscription symbols:
- Redis healthy:
- Strategy service healthy:
- OMS healthy:
- Reconciler healthy:
- External broker ready:

## Trade Pairing Checklist

Use this checklist for each trade before filling a comparison row.

- [ ] Symbol matches.
- [ ] Side matches.
- [ ] Trade pairing is confident.
- [ ] Timestamp comparison done in `ET`.
- [ ] Mai Tai strategy identified.
- [ ] Mai Tai entry path identified.
- [ ] Fill basis chosen consistently.
- [ ] Notes captured if the pair is ambiguous.

## Trade Comparison Table

Copy this block for each trade pair during the session.

### Trade 1

- Pair status: `paired | unpaired | needs review`
- Symbol:
- Side:
- Strategy:
- Entry path:
- Mai Tai account:
- External broker account:
- Mai Tai intent time (ET):
- Mai Tai accepted time (ET):
- Mai Tai first fill time (ET):
- Mai Tai final fill time (ET):
- External broker order time (ET):
- External broker first fill time (ET):
- External broker final fill time (ET):
- Mai Tai quantity:
- External broker quantity:
- Mai Tai fill price:
- External broker fill price:
- Fill basis used: `first fill | avg fill | full lifecycle`
- Time delta vs external:
- Price delta vs external:
- Trigger delta classification: `matched | early | late | unclear`
- Execution delta classification: `matched | better | worse | unclear`
- Chart/bar notes:
- Routing/fill notes:
- Verdict: `acceptable | watch | mismatch`

### Trade 2

- Pair status: `paired | unpaired | needs review`
- Symbol:
- Side:
- Strategy:
- Entry path:
- Mai Tai account:
- External broker account:
- Mai Tai intent time (ET):
- Mai Tai accepted time (ET):
- Mai Tai first fill time (ET):
- Mai Tai final fill time (ET):
- External broker order time (ET):
- External broker first fill time (ET):
- External broker final fill time (ET):
- Mai Tai quantity:
- External broker quantity:
- Mai Tai fill price:
- External broker fill price:
- Fill basis used: `first fill | avg fill | full lifecycle`
- Time delta vs external:
- Price delta vs external:
- Trigger delta classification: `matched | early | late | unclear`
- Execution delta classification: `matched | better | worse | unclear`
- Chart/bar notes:
- Routing/fill notes:
- Verdict: `acceptable | watch | mismatch`

### Trade 3

- Pair status: `paired | unpaired | needs review`
- Symbol:
- Side:
- Strategy:
- Entry path:
- Mai Tai account:
- External broker account:
- Mai Tai intent time (ET):
- Mai Tai accepted time (ET):
- Mai Tai first fill time (ET):
- Mai Tai final fill time (ET):
- External broker order time (ET):
- External broker first fill time (ET):
- External broker final fill time (ET):
- Mai Tai quantity:
- External broker quantity:
- Mai Tai fill price:
- External broker fill price:
- Fill basis used: `first fill | avg fill | full lifecycle`
- Time delta vs external:
- Price delta vs external:
- Trigger delta classification: `matched | early | late | unclear`
- Execution delta classification: `matched | better | worse | unclear`
- Chart/bar notes:
- Routing/fill notes:
- Verdict: `acceptable | watch | mismatch`

### Trade 4

- Pair status: `paired | unpaired | needs review`
- Symbol:
- Side:
- Strategy:
- Entry path:
- Mai Tai account:
- External broker account:
- Mai Tai intent time (ET):
- Mai Tai accepted time (ET):
- Mai Tai first fill time (ET):
- Mai Tai final fill time (ET):
- External broker order time (ET):
- External broker first fill time (ET):
- External broker final fill time (ET):
- Mai Tai quantity:
- External broker quantity:
- Mai Tai fill price:
- External broker fill price:
- Fill basis used: `first fill | avg fill | full lifecycle`
- Time delta vs external:
- Price delta vs external:
- Trigger delta classification: `matched | early | late | unclear`
- Execution delta classification: `matched | better | worse | unclear`
- Chart/bar notes:
- Routing/fill notes:
- Verdict: `acceptable | watch | mismatch`

### Trade 5

- Pair status: `paired | unpaired | needs review`
- Symbol:
- Side:
- Strategy:
- Entry path:
- Mai Tai account:
- External broker account:
- Mai Tai intent time (ET):
- Mai Tai accepted time (ET):
- Mai Tai first fill time (ET):
- Mai Tai final fill time (ET):
- External broker order time (ET):
- External broker first fill time (ET):
- External broker final fill time (ET):
- Mai Tai quantity:
- External broker quantity:
- Mai Tai fill price:
- External broker fill price:
- Fill basis used: `first fill | avg fill | full lifecycle`
- Time delta vs external:
- Price delta vs external:
- Trigger delta classification: `matched | early | late | unclear`
- Execution delta classification: `matched | better | worse | unclear`
- Chart/bar notes:
- Routing/fill notes:
- Verdict: `acceptable | watch | mismatch`

## Classification Guide

Use these labels consistently.

- `matched`
  - timing or price difference is small enough that it does not change the
    practical trade decision
- `early`
  - Mai Tai entered before the external broker in a way that may reflect signal
    or bar-timing drift
- `late`
  - Mai Tai entered after the external broker in a way that may reflect signal
    lag or delayed qualification
- `better`
  - Mai Tai fill quality is better than the external broker's fill for the same
    trigger window
- `worse`
  - Mai Tai fill quality is worse than the external broker's fill for the same
    trigger window
- `unclear`
  - pairing is weak, partial fills make comparison noisy, or chart evidence is
    missing

## Escalation Triggers

Pause and investigate if any of these happen.

- [ ] Two or more `30s` entries look materially early.
- [ ] Multiple trades are `unpaired` because strategy selection clearly diverged.
- [ ] Mai Tai fill timing matches but fill price is repeatedly much worse.
- [ ] Reconciliation findings open after fills.
- [ ] Ghost positions or pending close state appear.
- [ ] Active subscriptions fail to follow the watchlist.
- [ ] Redis or service health degrades during the session.

## Midday Summary

- Total paired trades:
- Acceptable:
- Watch:
- Mismatch:
- Main issue category so far:
  - `signal timing`
  - `bar parity`
  - `execution quality`
  - `strategy selection`
  - `pairing unclear`
- Notes:

## End-Of-Day Assessment

- Total Mai Tai trades reviewed:
- Total external broker trades reviewed:
- Total clean pairs:
- Main symbols reviewed:
- Main strategies reviewed:
- Did `macd_30s` look ready for stricter parity judgment?
- Did `macd_1m` look ready for stricter parity judgment?
- Did `tos` look ready for stricter parity judgment?
- Did `runner` participate meaningfully?

Final assessment:
- Ready for continued live side-by-side comparison:
- Biggest blocker:
- Highest-confidence parity area:
- Lowest-confidence parity area:
- Follow-up for next session:
