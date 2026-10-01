# CW target stay and per-leg release

Status: review only. No production install, restart, flag change, or watcher re-pin.

## Decision and scope

- A tagged `CW_TARGET` extended-hours LIMIT stays at its original broker limit despite the normal working-order refresh cadence and a bid below that limit. Untagged hard-stop, floor, flip, entry and native bracket orders retain their existing refresh paths.
- Each account's open managed row supplies its own entry price. A fresh bid at or below `entry_price * 0.99` requests cancellation of that account's target. A session change also requests cancellation. Only broker-origin `cancelled` readback releases the reservation. Webull's normal broker-origin `accepted` response is PENDING, with no immediate incident. Requests are spaced at least one second apart per order; a rejection or ten seconds without terminal confirmation creates one critical incident per order.
- A pending confirmation, a fresh ATR flip, or a hard-stop breach first requests target cancellation. Until the broker confirms it, no replacement software sell is sent. The confirmation decision remains pending. At 19:55 the existing operator rule takes precedence: request cancellation but submit the flatten even if the target's cancellation is unconfirmed; shares-unavailable refusal is handled by the existing overnight incident path.
- Schwab and Webull evaluations for the same quote are scheduled together, with elapsed time recorded per leg at debug level and slow legs (at least 250 ms) warned. The target rule is enabled by default through `oms_v2_cw_target_stay_enabled`; the expected-flags catalog requires it ON.

## Evidence and verification

- Before the change, the new NXL target tests failed because a nonmarketable target was repriced and no per-leg release existed. Added cases cover both accounts, NXL 7.48 limit held at 7.41 bid, 7.1569 entry releasing at 7.085 but not 7.09, independent Webull entry, return to a new target after confirmed release, session-end cancellation, Webull-shaped accepted-then-confirmed cancellation, ten-second paging boundary, one-second retry spacing across quote ticks, client-only cancel safety, flip and stop overrides, pending confirmation, 19:55, and concurrent leg scheduling.
- Local `tests/unit` was interrupted after 939 passes and 44 failures; the failures shown were installer tests invoking Linux `/usr/bin/sha256sum`, absent on this macOS host. Linux CI is required before review approval; this is not a full-suite pass claim.

## Install boundary

This PR changes live OMS behavior and adds an INC1 source to the repository watch. An operator-approved exact-SHA after-hours deploy must independently verify both broker accounts' flatness and restart OMS with its strategy companion, then read the new flag from the running OMS process. **The new `oms_v2_cw_target_cancel_unconfirmed` incident cannot page until the installed watch copy and both hash-guarded cron lines are re-pinned; that installation is held on WBREAD1.** Merge does not install them. The first live target, threshold release, and incident/page path remain unexercised until observed on the box.
