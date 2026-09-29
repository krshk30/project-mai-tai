# ORB Schwab live route: review-only build

This branch is not approved for deployment. `orb_live_schwab_orders_enabled` defaults to false;
the separate `orb-schwab` unit is not installed or part of the production target. The existing
`orb` paper observer remains broker-disconnected.

## Implemented locally

- One two-share NORMAL/DAY Schwab STOP_LIMIT parent with native +5% / -8% OTOCO children.
- Place after the completed 09:27 bar (three 09:25-09:27 bars), around 09:28 ET. Request an
  upward-only replacement after the 09:28 and 09:29 bars, never a second OPEN order.
- Completed Schwab one-minute MACD must be nonnegative; unknown data blocks entry, and a later
  negative/unknown reading requests cancellation of an unfilled parent. OMS also checks the
  condition and cancels working parents at the 10:00 ET entry cutoff.
- OMS checks broker positions and its own ORB/v2 orders before accepting a new buy. It previews
  the bracket, verifies an unfilled parent before cancel/replace, and records only a broker-
  confirmed replacement. An unknown PUT outcome is labelled unknown, not accepted.
- The OMS polls the exact owned Schwab child sell for fill attribution, not an account-wide
  symbol match.
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

## Local verification

- 420 tests passed across the ORB tests, native Schwab bracket tests, broker event-source
  checks, OMS ORB exits, v2 OCO emission/fan-out, and existing v2 end-of-day suites.
- Removing the confirmed-pair-release guard, ignoring the persisted attempt, or removing
  the 15:55 start boundary each makes its regression test fail (three mutation checks).
- The real OMS control loop is exercised with a held ORB fixture, proving the fallback runs
  without the ORB producer. Broker calls in these tests use fixtures; no live order was sent.
- Ruff and diff-whitespace checks pass. Full-suite baseline parity is not claimed.

## Not yet deployment-ready

- Schwab's specific pre-open STOP_LIMIT OTOCO preview, live placement, replacement, and confirmed
  cancellation have not been exercised. A separate attended one-order test at 09:25-09:26 ET can
  prove that broker workflow before activating the three-bar strategy. The test needs exact
  symbol/price/date authorization and confirmed cancellation before 09:30.
- Coverage and timing of v2-persisted Schwab bars for ORB-confirmed symbols have not been measured
  on the production box. Missing bars fail closed; they are not substituted with Massive bars.
- Independent review, base/head pinning, and full-suite baseline comparison remain outstanding.
- Early-close exchange sessions and phone delivery of the new end-of-day incident source must
  be covered in deployment review. The current local implementation uses the approved 16:00
  close and records incidents in the existing OMS incident table.

No service, broker order, account, environment flag, or production checkout was changed by this
local build.
