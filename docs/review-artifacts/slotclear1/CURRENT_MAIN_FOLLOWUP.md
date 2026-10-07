# Exact current-main follow-up

Human authorized one local draft rebase onto exact `994f08ae` at 15:03 ET.
The preceding source/test freeze and failed full-suite receipts were committed
and retained on backup branch `codex/slotclear1-sell-frozen-pre994-20261007`.
Rebase completed without conflicts; four commits are `=` in `git range-diff`:

| Before | After | Scope |
| --- | --- | --- |
| 1bc0f076 | 3c8b1ce0 | Original BUY assessment |
| d68d25e0 | a2f2aa5c | Original narrow fresh BUY |
| e246cc14 | 70107121 | Independent fresh SELL |
| 280a7473 | d9ac712a | Retained receipts / catalog total correction |

Rebased source/test head `d9ac712a64af9187e106ae0f30139affa387e4e8`, tree
`b63dff06a63ce2313c1bbf206935901c0edc16fe`. Only the original SLOT range was
replayed. No #1115/RPGSTALE or other patch was introduced. Later commits in
this task contain documentation, supplemental test harnesses and receipts only.
Initial local freeze preceded publication. Human later requested frozen-head
publication; exact-lease push replaced remote d68 with95cf, verified19:16UTC.

Rebased focused: 451 pass /2 sustained HOTFIX benchmarks deselected, 21.56s.
Supplemental both-SLOT-switches-ON: 10 pass, including eight exact owned-row
controls (primary/mirror x bound/consumed/awaiting-close/unknown). No historical
fills are asserted. Rebased S1-S7 and F1-F11 mutations: 18/18 RED. Source/ops/tests
diff check passes; literal raw failed receipts retain pytest trailing whitespace.

Old-base full pair remains failure evidence, not current-main readiness:
original d68 7,106 pass /56 fail; e246 7,144 pass /57 fail. Added failure was
`test_pmprint1_tonight_flags.py::test_s6_catalog_matches_all_on_live_set_and_149_checks_with_restoration_on`
(catalog total 157 vs stale expected 156); assertion corrected before rebase,
targeted test file passes. Earlier source/harness failures remain in separate folders.

Exact-main full baseline ran separately at
`/tmp/codex-slotclear-sell-current-main-994-20261007`. Receipt initially lives at
`/tmp/slotclear-current-main-994-unit-receipt`, to be preserved in this folder.
Candidate full suite followed baseline completion to avoid overlapping their
60-second HOTFIX benchmarks. Neither old-base pair nor focused results is a
replacement for the actual main failed-name comparison.

## Final measured standard pair

| Exact head | Pass | Fail | Pytest seconds | Suite UTC |
| --- | --- | --- | --- | --- |
| Main994 | 7114 | 56 | 455.80 | 19:03:43.202843--19:11:24.386758 |
| Candidate95cf | 7183 | 56 | 457.28 | 19:21:11.755196--19:28:54.052966 |

Standard commands/environment/capture match exactly: PYTHONPATH=src, shared
Python3.12.13, python -m pytest tests/unit -q --tb=short -vv. No observer or tee
in either standard run. No timeout; 660-second candidate bound unchanged.
Added failed names ZERO; removed failed names ZERO. All56 common failures remain
unresolved, including dead-consumer throughput; no green full-suite claim.
Actual normalized lists/hashes are in CURRENT_MAIN_PAIR.json. Its extraction
uses verbose outcomes to avoid a baseline unraisable-warning concatenated onto
one short-summary name. Raw result/output files remain byte-for-byte unchanged.

First timed run58fail/7181pass and FIRST_TIMED_PAIR.json remain unwaived.
Its later child-process pytest re-entry was a launcher defect, fixed only in the
receipt helper; subsequent standard run uses no helper. Both HOTFIX gates pass
in baseline and standard candidate. Standard-run passing metrics/internal-gate
UTC are unavailable under capture; first timed testcase bounds/metrics remain
separately labeled, not substituted as standard-run measurements.

Focused451pass; freshBUY30+freshSELL39; supplemental12pass; 19/19 named mutations
RED. Present-ticket proof preserves both SXTC vetoes evenhandoffOFF; zero opens
through19 actual probes, payloads unchanged, no historical clearance inferred.
Source/tests/ops unchanged fromd9ac throughout both full runs; later commits are
receipt/harness/docs only. CURRENT_SOURCES_AND_MUTATIONS.json freezes13 path
hashes and every actual RED failed name. REPRO_CURRENT_MAIN.md contains commands.

NOT READY: common56 local failures, Linux CI still pending, missing independent
review pin, six NFQ composition conflicts, historical durable ownership clearance
and live OMS qualification remain outside this scoped SLOT acceptance. No pin,
merge, activation, #1115/RPGSTALE patch or original NFQ edit.

Exact-main baseline completed 19:03:43.202843--19:11:24.386758 UTC:
7,114 pass /56 fail, 455.80s pytest time. Raw receipt retained in
`rebased-main-unit/`. Both retained ON/OFF HOTFIX benchmarks passed in that run.

Present-ticket supplemental controls: 11 pass. Read-only pull retained 7 durable
SXTC/DKI payloads at 19:09:28.572621 UTC; these are present state, not historical
13:45 snapshots. With both SLOT switches ON and handoff OFF, restoring both SXTC
payloads leaves both legs vetoed through 19 recorded probes: zero open drafts,
payloads unchanged, SLOT reconstruction released. See `RECORDED_INPUT_BOUNDARIES.md`.
No actual SXTC both-leg placement is proven; unknown RPG ownership stays parked.

First candidate full run95cf: 7,181pass/58fail/489.71s,
19:12:10.162068--19:20:26.100075UTC, preserved at rebased-candidate-unit/.
FIRST_TIMED_PAIR.json retains all56 baseline failures plus two added failures:
test_momentum_gateway_handoff.py::test_handoff_crosses_into_a_separate_consumer_process
and ::test_dead_cross_process_consumer_raises_socket_drop_counter.
Raw output shows child processes re-entering pytest because the docs timing
launcher lacked a __main__ guard. Launcher corrected ONLY in receipt/harness
follow-up; all7 handoff tests pass19:21:10.922491--19:21:11.640242UTC.
Do not erase/waive first full run. Standard full candidate rerun now uses exact
baseline PYTHONPATH=src and python -m pytest command/capture, no timing launcher.
No source/tests/ops change; main failed-name comparison awaits its actual result.

Remote head95cf published to DRAFT PR1113, verified base994 at19:16UTC.
Remote Validate underway. Independent-review-pin FAIL is preserved: head95cf
has no committed review pin. No pin created or policy bypass attempted.

## Scoped NFQ composition

Fresh scratch checkout from rebased d9ac:
`/tmp/codex-slotclear-nfq-hotfix-composition-20261007`.
NFQ `0239726f` assessment applied as `fc504e67`; `16c435b0` implementation hit
six conflicts, so `0318ef84` was not attempted. Conflict diff and metadata are
preserved at `rebased-nfq-conflicts/`; cherry-pick was then aborted. Scratch
returned clean to d9ac. No manual resolution or conflicted-tree test is claimed.

Conflicts: `src/project_mai_tai/oms/service.py` (fresh vs held 0.5%/1% authority),
`src/project_mai_tai/strategy_core/schwab_1m_v2.py` (fresh-first vs NFQ identity),
`tests/unit/test_all_on_pm.py`, `test_expected_flags_check.py`, `test_keeprest1.py`,
`test_mirrorhold1_retained_hold.py` (shared all-on/catalog counts).
Both existing NFQ worktrees remain untouched. Joint integration needs parent
review; no new combined NFQ performance or full-source readiness is claimed.
