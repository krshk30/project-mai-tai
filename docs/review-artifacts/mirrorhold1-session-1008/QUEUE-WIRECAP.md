# Queue/restore and D28 successor

Lane G remains sole writer on the existing branch/worktree and PR #1134,
descending from frozen `5a8b32087b15f9628d35acb308c90d552d6bf605`; exact main
base remains `1e15adb03c3758e647d333828bbe3090e24e832e`. Independent implementation
assessment: no source pin, production change, rollback, merge, or activation.
Parent owns shared handoff; A-F source unchanged.

## Authorization and independent data

The later user explicitly authorizes queue/restore session-bound scans and
D28's PRICE_AGGRESSIVE-only cap. This supersedes the earlier card's all-wire cap,
not its three actual resubmissions, 8% guard, free waits/reauthorization, or RPG
proof. Accepted reprices do not burn the refusal budget. Four evidenced broker
PRICE_AGGRESSIVE refusals (initial plus three actual resubmissions) exhaust it;
local no-wire refusals never spend it. All actual wire client IDs remain in a
separate monotonic ledger for duplicate/uncertainty proof and diagnostics.

Fresh own bounded READ ONLY capture `/tmp/mirrorholdG-D28-wirecap-1008.json`:
four FLYE orders, nine intents, twelve audits, zero fills; limit+1 complete within
11:15-11:40 ET account/symbol scope. LEFT JOIN strategy identity: zero NULLs.
Four accepted reprices submitted 11:20/11:22/11:24/11:32, all broker-terminal
cancelled before the 11:38:03.290006 intent (135 shares). That exact intent records
client_abort/mirrorhold_actual_submission_cap with no fifth order in scope.
The complete successor capture `/tmp/mirrorholdG-D28-complete-wirecap-1008.json`
includes all 43 prior FLYE buys, 43 linked intents, 52 audits, and the actual
10:00:40.195 fill, within limit+1 bounds. This includes the earlier unrelated
current-session fill in the cap replay instead of bypassing it. All strategy
identities are present (asserted from actual rows). The original four-order
D28-window capture remains separately preserved.
Safe projected fixture preserves actual IDs, clocks, quantities, and audits;
no invented accepted history or post-intent Fill. New SDK acknowledgements in
replays are controlled, not additional live acceptance/fill claims.

## Implementation boundaries

- Dispatch, queue, and restore use the same coarse SQL 04:00 ET bound, retaining
  same-segment history across age, working/unknown-status orders, NULL timestamps,
  and explicit unreadable identity. Retirement requires exact segment AND slot.
- Gate refusals in all three paths emit WARNING with the actual candidate count;
  invalid segment evidence before scanning logs zero. Precheck/8% unchanged.
- Durable PRICE_AGGRESSIVE client set survives canonical reprices, nonces,
  duplicate delivery, and restart. Legacy budgets are upgraded off-loop from
  retained exact-slot audits. An old lifetime cap becomes blocked (not immediate
  dispatch permission) only with proof; the normal current-order/slot/RPG/risk
  gates still decide the next intent. Missing proof remains uncertain. Terminal
  retired/filled owners cannot reopen.
- The tick scheduler's budget predicate reads the new refusal counter in memory;
  a legacy cache uses the old total conservatively until off-loop upgrade. No new
  tick database/HTTP call. Service, strategy, bot, ineligible cache, shared handoff,
  RPG abort proof and 8% precheck are not edited.
- PM soft mirror `eh_resting` reaches the shared dispatch call but fails the
  existing `rth_resting_mirror` scope check, bypassing this scan. Clock controls
  with the RTH gate held open are not real PM dispatch coverage. No EH policy added.

## Verification receipts

Original 5a8 full/controls remain preserved: 49 failed / 7738 passed / 55 skipped,
612.72s; two added failed names, one absent baseline name, not parity. Equal
resolved-path baseline/head cron line 1003 > 1000 is a portability residual;
isolated retained-on timing passed both but its full 318.467ms failure was not waived.

At source freeze:

- 268 passed / 2 deselected / 28.95s focused-plus-neighbor run. This includes 161
  retained/session/queue tests (65 direct tests + 27 queue/cap tests + 69 existing
  retained tests), plus 107 neighbor controls. The two 60s timing benchmarks are
  left to the exact-head full run, not waived or modified.
- 13/13 mutations killed with green baselines and assertion-red mutants:
  dispatch/queue/restore session bounds, current pending, same-segment history,
  exact-slot retirement, refusal WARNING, lifetime cap regression, unbounded
  PRICE_AGGRESSIVE retries, final dispatch cap, reprice budget reset, history
  adoption removal, and unknown legacy-budget release.
- PostgreSQL actual query: original AIXI retains its unrelated current fill;
  original FLYE retains none; D28 retains the earlier fill and four reprices.
  Ten VALUES controls retain unknown/working/current/same-segment rows, excluding
  three old unrelated terminals. Source SHA256:
  `c659705599230b823d8b69d465f2f881e493b9ec53fb23723392942e3c1934e5`.
- Ruff, whitespace, and exact-main source-protection checks pass. New budget
  code changes only the listed retained-hold methods; protected 8% precheck,
  RPG clear/no-wire proof, claim/CAS, and off-loop worker mechanics remain exact.

Raw `/tmp/mirrorholdG-queue-final-focus-neighbor.{xml,log}`,
`/tmp/mirrorholdG-queue-final-mutations.json` (children in queue-final-mutants),
`/tmp/mirrorholdG-queue-final-postgres.json`, and
`/tmp/mirrorholdG-queue-final-source-protection.json`.

Three requested path replays, separate D28 controls, and exact-head full literal
comparison will be published after this commit in the PR body and raw receipts.
Full main is not rerun; compare to preserved common exact-main XML, not counts
alone. No mandatory docs successor to restart CI after the full run.
