# Aggregate Confirmation / Sealed FIRSTBAR Compatibility

Source-only successor of `14b3e6198ad713d145f326a7388eb8298f748e55`.
Separate root `/private/tmp/mai-tai-l14-aggregate-sealed-compat-20261024`.
The14b producer root and running52847 are NOT modified or rerun.

## Cause / Exact Restoration

Main `/private/tmp/OCT24-MAIN-3FFE-SIM-INTEGRATED-FIRST-20261024.xml` reports
440PASS/78FAIL. File-only XML inspection groups all78 failures into seven
FIRSTBAR modules: cohort_job22, many_issues4, store_binding11,
study_publication37, reference_review_banner2, study_store_rows1,
two_group_binding1.68 failures directly report
`engine_source_pin_mismatch:types.py`;10 are expected-regex mismatches after
the earlier source-pin refusal. This is not evidence of model PnL error.

14b amended shared types.py's enum and shared firstbar_exits.py's class. The
legacy engine verifier checks types.py before the FIRSTBAR verifier reaches
firstbar_exits.py. Existing seals intentionally reject both changed sources.
Those checks are correct; they are not refreshed or bypassed here.

Both shared files are restored BYTE-EXACT to pre14b/cb44 source, not recreated
as a merely equivalent implementation. Static SHA and zero diff against cb44:

```text
types.py
90f8dfac9a732c1fd247e5c10f2af18c7d3360ef987f43826ce3b8f708cef4ef
firstbar_exits.py
ee38e47e7188c2dec75cb5f58edad7b897841cdb745ed4be8facaedccffb11d2
```

No engine_binding.py / firstbar_binding.py / target leaf / old fac9 metadata,
producer pins, receipt bytes or historical result labels are edited. Static
byte equality is not a runtime integration PASS; those controls are UNRUN.

## Aggregate-Only Extension / Runtime Reason Handling

Only firstbar_live_baseline.py introduces AggregateExitReason, a local str Enum
containing CONFIRMATION_EXIT, and AggregateBaselinePosition(FirstBarPosition).
The recorded-entry adapter constructs that subclass instead of the sealed base.

Before confirmation, with no confirmation, or with an earlier SELL/session
boundary, the subclass delegates to the unchanged original trade method.
Only a reached earliest confirmation boundary locally performs the SAME size,
price, strict causal-cursor, before-boundary and execution-eligibility checks,
then closes at the first eligible observed100+ print. It does not invoke the
parent trade handler again after advancing the cursor. Its local before/finish
methods retain14b cancellation/OPEN semantics. No target/stop math is forked.

The existing SimExit container is a frozen dataclass with a reason annotation,
not a runtime enum coercer or validator. The aggregate-local str Enum supports
the existing reason.value use, _close's cancellation string, JSON/asdict output,
and the identity check against the ORIGINAL SimExitReason.OPEN. Its serialized
value is CONFIRMATION_EXIT, not an Enum class-name string. Pure publication's
unchanged explicit V3 reason whitelist remains the validation boundary.
No sealed type/validator is weakened and no local reason enters legacy engine
entrypoints. New controls directly cover these runtime boundary assumptions.

## Policy / Source Provenance

The aggregate policy remains EXACTLY14b V3:
ATRSIM_RECORDED_AGGREGATE_ENTRY_BASELINE_V3_CONFIRMATION_EXIT,
C1_20261024_0000_ENTRY_BAR_SHORT_CONFIRMATION_EXIT. Entry-close evidence,
geometry policy,35confirmation controls, publication/projector APIs, three
buckets and cohort classifications are not revised by this compatibility move.

The producer member set remains exact17. No extra project module is imported;
execution_eligibility.py and exits.py are already17 members. Against14b, the
changed members are types.py/firstbar_exits.py (exact restoration) and
firstbar_live_baseline.py (local extension). Runner bytes are unchanged.

CRITICAL: any actual14b raw receipt remains bound to its original14b head,
original root and original17 source pins, including its then-modified shared
files. It must NEVER be relabeled as having run this successor. Main's pure
publication/read binding can consume that immutable generation using those
external original pins; current APP loader compatibility is a separate claim.
No new model execution is requested here. Any later run under this source
requires its own Main-reviewed manifest/head, authority and explicit claim.

## Controls / Broader Selectors (All Author UNRUN)

New `tests/unit/test_atr_simulation_aggregate_sealed_compat.py`:16 expanded
CONTROLLED controls: two exact byte hashes; both real sealed verifiers; unchanged
global enum; original FIRSTBAR target semantics and constructor; local reason
serialization/OPEN identity; exactly-once exit/no reopening; strict cursor;
two ineligible print cases; unknown execution; three input validation cases;
and aggregate-without-confirmation using the original target reason/price.

Changed implementation coverage: Main may review/run this new16 plus the35
confirmation controls on the successor (same policy, relocated executed path):

```text
tests/unit/test_atr_simulation_aggregate_sealed_compat.py
tests/unit/test_atr_simulation_aggregate_confirmation_exit.py
```

Broader affected regression selectors corresponding exactly to the seven failed
modules in the actual3ffe XML, not a model or performance run:

```text
tests/unit/test_atr_simulation_firstbar_cohort_job.py
tests/unit/test_atr_simulation_firstbar_many_issues.py
tests/unit/test_atr_simulation_firstbar_store_binding.py
tests/unit/test_atr_simulation_firstbar_study_publication.py
tests/unit/test_firstbar_reference_review_banner.py
tests/unit/test_firstbar_study_store_rows.py
tests/unit/test_firstbar_two_group_binding.py
```

The existing original3ffe raw78 failures remain retained. No worker tests,
imports, AST/compile, model/reference execution, provider or PG action occurred.
Main review, local reason controls, broad sealed-loader regressions and actual
14b successor outcome/C1 validation remain distinct pending gates.
