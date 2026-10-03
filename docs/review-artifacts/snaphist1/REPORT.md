# SNAPHIST1: scanner snapshot retention

Status: built for review only. Do not merge until the separate gateway-restart
plan exists; neither merge nor this report authorizes an install. RPG1 piece 3
remains first priority. Operator chooses the weekend and exact application SHA.

## Requirement and independent assessment

Operator 2026-10-03 decision 5: reduce retained snapshot batches from 180 to 120,
preserve scanner 5-minute and 10-minute squeeze alerts, and validate the scanner
after installation. The operator accepted the correction that the gateway,
not strategy, owns retention and must restart to activate this setting.

Independent read on 2026-10-03 12:42:47 ET: gateway PID 2346625, strategy
2800140, paper 2477935 all had process interval 5 seconds; retention was unset
and defaulted to 180. XLEN was 180; evicted_keys remained 23 -> 23. No snapshot
payloads were read. Local raw evidence:
`/Users/velkris/.codex/study-evidence/scanner-depth-20261003/runtime.json`.

`MomentumAlertEngine` needs 60 entries for 5-minute and 120 for 10-minute
warm-up at that interval. AGREE with the count budget. The gateway constructs
`MarketDataPublisher` with this setting once; XADD uses that captured value.
A fake-Redis ownership test showed an existing publisher remained at 180 after
changing Settings to 120; only a newly constructed publisher used 120. Evidence:
`/Users/velkris/.codex/study-evidence/scanner-depth-20261003/publisher-owner-proof.json`.

Reader audit against main 608339894a1cfb33284e695196df55c18f312889:

| Reader | Depth / behavior | Effect |
| --- | --- | --- |
| Strategy prefill | XREVRANGE count=squeeze_10min_needs, currently 120 | Full count remains available if 120 valid nonempty batches exist. |
| Strategy live stream | Latest-entry initialization, rolling XREAD count=500 | Less retained backlog after a stalled consumer. |
| Control dashboard | limit=1 | No historical-depth requirement. |
| Momentum paper | Starts at `$`, then rolling XREAD count=500 | `$` does not eliminate later backlog; tolerance is reduced. |
| Treatment guard | Bootstrap COUNT 1, exclusive forward COUNT 1, bounded catch-up | No 180-entry requirement. |
| Gateway throughput diagnostic | Starts at `$` | No warm-up depth requirement. |

120 entries at 5 seconds are **600 seconds nominal backlog**, versus 900
seconds previously. First-to-last timestamp span for 120 exactly periodic
entries is 595 seconds. Actual cadence, MAXLEN approximate trimming, invalid
batches and consumer delays mean neither figure is an exact guaranteed recovery
window. The 120-entry strategy warm-up reply is NOT made smaller by this change.
No consumer logic, interval, alert rule, guard or entry/exit rule changes here.

## Implementation

- Settings default is 120; the publisher already reads it through gateway construction.
- Numeric catalog expected=120, owning_service=`market-data`,
  require_process_env=true. The installation must explicitly set
  `MAI_TAI_REDIS_SNAPSHOT_BATCH_STREAM_MAXLEN=120` and read the NEW gateway's
  `/proc`; a default inferred from newly checked-out code must not certify an
  old running gateway.
- No production env edit or service restart performed by this PR.

## Tests and mutation

`test_snapshot_batch_stream_default_covers_alert_warmup_window` pins the
default, the live-interval warm-up budget, catalog value and gateway ownership.
It was RED with the old default 180.

`test_snapshot_retention_prefills_both_squeezes_at_live_five_second_interval`
publishes 181 synthetic batches through the actual gateway publisher into a
fake Redis that honors MAXLEN with exact trimming. A newly constructed strategy
prefills >=120 entries, logs N=120 and emits both SQUEEZE_5MIN and SQUEEZE_10MIN.
The test does not rely on approximate-trim overshoot to obtain extra history.
Mutation: default 120 -> 60 makes the actual prefill assertion RED:
`assert warmup["history_cycles"] >= needs`, observed `60 >= 120`.
Restoring 120 is GREEN. Fixtures are synthetic unit data, not live scanner proof.

Existing publisher assertion updated to 120; numeric catalog audit updated
from 7/7 to 8/8. No gate logic changed.

Regression: 384 passed (2 existing SQLAlchemy warnings) across strategy service
and loop resilience, gateway, Momentum Option A/paper service, treatment guard,
1008 sampler and expected-flags suites. Changed Python files pass Ruff.
Full unit suite is delegated to both GitHub Validate runs; do not infer their
result from this focused pass. First-session scanner and real restart behavior
remain UNEXERCISED pending the reviewed installation and validation recipe.

## Install dependency

Draft plan: branch `codex/snaphist1-gateway-restart-plan`,
`docs/review-artifacts/snaphist1/GATEWAY_RESTART_PLAN.md`.
It must cover every installed 1008 detector before #1077 INFO logging becomes
live, #1084 full owner persistence, all five consumers, unchanged evictions,
numeric ownership, and post-strategy-restart scanner validation. No standalone
strategy restart or retention-only Redis trim is authorized by this PR.
