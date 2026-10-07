# ORBLIVE1: one live ORB dashboard (draft)

## Latest Disposition

2026-10-07 operator/reviewer correction: ORBPAPEROFF1 is withdrawn. Keep
`project-mai-tai-orb` enabled and running; no further service action tonight,
and no change to the parent's paired job. Reviewer reports restoring it as
PID 1322003. The stop receipt below is historical, not current state.

The executable entry point is restored in this draft. The simulation's
modelled-fill/writer removal INSIDE the running service remains unfinished;
this PR is NOT ready to pin/install. Dashboard attribution work is isolated
and tested, but must not be presented as complete ORBLIVE1. Tomorrow's
reviewed code change must preserve levels/decisions/listening while removing
simulation writes without duplicating the independent live producer.

## Step 0

AGREE: the separate `mai-tai-orb` entry point runs the modelled-fill
simulation. `mai-tai-orb-schwab` is the independent live producer. Its live
entry/exit implementation is unchanged by this patch. Do not enable a second
producer or reinterpret simulation events as broker fills.

AGREE: `/bot/orb` already selects strategy `orb_schwab`. Completed trades and
realized P&L use its live orders/fills; open exposure uses its strategy-owned
book, never the shared account's aggregate quantity. This patch also filters
recent intents/orders/fills by the registered account, removes the simulation
page/nav/runtime registration, and shows live pre-submit refusals in both the
decision tape and failed actions. Watchlist, levels, activity and decisions
remain the live bot's own reported state. Missing activity remains UNKNOWN.

AGREE: the recorded 2026-10-07 09:28 ET BIYA/SXTC entries were refusals, not
trades. Own bounded read of `trade_intents` joined to `strategies`, with
`strategy.code=orb_schwab`, symbols BIYA/SXTC and UTC window 13:27-13:30:

| Symbol | Quantity | Updated UTC | Status | Origin | Code |
| --- | --- | --- | --- | --- | --- |
| BIYA | 2 | 13:28:04.279307 | rejected | client_abort | orb_schwab_preview_not_accepted |
| SXTC | 2 | 13:28:05.070142 | rejected | skipped_before_submit | schwab_ineligible_cached |

Own broker-order/fill/book reads found no corresponding orders, fills or open
managed rows. The replay asserts both refusals visible, zero positions,
completed trades and P&L. Yesterday's simulation results do not populate any
live panel. Existing JAGX live-fill and shared-account controls remain green.

## Authorized retirement receipt

The operator explicitly superseded the earlier keep-running instruction:
stop the separate simulation, keep ORB Schwab live. Executed only
`systemctl disable --now project-mai-tai-orb.service`.

- Simulation: inactive, disabled, PID 0.
- ORB Schwab: active, enabled, unchanged PID 765206.
- Momentum paper: unchanged (inactive, enabled, PID 0 at this observation).
- Published ordinary empty subscription `replace` for consumer `orb` at
  stream ID `1791403760852-0`. Its stale BIYA/SXTC claim became `[]`.
  Other owner values and migration marker were unchanged; ORB Schwab retains
  BIYA/SXTC. No gateway restart or direct owner-hash deletion.

The empty historical owner field is retained for compatibility; it owns no
symbols. Historical `orb_paper_events` data and legacy types remain intact.
The executable remains running under the latest disposition. The inherited
aggregation helpers used by the live producer are unchanged; removing the
simulation writer from the running service remains a draft blocker.

## Verification And Deployment Boundary

223 focused tests passed across control-plane, simulation/live boundary,
ORB Schwab service and expected-flag checks. Ruff and diff whitespace checks
passed. Full-suite/control comparison and independent review are not yet
complete; this is not a pin or install receipt.

Source/dashboard/catalog changes have NOT been deployed. No live ORB,
control-plane, momentum or other service was restarted. The reviewed install
must publish the new control-plane source and matching health/catalog files;
until then the old production catalog may still expect the disabled simulation.
Do not re-enable simulation to satisfy that catalog. Existing live orders,
fills, database schema and historical tables are unchanged.
