# ORB paper versus the new Schwab route

This comparison is for the **running-high + fixed-resting paper mode actually selected**, not
the inactive classic or reclaim modes. No claim of equal fills or equal profit is made.

Read-only production inspection on September 29 found paper PID 2051823, started September 16,
with running-high, resting-entry, paper lifecycle, and ATR exit ON; reclaim and window-flatten
OFF. Its environment set target 5%, stop 8%, body minimum 45%, quantity 2. The optional ATR
entry gate and four-red delay were absent and default OFF in code. Inspection did not change
production. The running paper process has not received this branch's high-size filter.

| Rule | Selected paper behavior | New Schwab route | Verdict |
| --- | --- | --- | --- |
| Scanner universe | Names confirmed by 09:25 ET | Same universe reader | Retained |
| Opening range | 09:25-09:29 reference; full-range software rest, then possible adjustment from the 09:30 bar | Place after bar 3, raise the same parent after bars 4 and 5; last source bar is 09:29, not an extra 09:30 raise | Operator-approved staging change |
| Breakout high | Original running process allowed small-print highs | Only prints of at least 100 shares set the high; shared aggregator fix also applies to paper after deployment | Explicit high-filter correction; other OHLC unfiltered |
| Actual order size | Two modeled shares | Exactly two shares on Schwab | Retained size; broker execution replaces simulation |
| First entry | One modeled entry per symbol | One native buy attempt; no fresh OPEN after a cancel/reject/uncertain replace | Deliberately stricter one-attempt rule |
| Entry timing | 09:30-10:00 software cross | NORMAL/DAY native parent can fill from 09:30; cancel remaining buys at 10:00 | Same intended window; cancellation can race a fill |
| Chase/price | Trade strictly above level models a fill at the level, even if the print was higher | Native stop trigger and +0.5% maximum buy price; price can gap past it without filling | Native execution difference, never a guaranteed fill |
| MACD entry check | Not part of this selected paper mode | Previous completed Schwab minute, MACD histogram >=0; unknown/negative cancels unfilled buy | Operator-approved addition |
| 45% breakout body exit | At the modeled fill, close when abs(close-open)/(high-low) <45% | Same shared formula using gateway trade prefix at Schwab's first execution timestamp; exactly45 allowed | Restored; actual fill evidence required |
| ATR exit | Shared ATR 5/3.5/Wilder's; gateway raw OHLC; SELL flip after entry | Same math; completed Schwab OHLC from 07:00 onward; SELL flip closes at/after the completed minute boundary | Restored with operator-approved source change, not signal parity |
| Profit and loss exits | Bid-modeled +5% target and -8% stop from intended level | Native OTOCO +5% / -8% from rounded trigger | Retained percentages; rounding/execution can differ |
| Simultaneous exit reasons | Body, then ATR, then target/stop in paper quote evaluation | Body before ATR in strategy; native children can win any broker race | Native execution cannot promise paper priority |
| 10:00 cutoff | No new modeled entries; selected window-flatten OFF | Cancel unfilled buys, keep held shares under exit management | Retained |
| Residual position | No selected forced window flatten | Guarded 15:55 close before regular 16:00 end | Operator-approved addition |
| ATR/v2 collision | No live account interaction | Same-account same-symbol ownership/order gate in OMS | Operator-approved addition |
| Optional ATR cyan-entry / four-red delay | OFF in the inspected paper process | Not implemented; refuse startup if either is enabled rather than silently omit it | No active rule removed |
| Classic width/volume/VWAP/EMA filters; reclaim model | Inactive in selected fixed-resting mode | Not imported into this route | Not active rules; separate design required to add |

## Evidence and safety details

- The body calculation includes all gateway prices, including smaller trades. The 100-share
  filter affects the breakout high only. First execution time comes from Schwab execution legs,
  never parent enteredTime, a quote-cross time, or the report-arrival clock.
- Keep a bounded two-minute / 20,000-tick buffer per symbol, requiring observation from before
  the fill minute and tape reaching the execution timestamp. Missing/truncated history is
  UNKNOWN. Save the body evidence on the owned entry so a producer restart does not recalculate
  it from later prices. Late/out-of-order provider delivery and production timestamp coverage
  still require first-session observation; this is not a perfect exchange-tape reconstruction.
- ATR reads only strategy_bar_history for schwab_1m_v2, interval60, live/rest provenance,
  complete minutes since 07:00 ET. No close-only synthetic OHLC, forming minute, or gateway
  fallback. Same-day initialization differs from paper's gateway history; no parity claim.
  Missing latest minute or insufficient Wilder seed is UNKNOWN. Internal illiquid gaps retain
  the unchanged paper helper's gap treatment. This PR does not subscribe ORB names to Schwab.
- An unknown body does not suppress an independently proven ATR exit, and missing ATR does not
  suppress a proven body exit. Missing evidence opens a durable incident, without cancelling
  protection merely because data is unavailable. Incident phone delivery remains unproven.
- Producer persists evidence before publishing a deterministic entry/fill/reason intent. OMS
  verifies the exact entry and fill and recomputes the rule, requires a fresh post-decision bid,
  then shares the EOD durable close claim. A stale queued exit from an older trip cannot close
  today's position. A new API POST is never authorized by an uncertain earlier POST.
- Early strategy closes currently defer while the parent is partially filled, keeping native
  protection intact. They resume only on a fully filled parent. A terminal partial buy needs
  manual handling; this is an activation-review limitation, not completed paper/live parity.
- Native children remain broker-managed regardless of producer health. Confirmed release of
  the pair precedes a fresh broker remaining-quantity read and one market sell. Sell rejection
  after release creates a durable incident; it does not manufacture protection or a fill.
- Observation records hypothetical entry/exit decisions only. It never writes fill evidence,
  broker orders, OMS intents, or gateway subscriptions. Actual orders and P&L stay UNMEASURED.

Code compared: services/orb_app.py, orb_paper_lifecycle.py, strategy_core/orb_intrabar.py,
services/orb_schwab_app.py, orb_schwab_exits.py, oms/orb_schwab_eod.py, and the isolated paper
environment generator ops/systemd/build_orb_paper_env.sh. v2 strategy and Webull rules unchanged.
