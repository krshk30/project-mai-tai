# Native-Timezone Runner Reissue, October5 18:50 ET

Application:7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf. Same operator GO,
four true flags, schema0022 and no recovery. Fresh exact-byte approval needed.
No staging/execution of these bytes; no new production mutation.

## Runtime Delta Against950e9e42

ONLY run.sh changes among executable artifacts. Global TZ export removed.
UTC display uses date -u; ET display/window uses command-scoped
TZ=America/New_York date. No unsetting/overriding the caller's native TZ.
On the verified native UTC box systemctl therefore retains UTC output.
The NY-parent test proves the runner does NOT set TZ, not that a caller
which itself supplies NY would get UTC from systemctl.

First-stop date10-05 and bounds20:05:00 <= ET <=21:30:00, checked immediately
before the v2 stop. Date/upper failures return1 explicitly. No clock checks
after that stop. Preparation waits until20:05 before advancing checkout/env.
The reviewed rc2 retries remain3 attempts60s apart; measured blocks stop.

Attempt2's official snapshot failed on EDT before checkout/env/service work.
Old job:/home/trader/after-hours/2026-10-05/owned-entry-rpg-all-on-1825-job.
Preserve it; the approved reissue must use a new exclusive job directory.
Old runner.log SHA256:
e15a1060714481b424f216d44f89198a5943d88a44790485dc36eaff69b7fc45.

## Timestamp Consumer Audit

Own source grep/read of the complete job and invoked ops sources; no edits
to these consumers. This is code-derived, not a new production dry run.

| Consumer | Status on native UTC caller |
|---|---|
| run.sh run/abort/window/wait | Explicit ET only in date command substitutions; all child tools inherit native environment; bounds only before first stop |
| v2_restart_evidence.py snapshot/report | systemctl timestamps parsed UTC/GMT only; no parser edit; removing runner TZ export restores native UTC strings |
| preopen_restart_evidence.sh | Routes supplied rc and Final call only; no date/systemctl timestamp parser or TZ assignment |
| preflight_v2_restart.sh and preflight_oms_restart.sh | Compute ET with their own scoped TZ date; freshness uses aware timestamps; native environment unchanged |
| proof.py service/capture/checkpoint | Preserves native ExecMainStartTimestamp string and monotonic identity; no ambient ET assumption |
| proof.py start_time/logs/bar-holes | date --date converts explicit native systemd UTC stamp to aware ISO; log prefixes read as UTC; session boundaries use explicit ET |
| proof.py repin | Copies exact saved native systemd strings into EXPECTED_*START; does not reinterpret them |
| actions.py record | Copies UTC snapshot capture field; no systemd parser |
| actions.py repin | Hash-checks/installs proof's candidate, no timezone conversion; date fixed Oct6 |
| actions.py verify/journal/migration | Approval date explicitly ZoneInfo ET; journal UTC aware; migration child inherits native environment |
| census_readonly.py / redis_checkpoint.py / strict_flat_readonly.py | Aware UTC/source timestamps or explicit ET; no systemd stamp parser/global TZ change; bytes unchanged |
| proof.py legacy positional verify-source/pre-action hooks | Old18:00-20:00 action=True fence remains UNUSED by run.sh. Detailed capture/checkpoint/logs/proc/census-after/gates/repin/bar-holes interface calls wall(action=False); existing tests pin post-stop completion beyond midnight |

No other job-level global TZ assignment found. Retain all helper hashes.
Fresh current oms.log / schwab-1m-v2.log / strategy.log after00:00 UTC
are10-06 evidence sources; their .log-20261006 copies contain earlier lines.
Quote actual paths/inodes/offsets from post-start-logs.json, not an assumed
filename or yesterday's content. No local test claims observed rotation.

## Tests

All39 PASS: test_actions.py, test_runner_retry.py, test_census_exit.py,
test_runner_timezone.py. Existing29 remain green; new10 cases:

- test_runner_never_sets_process_timezone_even_with_new_york_parent
- test_et_display_does_not_leak_into_snapshot_or_identity_child (NY/UTC/unset)
- test_literal_et_first_stop_boundaries (prep20:04:59; stop20:04:59 denied;
 20:05:00 allowed;21:30:00 allowed;21:30:01 denied; next date denied)

Existing R1/R2 tests still pass: unreadable-then-ok; three unreadables;
measured blocker/unrecognized code no retry; every flat/census call wrapped;
clock only before first stop; completion after20:05/midnight unaffected.
bash -n and Ruff PASS. Local, offline; no broker or systemd call in new tests.
No production code or gate change. Manifest regenerated from committed blobs
by unchanged make_release.py; old exact-byte approval cannot be reused.
