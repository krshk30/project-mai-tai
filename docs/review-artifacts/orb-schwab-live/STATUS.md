# ORB Schwab live route: review-only build

This branch is not approved for deployment. `orb_live_schwab_orders_enabled` defaults to false;
the separate `orb-schwab` unit is not installed or part of the production target. The existing
`orb` paper observer remains broker-disconnected.

Start with [the simple review guide](REVIEW.md) for the rules and proposed rollout.
The [rule comparison](RULE_COMPARISON.md) covers the selected paper mode and every intentional
native-route difference, including remaining activation limitations.

## Implemented locally

- One two-share NORMAL/DAY Schwab STOP_LIMIT parent with native +5% / -8% OTOCO children.
- Place after the completed 09:27 bar (three 09:25-09:27 bars), around 09:28 ET. Request an
  upward-only replacement after the 09:28 and 09:29 bars, never a second OPEN order.
- Full 07:00+ Schwab series MACD uses V2Indicators.macd (minimum 35 bars; latest 35 consecutive).
  The last completed histogram must be nonnegative; unknown data blocks entry, and a later
  negative/unknown reading requests cancellation of an unfilled parent. OMS also checks the
  condition and cancels working parents at the 10:00 ET entry cutoff.
- ORB checks broker positions and its own ORB/v2 orders before a new buy. v2 itself only checks
  ORB-owned orders/positions, not an added broker-position gate that would block v2's own adds.
  The replacement's NEW broker ID is retained even if confirmation fails. Such a receipt is
  not a confirmed working bracket; durable HOLD blocks cancel/reprice until reconciled. Exact
  fills are still polled from the new ID. Unknown replacement, exit-evidence and EOD incidents
  are included in the pager query; installation and phone delivery remain unexercised.
- The OMS polls the exact owned Schwab child sell for fill attribution, not an account-wide
  symbol match.
- Operator card supersedes the earlier fill-time body implementation: body <45% at the CLOSE
  of the completed Schwab minute containing the actual parent fill; >=45% holds. Only later
  completed bars can cause ATR SELL, using unchanged shared 5/3.5/Wilder's math. Native bracket
  exits inside the break bar are booked first, against the exact current parent, with no body
  sell. Missing break bar means no strategy sell plus an incident; native protection stays.
- Early exits reuse the same durable close claim as the 15:55 fallback, so an earlier strategy
  close cannot be duplicated by a later end-of-day sweep. OMS checks the persisted exact-fill
  evidence and a fresh post-decision bid before claiming; negative MACD never blocks a close.
- Operator decision 2026-09-29: the normal strategy exits manage held shares; any residual
  position gets an end-of-day close attempt starting at 15:55 ET. The 10:00 cutoff cancels
  unfilled buys only. It does not liquidate a filled position.
- The end-of-day close verifies the owned parent fill, confirms the native sell pair has been
  released, rereads the remaining broker quantity, and sends a regular-session market sell.
  Its attempt is committed before submission. Repeated sweeps and OMS restarts cannot send a
  duplicate sell after an uncertain response. An exact child sell fill wins over a new close.
  Rejections or unreadable evidence open a durable incident for manual attention; an accepted
  sell is not a fill. A still-held position at 15:59 is flagged. No market sell is sent at/after
  16:00. These times describe a normal full trading session.

## Revision 2 verification

- 655 focused tests pass, including ORB, native Schwab brackets, existing v2 exits/fan-out,
  the unexercised watch and known-defect watch. No real broker call was made.
- All 13 new in-memory guard mutations were killed. Four RED controls restore the relevant
  code from reviewed head 84c3f234 and expose R1/A1/A2/A3. A separate exact-main control runs
  the same flag-OFF v2 test with the base process_trade_intent: it also passes.
- The full-suite comparison is recorded in [REVISION_2.md](REVISION_2.md). Raw local files:
  /tmp/orb-1064-r2-final-unit.log, /tmp/orb-1064-r2-base-unit.log,
  /tmp/orb-1064-r2-targeted.log, /tmp/orb-1064-r2-controls.log.
- New-head Linux CI and independent pinning remain separate requirements.

## Historical verification at 84c3f234 (superseded body rule)

- 483 tests passed across the ORB tests, native Schwab bracket tests, broker event-source
  checks, OMS ORB exits, v2 OCO emission/fan-out, and existing v2 end-of-day suites.
- Removing the confirmed-pair-release guard, ignoring the persisted attempt, or removing
  the 15:55 start boundary each makes its regression test fail (three mutation checks).
- Six additional in-memory mutations were killed for the restored early exits: remove body
  exit (1 test), change <45 to <=45 (1), remove ATR SELL (1), accept a non-Schwab ATR source (1),
  accept a six-second-old bid (1), and ignore an unconfirmed pair release (1). The stale-bid
  fixture was strengthened to keep the quote after the decision, isolating freshness from the
  independent post-decision guard. Repository files were not mutated by these runs.
- RED wiring control: restore the whole pre-exit intent contract from 60737ca7 in memory,
  retaining only the new test-event builder. Both body/ATR integration cases then fail because
  the strategy close is refused and only the later EOD fallback sells. This is a scoped RED
  control, not a claim that new tests can collect against main without the new modules.
- The real OMS control loop is exercised with a held ORB fixture, proving the fallback runs
  without the ORB producer. Broker calls in these tests use fixtures; no live order was sent.
- Full tests/unit on this build: 56 failed / 4588 passed. Fresh exact base
  fc68b2389a2e94f87db6183189a61f9b1ace15b3: 56 failed / 4459 passed, identical FAILED node IDs
  after stripping an interleaved warning from the output. No new failed test names; this is
  baseline parity, not a clean full-suite pass. Shared errors include Linux-only shell tooling
  missing on this Mac; neither run is production trading evidence. Local raw logs:
  /tmp/orb-1064-head-unit.log and /tmp/orb-1064-base-git-unit.log. The base was a clean isolated
  Git snapshot; an initial tar-only run was discarded because one test requires a Git index.
  The final focused rerun after strengthening the stale-bid fixture remained 483/483.
- Ruff and diff-whitespace checks pass. New-head GitHub CI and independent review are separate.

## Not yet deployment-ready

- Schwab's specific pre-open STOP_LIMIT OTOCO preview, live placement, replacement, and confirmed
  cancellation have not been exercised. A separate attended one-order test at 09:25-09:26 ET can
  prove that broker workflow before activating the three-bar strategy. The test needs exact
  symbol/price/date authorization and confirmed cancellation before 09:30.
- The reviewer supplied 26-bar coverage for 32/34 candidates and persistence latency for 921
  bars. This does not establish the new full-series/35-bar seed requirement, nor continued
  coverage after v2 watch removal. No new production read was performed in this revision.
- Independent review, base/head pinning, and new-head Linux CI remain outstanding.
- Early-close exchange sessions and real phone delivery of all new ORB incident sources must
  be covered before activation. The fallback still uses the normal 16:00 close. Installing
  the pager requires updating its separate installed copy and both cron sha guards; no such
  installation or cron change was made here.

No service, broker order, account, environment flag, or production checkout was changed by this
local build.

## Flag-OFF rehearsal added September 29

- Separate default-off `orb_schwab_observe_enabled` allows this producer to run while
  `orb_live_schwab_orders_enabled=false`. Enabling both is rejected at startup.
- Reuses the same three-bar decision model, bracket pricing and completed Schwab MACD read.
  Observation branches before any intent construction/publish and does not change gateway
  subscriptions. The existing feeds must cover the candidates; missing range minutes appear
  in the once-per-minute coverage record.
- `[ORB-SCHWAB-OBSERVE]` JSON records contain raw/qualified bar highs, bar counts, proposed
  actions, prices, MACD result, fresh observed trade crossings and conditional unfilled-order
  cancellation checks. Each states `broker_orders_sent=0` and `fill_status=UNMEASURED`.
- Cross records require an explicit fresh trade timestamp after the hypothetical plan and
  within 09:30-10:00 ET. They are deduplicated by symbol, proposed trigger and cap relation;
  they do not represent the number of trades, fills or executable opportunities.
- After-open MACD observation is sampled once per minute; it does not model the exact OMS
  polling cadence, broker state, cancel/fill races, or fills. ATR/account collision checks and
  end-of-day sells are not exercised by this read-only producer.
- Producer/observation tests cover zero publisher calls/Redis writes,
  live-OFF startup, mode exclusion, actual gateway event parsing, missing/negative MACD,
  conditional cancel after a cross, cutoff, stale-tape rejection and session reset.
- Conditional exit observations now include body and completed-Schwab ATR evidence, explicitly
  contingent on an observed cross having filled. They are not actual fills or P&L. Live producer
  tests read actual fixture fills and persisted Schwab bars, persist evidence before publishing,
  preserve held-symbol subscriptions after scanner removal, and refuse unknown execution time.
