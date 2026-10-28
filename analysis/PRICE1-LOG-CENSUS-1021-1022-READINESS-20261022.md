# PRICE1 Replacement Census: Recorded Logs / Readiness

Claude15:51:27 explicitly WITHDRAWS10-12: warning path did not run/0ADD.
Use10-21 and10-22 instead. Earlier V1/V2/V3 contracts remain historical source,
not an ongoing10-12 build gate. This change is tools/tests/docs ONLY aftere6fe0b6b;
production remains byte-identical to5296267598b591ea465103fb58e4a4aee1a5dce4.

## Delivered Identity / Actual File-Only Inventory

Directory /Users/velkris/.codex/task-queues/analysis/PRICE1-CANDIDATE-CENSUS-INPUTS-20261022.
SHA256SUMS agrees with independently calculated SHA256 for BOTH files:

- momentum-paper_2026-10-21.log:
  152f46d2477c778ab88a845e5427bd32e5856621cc689968230072a7f886ff1a.
- momentum-paper_2026-10-22_to_1552.log:
  a241c662279b05b1b0de87f35bab35e104650c91977dd8611873017d774a9eda.

Bounded awk inspection of actual recorded lines, NOT application/model execution:

| File Day | Lines | Process starts | ADD | DROP | Peak logged transition set | First peak (literal log clock) |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
|10-21|184|2|46|42|5|20:21:16,029|
|10-22|412|5|78|72|5|08:15:24,021|

Method: maintain unique ADD-minus-DROP names; reset on logged process start.
No duplicate ADD/orphan DROP in these delivered transitions. The reconstructed
sets still have2/3 members at EOF respectively; these are NOT native open orders.
Raw logged peak5 is11 below16, but this is NOT old/new modeled maxima or proof
that the owner union never hit cap. Missing REFUSED/IGNORED decisions, unlogged
coverage, active paths and symbols excluded by either gate can change that claim.

Actual provenance discrepancy:10-21 has NO POM line (all184 lines examined by
bounded symbol search);10-22 has13 POM ADDs. First is line6 at08:00:03,753,
high3.01; later examples line25 high1.86 and line33 high2.37. Claude's POM
prior-close0.66/both-days assertion is declared evidence, not a prior-close map
contained in these logs. Do not claim observed POM admission on10-21 from it.
Finite clarification: identify the10-21 POM warning/aggregate reference or confirm
that 'above1 both days' is external price context, not an ADD assertion.

10-21 start lines1/143, ready2/144, STOP142/184.10-22 start1/135/168/201/234,
ready2/136/169/202/235; do not stitch RAM state over those boundaries.
10-21 literal range07:47:16,508..22:10:00,778;10-22
07:40:11,161..19:49:29,411. No timezone is embedded in the logging formatter.
The filename1552 is not proof of a last15:52 bar, receipt clock or completeness.
Caller must declare log timezone (UTC is consistent with07:55 session readiness
and04:00ET feed, but not independently host-attested by this packet).

## Exact Source Semantics / Limits

services/momentum_paper_app.py::_log_warning_transition logs ONLY ADMITTED/DROPPED.
At ADD, trigger_price=bar.high; at DROP it reuses the stored ADD high. It does not
log bar.close, complete prior closes, retained/refused decisions, all A/T, raw
subscriptions/received times, actual settings or owner union. Thus a logged
high>=1 does NOT prove close>=1. A valid aggregate high<1 proves close<1 only
for that ADD bar, not later retention/drop/current quotes. Source as currently
authored is not an attestation of the installed historical consumer head.
warning_window.WarningCandidates counts the union with active path symbols and
has separate candidate/owner caps. DROP lease/cooling and path lifecycle can
change when the gate changes; deleting POM from a logged set is NOT an old-leg
replay and cannot recover the new counterfactual competitor stream.

## New Standalone Normalizer / Main-Owned Command

tools/price1_log_census.py imports stdlib ONLY and reads one pinned file <=2MiB,
lines<=64KiB, stdout JSON only. It emits every minute between literal first/last
record, transition line references, restart/session/stop boundaries, duplicate/
orphan anomalies, logged active-set peak/end and peak-minus16. No app import,
model, native transport, provider, DB, subprocess or log writes. Missing prior
map/bar.close/model states produce null old/new counts, maxima/newly-entering
symbols and UNKNOWN cap verdicts. Optional provenance-bound prior-close values
support per-ADD gate predicates ONLY; never full admission. POM prior.66 gives
old-gate refusal and potential new-gate expansion, NOT confirmed newly entering.

After source review, main may invoke twice (no worker invocation occurred):

```sh
PYTHONDONTWRITEBYTECODE=1 /Users/velkris/Projects/project-mai-tai/.venv/bin/python /private/tmp/mai-tai-price1-buy-floor-20261022/tools/price1_log_census.py --input /Users/velkris/.codex/task-queues/analysis/PRICE1-CANDIDATE-CENSUS-INPUTS-20261022/momentum-paper_2026-10-21.log --sha256 152f46d2477c778ab88a845e5427bd32e5856621cc689968230072a7f886ff1a --log-zone UTC --prior-close POM=0.66 --prior-source Claude-155127-declared
PYTHONDONTWRITEBYTECODE=1 /Users/velkris/Projects/project-mai-tai/.venv/bin/python /private/tmp/mai-tai-price1-buy-floor-20261022/tools/price1_log_census.py --input /Users/velkris/.codex/task-queues/analysis/PRICE1-CANDIDATE-CENSUS-INPUTS-20261022/momentum-paper_2026-10-22_to_1552.log --sha256 a241c662279b05b1b0de87f35bab35e104650c91977dd8611873017d774a9eda --log-zone UTC --prior-close POM=0.66 --prior-source Claude-155127-declared
```

UTC here is an EXPLICIT declaration pending host receipt, not inferred as-known
arrival. Main chooses fresh immutable stdout paths and pins source/head before
execution. Expected cost one pass over~106KiB plus<1440 minute rows/file;
actual cost UNMEASURED. Ten new synthetic parser controls authored, ALL UNRUN;
these are not native/model qualification. Existing127 intended controls retained.

## Finite Existing-Artifact Request / Honest Acceptance

Do NOT fetch missing bars. For meaningful two-leg warning-model census, ask only
whether already-retained artifacts contain: full prior-close maps/versions,
all competing symbols'1s aggregates with close/high/volume/bounds and actual
published/received/consumer clocks, settings/session window, drop/epoch/sequence
coverage, complete-read heartbeats, initial/restart candidate+active-path/legacy
owner state and any required delivered T. Pin day/window/end completeness and
historical installed source separately. Where absent, per-minute old/new count,
max vs16 and exact newly entering symbols stay UNKNOWN (not0). A declared
counterfactual may still be useful under V3's explicit clock contract; never
label SIP or synthetic delivery native arrival/as-known. Log-only baseline is
available now and does not wait for impossible10-12 inputs. Main owns all actual
warning-model runs and acceptance. L2 critical capacity remains first.

## Corrected Model Driver v4 (Separate From Log Normalizer)

tools/price1_candidate_counts.py and price1_candidate_compare.py now require
PRICE1-CANDIDATES-v4 / PRICE1-CENSUS-RESULT-v4. An explicit session_date of
2026-10-21 or2026-10-22 is mandatory;10-12 is rejected. Both study bounds must
land in that declared ET date, with the original half-open, whole-minute,
<=1440-minute restriction. Validation-window session is either explicitly
unrestricted null (retained config) or the SAME declared day. No default date,
study bounds, closes, prefix, end coverage, late-bar timestamp or settings are
invented. Results expose session_date and the comparator refuses mixed sessions.
POM is the corrected case label; ALL competing symbols still enter the actual
model. V1..V3 source and synthetic10-12 controls remain frozen in earlier heads.

The existing54 synthetic census controls retain their assertions under an
explicit synthetic10-21 fixture date. Ten additional intended cases cover both
replacement dates, withdrawn/undeclared days,21-bounds-vs22-label, mismatched
validation session, oldv3 rejection, comparison session drift and a receipt
AFTER study_end that cannot backfill any quiet study minute. They are ALL UNRUN;
no actual log/aggregate was relabelled or converted to a fabricated receive clock.
New intended census-module total64; entire authored PRICE suite147 including
the10 separate log-parser controls. Counts are source-intended, collection unverified.

Main command shape for normalized ACTUAL or declared-counterfactual A/T inputs:
use the same external candidate_counts.py with each pinned baseline/current
PYTHONPATH, declared fullhead, exact expected-source-root and actual inputSHA.
Header must include the corrected session, actually supplied study_start/end and
all existing V3 clock/provenance/config operands. Then compare the two v4 results
with candidate_compare.py. The delivered text logs are NOT valid v4 model inputs
and must NOT be sent through the model driver. Missing1s bars remain UNKNOWN/no
fetch; log-only commands above remain the immediately available finite path.

## Single Offline Delivered-Log Census Runner (Main Only / UNRUN)

New tools/price1_recorded_census.py checks the EXACT two-entry SHA256SUMS
manifest and independently pins both full log contents through the stdlib-only
normalizer. It emits ONE stdout JSON receipt with both days, raw line/byte/hash
identities, source hashes, boundaries/anomalies and every covered log minute.
No application import/model/replay/provider/native/DB/subprocess. No authored
driver invocation, tests/import/compile/AST or additional file analysis by worker.

Per minute: observed_log_candidate_peak, observed_log_peak_symbols/end_symbols,
observed_log_peak_minus_16, transition_line_refs; separate null
old_prior_gate_candidate_count/new_close_gate_candidate_count/newly_eligible_symbols,
plus UNKNOWN_BAR_CLOSE_MISSING for newly_eligible_POM. Daily maxima and cap
comparisons follow the SAME separation. POM ADD records retain actual line refs,
logged aggregate high, prior predicate false under DECLARED.66, and UNKNOWN new
close predicate/full admission. These fields fulfill the executable observed-log
baseline without implying either counterfactual was run or reconstructed.
Ten additional synthetic receipt controls authored; entire intended suite157,
ALL new successor controls UNRUN (main's127 PASS belongs only toe6).

Executable command after main source review, from the source-owning worktree:

```sh
cd /private/tmp/mai-tai-price1-buy-floor-20261022
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m tools.price1_recorded_census --input-dir /Users/velkris/.codex/task-queues/analysis/PRICE1-CANDIDATE-CENSUS-INPUTS-20261022 --log-zone UTC --timezone-source 'explicit-UTC-declaration-consistent-with-session-log-not-host-attestation' --pom-prior-close 0.66 --prior-source 'Claude-155127-declared-POM-prior-both-days-not-exported-map'
```

Main binds this command to its exact reviewed head/clean root, redirects stdout
and stderr to NEW immutable artifact paths and records actual exit plus hashes.
No --head text is claimed as automatic Git attestation. UTC is a declared log
timezone ONLY, never A/T receive or consumer observation time. Full raw input
SHAs above are mandatory in code and receipt; a different capture needs explicit
source review, not a permissive manifest override. Expected work bounded to
two<=2MiB logs, one read each plus tiny source/manifest reads and minute output;
actual elapsed/cost UNKNOWN until main executes. New module selection is
tests/unit/test_price1_log_census.py +tests/unit/test_price1_recorded_census.py
plus the corrected whole test_price1_candidate_census.py, no control filters.

No absent minute/bar is filled with a quiet price or made-up delivery. If the
logs cannot support exact old/new census, the above UNKNOWN is the required
result, not a request for a new provider fetch or an indefinite blocked build.

Post-D3 quiet release source audit adds3 unit controls for the COMPLETE two-day
runner wiring, actual-file hash verification despite a correct manifest, and
date-label rejection despite matching pinned contents. Pins are explicitly
synthetic and monkeypatched only in those controls; no delivered logs modified
or executed. Runner/normalizer/production source unchanged after3a31b76e.
Recorded-runner whole-module intended count13; authored-suite intended160.
All successor controls remain UNRUN; e6's127 PASS is not borrowed as their proof.
