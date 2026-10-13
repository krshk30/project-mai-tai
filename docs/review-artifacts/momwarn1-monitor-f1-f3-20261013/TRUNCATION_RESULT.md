# F1/F3 Native Copytruncate Follow-up

Parent accepted logger packet: `fede48e5781ace6c40d43cb93569b5727d5dca30`. X/S base remains `d9e92b92a93b771edf2539fa9d954fcf6fa2b5d9`; prior commits/bundles/XML/results preserved, no rewrite. Local only; no box action or benchmark.

## Assessment / Narrow Cut

The old `WarningHealthReader.reset_window()` already cleared `_logs`, source/config and last OMS evidence. Gateway normal close invoked it only if A was active/requested; this did not guarantee a new baseline when a prior day had monitored but never activated A. Gateway now resets the native reader and window watchdog exactly once at each valid daily-window first monitor operation, even in that case. Sticky gateway/provider safety failures still return before reset and cannot be rearmed by a new day. The normal-close reset remains unchanged.

Actual `read_live` reaches native OMS and v2 offsets BEFORE the error-log reader, so changing `_logs` alone was insufficient. Same-inode shrink/rewrite now rebaselines all three paths. File replacement remains failure; this is not a rotation-identity waiver. Native OMS sequence/process/completion checks, source config/restamp guards, v2 process/probe/expectation checks, EOF/backlog bounds, memory/load, ERROR and slow-pass checks remain enforced.

The source scope beyond the logger packet is exactly:

- `src/project_mai_tai/market_data/gateway.py`: first-window reset and one structured gap notice on each newly detected per-path rebaseline; no per-second repeat notice.
- `src/project_mai_tai/market_data/momentum_warning_watchdog.py`: same-inode cursor rebaseline, discard cross-gap partial ERROR bytes, bounded per-log gap counts/accessor.
- `src/project_mai_tai/momwarn_health_contract.py`: native OmsLogMonitor cursor rebaseline; previously actually observed completion stays unchanged and ages normally. Sequence gaps/regressions, actual duration, process identity and source age are not cleared or faked.
- `src/project_mai_tai/momwarn_live_health.py`: native v2 cursor rebaseline; only previously actually observed bar completion can remain useful. If a required current-due bar is not known after a gap, fail UNKNOWN rather than infer it was not late or fabricate completion.
- New `src/project_mai_tai/momwarn_log_cursor.py`: at most 256 cursor bytes per fingerprint/check, using pread on an already opened regular file, no archive scan or connection. Detects shrink and changed cursor bytes after regrowth, preserving true backlog failures.
- `ops/health/momwarn_service_user_proof.py`: receipt adds the reader's bounded `log_observability`; no execution/environment/phase changes.

No OMS/provider/quote/consumer/settings/catalog/health-schema or trading-source changes. Existing `WarningHealth` model unchanged. Tests add native controls, adapt the controlled proc factory to survive actual daily source reconstruction, and replace ONLY the explicitly superseded legacy shrink-must-raise expectation with gap/rebaseline plus retained-ERROR assertions. All other original negative assertions remain. No performance harness change.

## Explicit Observability Decision

**Detected truncation alone rebaselines; it does not latch A off. Removed bytes are UNKNOWN, not no-error coverage.** Same-inode shrink or cursor-fingerprint mismatch marks `GAP_UNOBSERVED_COPYTRUNCATE`; gateway emits `[MOMWARN1-LOG-REBASELINE] unobserved_interval=UNKNOWN detail={per_log_counts}` once when counts advance. Subsequent normal samples do not repeat it. F3 receipt carries this status. Counts merge native/error cursor detections per path, not a measured number of logrotate executions.

Available retained bytes are scanned within existing bounds, including a retained ERROR at the beginning after regrowth. Original partial bytes are never joined across a missing interval. No copied/rotated interval is opened; inaccessible/missing archived bytes remain unobserved. A controlled ERROR removed before the monitor could read it intentionally cannot be rediscovered and is represented by GAP, never called clean. A may continue under this explicitly approved rebaseline policy only while all independent measured health requirements pass.

Previously actually observed same-process OMS completion/current-due bar facts may remain usable with their original timestamps; they are not restamped. **If the lost interval causes an OMS sequence gap or leaves required current-due v2 completion unknown, A still fails closed on that independent evidence gap.** Permission/read failure, missing source, dead/reused PID, config change, actual ERROR/SLOW, true backlog or inode replacement still reverts. This is the specific review boundary: no unconditional guarantee of A continuity when required health evidence itself is gone, and no waiver for unrelated UNKNOWN.

Fingerprint comparison is bounded, not exhaustive historical evidence. A truncate followed by byte-identical regrowth at the sampled cursor can be indistinguishable from continuity; it does not prove an unseen interval was clean. Even with no detected gap, status is only `OBSERVED_CURSOR_READS_ONLY`, never a blanket no-error claim. Concurrent changes that break bounded-read consistency remain read failures. No arbitrary historical recovery or provider query is attempted.

## Controls / Receipt

Focused result before exact-commit receipt: **199 PASS, 0 FAIL, 0 SKIP, 4 timing/burst controls deselected in 1.78 seconds**; 30 new native truncation controls plus all prior 169 selected controls. Ruff and diff check PASS.

- `/tmp/momwarn1-copytruncate-final-controls-20261013.xml` and `.log`.
- `/tmp/momwarn1-copytruncate-ruff-20261013.log`.
- Native `read_live` (not legacy-reader-only): each of four logs truncated empty or rewritten/regrown beyond old offset, real retained facts/TQ/owners unchanged, one gap notice, no repeat, no automatic truncate-only revert.
- Two daily cases: prior A active AND prior A never active. Explicit controlled09:30 close,20:00-ET truncation, next-trading-day04:00 fresh source/OMS sequence100; no old source/offset/completion baseline survives, no false revert.
- Four retained ERROR cases; two actual slow OMS completions across separate truncations still latch; unobserved archived ERROR remains explicit GAP; partial `ERR`/`OR` across gap cannot fabricate ERROR.
- Native negatives: replaced inode (OMS/v2/error log), true backlog (all three), OMS sequence gap, missing next current-due bar, dead OMS/v2, changed config, missing log, altered same-sequence end; all still A-only fail closed.

Preserved first candidate regression XML `/tmp/momwarn1-truncation-initial-regressions-20261013.xml`:168 PASS/1 FAIL, solely old now-superseded legacy truncation expectation. Preserved new-control initial XML `/tmp/momwarn1-copytruncate-controls-initial-20261013.xml`:27 PASS/1 FAIL revealed 64-byte anchors could miss changed timestamp/sequence on equal-length OMS lines with identical suffixes. Increased bounded fingerprint to256 bytes; real-SLOW proof now passes. These are functional controls, not timing retries or fabricated mutation proof.

Final exact-head command:

```sh
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -B -m pytest -p no:cacheprovider tests/unit/test_momwarn1_copytruncate.py tests/unit/test_momwarn1_monitor_diagnostic.py tests/unit/test_momwarn1_service_user_proof.py tests/unit/test_momwarn1_gateway.py tests/unit/test_momwarn1_live_health.py tests/unit/test_momwarn1_health_contract.py -k 'not fair_live_loop and not huge_synthetic and not slow_offloop and not raw_SDK_normalization' -q --junitxml=/tmp/momwarn1-copytruncate-exact-20261013.xml
```

Actual F3 is still NOT RUN: main-only after F2 actual after-close permissions/copy-rotation proof, target weekday16:30-19:00 ET before midnight, current service user/environment, two native reads without clock override, before gateway restart. Read success is not healthy. Main owns exact final composition broad/original60s3/3, then reviewed install/restart/guard rebaseline/dry-run/postcheck. This packet confers no production authority.
