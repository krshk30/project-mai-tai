# Native Coverage Review Repairs

Parent `39fc287e0f1051db0682df8fa245450bef949250` preserved with its independently verified200 controls/bundle. This is a new local follow-up, not an amendment or source-acceptance claim. No box/performance action.

## OMS: Sequence Coverage Is Not Byte Coverage

Finding valid: old native OmsLogMonitor returned `oms_monitor_coverage=complete_since_baseline` after a detected lost interval. Its separate observability flag did not qualify that returned receipt.

New native OmsLogMonitor ALWAYS emits:

```json
{
  "oms_monitor_coverage": "consecutive_observed_end_sequence",
  "oms_log_byte_coverage": "gap_unobserved_copytruncate",
  "oms_log_copytruncate_count": 1
}
```

Before a detected gap the byte value is `observed_cursor_reads_only`, count0; this counts detected rebases, not proof that no copytruncate ever occurred. After a gap it stays explicitly qualified. The sequence value means actual parsed phase=end IDs remain consecutive since the first observed baseline; it does NOT assert continuous log-byte/time/error coverage. No original completion time/duration/process/source is restamped. Original sequence-gap/regression rejection is unchanged.

`proposed_health_v2` validates the new enum AND byte-status/count correspondence, retaining both extra fields through native source output and the reader's second validation. Negative/noninteger counts, UNKNOWN/missing byte status, status/count mismatch, false legacy-continuity string with byte fields, stale end and wrong source still reject. It does not upgrade an UNKNOWN interval to complete bytes. Unchanged old marker WITHOUT new byte fields remains the existing legacy controlled-input compatibility path; no new native source emits that marker, and a supplied explicit gap cannot be hidden under it. Live configuration/source binding checks remain unchanged. This is an additive schema-v2 metadata representation, not a health threshold change.

## V2: Bound Gap Taint to Its Actual Interval

Finding valid: cumulative `self.truncations` made every future missing-completion minute UNKNOWN, even after the gap's due interval had ended. That wrongly removed the native0-5-second arrival allowance.

On detected same-inode rewrite, `_read_probes` now moves `covered_from_ns` to the actual post-gap bounded EOF observation time. It does NOT backdate coverage to a retained log timestamp or invent removed evidence. Gap counters remain cumulative for truthful historical observability.

- A missing required completion whose due is at/before that post-gap baseline remains UNKNOWN. The gap's removed bytes might contain its completion, so neither absence nor punctuality is inferred.
- A later due strictly after that baseline belongs to the newly observed interval and receives the original pending-bar lateness measurement. Exactly5seconds remains allowed; >5seconds reverts A through the unchanged watchdog.
- A current-due completion already actually observed (or positively present in retained bytes) remains usable with its original timestamp. No fabricated completion or future print is seeded.
- Every new detected gap updates the interval boundary again. New-window reset clears old reader instances normally. Inode/process/config/read/backlog/provenance/ERROR/SLOW failures remain independent and fail closed.

Source changes from39fc are ONLY `src/project_mai_tai/momwarn_health_contract.py` and `src/project_mai_tai/momwarn_live_health.py`. Gateway/logger/cursor helper/staged caller and settings/OMS/provider/trading code unchanged. Bounded cursor detection limits disclosed in the earlier truncation packet remain; this does not prove an undetectable byte-identical rewrite was clean.

## Native Controls

**12 new controls PASS** (`/tmp/momwarn1-gap-coverage-review-controls-20261013.xml/.log`,0.62s). Full selected follow-up: **212 PASS,0FAIL,0SKIP,4 timing controls deselected**,2.00s, preserving previous200 controls. Ruff/diff checks PASS. Exact committed-head receipt is saved separately after commit.

- Actual native OMS raw receipt before/after shrink, original end preserved, qualified fields survive source normalization and reader revalidation.
- Eight malformed/UNKNOWN/false-continuity/stale/source cases rejected.
- Controlled08:20:30ET truncate with current due already known; actual native read_live at08:21:00 returns pending0s and stays active; actual controlled08:21:02 qualifying probes prove2s arrival, no early revert. Byte-gap history remains flagged.
- Separate post-gap pending minute:0/1/5s allowed,5s+1ns reverts `v2_bar_late`.
- Due boundary lies INSIDE lost interval (last read08:20:04, detected post-gap EOF08:21:03): actual missing completion stays UNKNOWN, gateway A-only latch still fires.

The earlier `v2_missing_due` negative is preserved as the real inside-gap shape, rather than its former permanently tainted future-minute input. Its exception/A-only assertions remain; the new fully observed minute positives are its required twins. All original ERROR/SLOW/PID/config/backlog/sequence negatives still pass.

Command for final exact-head receipt:

```sh
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -B -m pytest -p no:cacheprovider tests/unit/test_momwarn1_gap_coverage_review.py tests/unit/test_momwarn1_copytruncate.py tests/unit/test_momwarn1_monitor_diagnostic.py tests/unit/test_momwarn1_service_user_proof.py tests/unit/test_momwarn1_gateway.py tests/unit/test_momwarn1_live_health.py tests/unit/test_momwarn1_health_contract.py -k 'not fair_live_loop and not huge_synthetic and not slow_offloop and not raw_SDK_normalization' -q --junitxml=/tmp/momwarn1-gap-coverage-review-exact-20261013.xml
```

Actual F3 remains NOTRUN/main-only after F2, weekday16:30-19:00ET before midnight, two actual trader/environment native reads without exception (NOT healthy), before gateway restart. Main's exact composition broad/original60s3/3 and source acceptance/install remain pending. No worker queue edit or production authority.
