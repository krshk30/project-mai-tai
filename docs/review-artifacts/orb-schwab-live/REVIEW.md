# ORB Schwab: Simple Review Guide

## What this change does

It adds a separate ORB route for Schwab, with **two shares per trade**. It does not turn
the existing paper bot into a live bot. **Live order sending is OFF by default.**

The next step is review, followed by an after-hours installation with live sending still OFF.
Then we observe a morning before deciding whether to activate real orders. Nothing in this
document authorizes activation automatically.

## The trading rules

| Question | Rule in this build |
| --- | --- |
| Which stocks? | Scanner-confirmed names available by 09:25 ET. Later confirmations are excluded. |
| Where is the breakout price? | The highest qualifying trade in the 09:25-09:29 minute bars. Trades smaller than 100 shares do not set this high. These bars come from the existing gateway feed. |
| When is the order prepared? | After three completed bars, around 09:28 ET. A regular-session Schwab stop-limit buy is prepared ahead of 09:30, not a pre-market buy. |
| What happens after bars four and five? | Raise the same resting order if the high increased. Do not lower it or send a second buy. Only a confirmed, unfilled parent may be replaced. |
| What is the buy limit? | The trigger is rounded up to a valid price tick. The limit is capped at 0.5% above the breakout level, rounded down. A crossing does not guarantee a fill. |
| What does negative MACD mean here? | The **MACD histogram** must be zero or positive on the latest completed Schwab one-minute bar. Negative, missing or stale data blocks entry or requests cancellation of an unfilled buy. This does not use the forming bar. |
| Can ATR and ORB both buy the same stock? | The live OMS checks for conflicting orders or holdings in the same Schwab account and blocks another entry. Observation alone does not prove this broker check. |
| How are exits handled? | Native paired sell orders: target +5%, protective stop -8%, measured from the rounded buy trigger, not the eventual fill. Execution prices are not guaranteed. |
| What happens at 10:00? | Cancel unfilled buys. Filled shares remain under their exit management; 10:00 does not force a sale. |
| What if shares remain near the close? | On a normal full session, attempt a close from 15:55, after confirming the existing sell pair is released and rereading the remaining shares. An uncertain result cannot cause a duplicate sell. No new market close is sent at/after 16:00. |

A cancellation can race a fill. The live route must use Schwab's confirmed order state;
it cannot assume that a cancellation request prevented the purchase.

## The flag-OFF morning test

There is a separate **observation switch**. With observation ON and live sending OFF, the
new route records the same proposed breakout orders, reprices and MACD decisions. It reads
existing feeds without adding gateway subscriptions, and sends **no broker orders or OMS
order instructions**. Missing feed coverage is reported, not treated as a successful test.

We will check:

- The exact bars, qualifying highs, proposed buy prices and two-share bracket prices.
- Placement after bar three and upward repricing after bars four and five.
- Completed-bar MACD decisions, data gaps, price crossings, and the 10:00 cutoff.
- Candidate counts and missing minutes, including cases where no order would be proposed.

**This is not a fill simulator.** A crossing is not a Schwab fill, and there is no claimed
live profit/loss. After the open, cancellation observations mean "cancel if still unfilled";
without a broker order we cannot know whether it would already have filled.

## Review and rollout

1. Independently review this branch, its safety tests, and the exact version to merge.
2. Install the reviewed version after hours with live sending OFF and observation ON.
   Verify the running settings. Do not switch on live sending as part of this installation.
3. Observe the next morning, intended for September 30 if installation is ready in time.
   Report actual evidence; no signals or missing data do not count as validation.
4. Separately complete the attended Schwab place/reprice/cancel test discussed earlier.
   This uses a real broker order and therefore needs its own controlled test approval.
5. Review both sets of evidence. Real trading can be enabled after hours only after the
   remaining safety items are resolved and the exact version receives activation approval.
   Tomorrow evening is a decision point, not a promised activation.

## Still required before live activation

Schwab order acceptance/replacement/cancellation, production Schwab-bar coverage, full-suite
baseline comparison, and independent review remain to be verified. Early-close sessions and
operator notification for an unresolved end-of-day close also need to be covered before live
activation. The current close fallback uses the normal 16:00 session end and records an OMS
incident; phone delivery for that incident is not yet proven.

The new setting names are `orb_schwab_observe_enabled` and
`orb_live_schwab_orders_enabled`. Both default OFF; the producer refuses to start with both ON.
The technical implementation and test record are in [STATUS.md](STATUS.md).
