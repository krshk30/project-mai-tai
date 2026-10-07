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
No force-push has been performed: original remote PR head may still be d68.

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

Exact-main full baseline is running separately at
`/tmp/codex-slotclear-sell-current-main-994-20261007`. Receipt initially lives at
`/tmp/slotclear-current-main-994-unit-receipt`, to be preserved in this folder.
Candidate full suite follows baseline completion to avoid overlapping their
60-second HOTFIX benchmarks. Neither old-base pair nor focused results is a
replacement for the actual main failed-name comparison.

Exact-main baseline completed 19:03:43.202843--19:11:24.386758 UTC:
7,114 pass /56 fail, 455.80s pytest time. Raw receipt retained in
`rebased-main-unit/`. Both retained ON/OFF HOTFIX benchmarks passed in that run.

Present-ticket supplemental controls: 11 pass. Read-only pull retained 7 durable
SXTC/DKI payloads at 19:09:28.572621 UTC; these are present state, not historical
13:45 snapshots. With both SLOT switches ON and handoff OFF, restoring both SXTC
payloads leaves both legs vetoed through 19 recorded probes: zero open drafts,
payloads unchanged, SLOT reconstruction released. See `RECORDED_INPUT_BOUNDARIES.md`.
No actual SXTC both-leg placement is proven; unknown RPG ownership stays parked.

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
