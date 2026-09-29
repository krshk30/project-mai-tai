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
| 45% breakout body exit | At the modeled fill, close when abs(close-open)/(high-low) <45% | At the close of the Schwab bar containing the actual parent fill, if still held; exactly45 holds | Intentional operator-confirmed timing/source change from paper |
| ATR exit | Shared ATR 5/3.5/Wilder's; gateway raw OHLC; SELL flip after entry | Same math on completed Schwab OHLC from 07:00; SELL on a bar strictly later than the break bar | Operator-approved source/timing, not signal parity |
| Profit and loss exits | Bid-modeled +5% target and -8% stop from intended level | Native OTOCO +5% / -8% from rounded trigger | Retained percentages; rounding/execution can differ |
| Simultaneous exit reasons | Body, then ATR, then target/stop in paper quote evaluation | Body before ATR in strategy; native children can win any broker race | Native execution cannot promise paper priority |
| 10:00 cutoff | No new modeled entries; selected window-flatten OFF | Cancel unfilled buys, keep held shares under exit management | Retained |
| Residual position | No selected forced window flatten | Guarded 15:55 close before regular 16:00 end | Operator-approved addition |
| ATR/v2 collision | No live account interaction | Same-account same-symbol ownership/order gate in OMS | Operator-approved addition |
| Optional ATR cyan-entry / four-red delay | OFF in the inspected paper process | Not implemented; refuse startup if either is enabled rather than silently omit it | No active rule removed |
| Classic width/volume/VWAP/EMA filters; reclaim model | Inactive in selected fixed-resting mode | Not imported into this route | Not active rules; separate design required to add |

## Evidence and safety details

- The body uses the persisted completed Schwab candle, not any gateway tick prefix. The
  100-share filter affects the breakout high only. First execution time comes from Schwab
  execution legs, never parent enteredTime, a price cross, or the report-arrival clock.
- Wait for the break bar to close. Normal 0-3 second persistence delay is pending, not an
  incident; after that, absent evidence opens an incident and leaves the native bracket intact.
  Saved completed-bar evidence survives producer restarts. The reviewer's 921-bar timing
  sample is supplied evidence, not a new production measurement in this revision.
- ATR reads only strategy_bar_history for schwab_1m_v2, interval60, live/rest provenance,
  complete minutes since 07:00 ET. No close-only synthetic OHLC, forming minute, or gateway
  fallback. Same-day initialization differs from paper's gateway history; no parity claim.
  Missing latest minute or insufficient Wilder seed is UNKNOWN. Internal illiquid gaps retain
  the unchanged paper helper's gap treatment. This PR does not subscribe ORB names to Schwab.
- A missing break bar means no strategy sell, even if a later ATR flip exists; keep protection
  and report the evidence gap. A known small body can exit without sufficient ATR seed.
  Native target/stop fills take priority, including a fill before the local position catches up.
- MACD calls V2Indicators.macd on the full available 07:00+ Schwab series, not a freshly seeded
  last-26 slice. At least 35 bars and the latest 35 consecutive minutes are required. Earlier
  sparse minutes retain v2's observed-series math. Reviewer-supplied 32/34 coverage of 26 bars
  does NOT prove this stronger seed/coverage prerequisite. The IMCC-shaped test is synthetic.
- v2 entries no longer acquire an ORB broker-position read or refuse v2's own adds. Only ORB-
  owned working buys/positions collide with v2; the existing post-submit reconciliation is
  unchanged. Flag OFF adds neither this ownership gate nor an advisory lock.
- An accepted replacement receipt persists the NEW ID even if the following read fails,
  shows a partial fill, or cannot verify shape/prices. It is explicitly UNKNOWN, not confirmed
  working. Durable HOLD blocks cancel/reprice; ordinary reconciliation reads the new handle
  and records actual fills. It does not automatically clear HOLD on a mere status read.
  No receipt/ID after transport uncertainty means manual broker reconciliation, never retry.
- The pager query now includes ORB replacement, exit-evidence and EOD incidents with a specific
  message. This changes only routing for these new sources, not other sources' closure rules.
  Install requires separately copying ops/health/unexercised_watch.py to the sha-guarded
  /home/trader/unexercised_watch/watch.py and updating BOTH cron sha guards; merging is not
  installation. Mocked delivery tests are not proof that the operator's phone received a page.
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
