# Connected173 study reconstruction and differential (UNRUN)

## Pure owned interfaces

`firstbar_study.reconstruct_study_cases(seed, buy_bytes, buy_source, packets,
execution_policy=..., account_brokers=..., strategy_code=...,
live_by_order=...)` returns one `ReconstructedStudyCase` per selected reference
key, including exact account/order legs, reconstructed inputs, issues and
explicit assumptions. No provider, DB, native call or live/table write.

The builder uses original account/client-order IDs and quantities. It reproduces
the reference180-second cross-account grouping ONLY to join the seed, then
decomposes that group into independent account identities. It never deduplicates
economic legs by symbol/minute or relabels a group key as a native flip ID.

Raw tape rows retain Decimal prices, integral quantities and condition columns;
represented decimal timestamps become exact integer nanoseconds at the source's
precision. File-line IDs are DERIVED reference-event IDs, not recovered provider
IDs. Same-millisecond ordering is preserved file order, not native-arrival proof.

Completed one-minute bars are rebuilt causally from typed-eligible prints, with
no neighbor deletion or missing-minute flat fill. The vendored pure oracle must
hash22ead27b..., seed=sma5/period5/factor3.5. A reference fill-minute proxy is
explicitly named `REFERENCE_FILL_MINUTE:<original-order-id>`, not an asserted
historical ATR arrival. Its completed HIGH is known only at minute end. A later
oracle SELL is available at its bar start+60s, never at its bar start. Session
acquisition/holding ends20ET; the15:45 cutoff controls new entries, not a forced
16:00 exit. Unknown bar/route/coverage operands remain UNKNOWN, not skips.

`firstbar_comparison.compare_firstbar_case(reference_case, result_tuple,
mode=..., bindings=...)` requires ALL explicit per-account episode bindings.
It returns per-field differences for quantity, entryVWAP/time, exitVWAP/time,
reason family and cent-reported PnL; unrounded dollar delta also survives.
Missing/open/unknown economics do not become zero. Only an affirmative complete
no-trade has zero exposure. A MATCH label is COMPARED_FIELDS_ONLY, not native
economic/queue/installation acceptance. Reference lineclose is a broad family,
not proof distinguishing an actual confirmation from an actual SELL event.

Ten NEW controls: five in `test_atr_simulation_firstbar_study.py`, five in
`test_atr_simulation_firstbar_comparison.py`. All UNRUN by worker; no old controls
requested again. These are CONTROLLED mechanisms, not the173 study acceptance.

## Main-only source-ready runner

`tools/atr_firstbar_study_replay.py` connects the actual pinned CSV/X3/selector
and original buys.psv to the builder, cohort engine and differential. It processes
one symbol's ALL dates together, so an old unresolved position cannot silently
reset on a later day. Both modes share per-world physical volume across accounts.
Every selected reference case remains in the final receipt, including unknowns;
all seven reference exclusions remain separate. No reference script is imported.

Proposed invocation, NOT executed/authorized by this author:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. \
  /Users/velkris/Projects/project-mai-tai/.venv/bin/python \
  tools/atr_firstbar_study_replay.py \
  --reference-root /private/tmp/claude-502/-Users-velkris/1c6e03c9-4925-4f24-98e6-303999cfcd67/scratchpad \
  --policy-file ACTUAL_APPROVED_RETAINED_55_RULE_FILE \
  --policy-sha256 ITS_EXISTING_VERIFIED_SHA256 \
  --account-brokers '{"live:orb":"webull","live:schwab_1m_v2":"schwab"}' \
  --strategy-code schwab_1m_v2 \
  --coverage-manifest ACTUAL_REVIEWED_STUDY_COVERAGE.json \
  --output /private/tmp/UNUSED-FIRSTBAR-173-REPLAY-OUTPUT.json
```

Coverage manifest is a source-hashed array of `{symbol,day,sha256,start_ns,
end_ns,complete,gaps,evidence}`. `complete` is exact bool or null; bounds are
integer ns. Every true declaration needs its actual evidence reference. The
manifest's byte hash and row pointer are retained. It may describe the approved
retained STUDY envelope; it is not a native-orderbook/arrival completeness claim.
No original HTTP envelope is demanded just to label this reconstructed study.
Missing declarations cause explicit UNKNOWN coverage, never inferred completeness
from first/last timestamps, file size or a copied code comment.

Policy file/hash are the EXISTING Main-approved retained55 ex-post snapshot,
not a new retrieval or the controlled three-rule test fixture. Main already owns
that policy reference; this runner preserves the approved ex-post/unknown-time
label. Original individual LIVE entry/exit facts are not synthesized from grouped
study economics. The runner preserves provided LIVE fields and labels individual
native facts UNKNOWN; V2 can pass already-qualified per-order facts through the
pure builder's `live_by_order` boundary without changing LIVE economics.

## Acquisition-source correction / remaining proofs

New static evidence: `fbh/mfetch.py` SHA
`94e0818ff378d5b2001c3fbefd52209083caedd6d8220216dc00a32cf3697d0a`
filters size>=100 before writing `.t`, requests04..20, emits rounded3-decimal SIP
seconds and omits provider IDs. `fbh/fetch.sh` SHA
`9af0130dfe4d0af3fa4d6b0c65548804e7988c7b199f2d7ec1e4ab87ccae3b00`
also filters size>=100 and rounds event_ts to3 decimals. Thus the absence of an
exit-loop size check alone does NOT prove oddlot fills in these delivered files.
Typed condition eligibility can still differ from the reference hardcoded list;
the new bar feed differs as well and must be reported per affected case.

Those fetch scripts are STATIC references only: NEVER execute them. Nonempty
file skip logic does not independently prove a completed acquisition. Per-file
bytes/coverage evidence, source-order precision limits, true native flip/watch
arrival, native queue priority and any missing individual native exit remain
distinct. The latter native facts are not new-provider requests or requirements
for inventing them. Real173 engine results and mandatory samples are UNRUN,
not qualified by the already-matched provided sums231.45/257.54.

The21:08 checkpoint remains a conditional source/comparison-readiness target,
not a promise of runtime acceptance. No worker imports/tests/compile/AST/replay,
PG/provider/SSH/runtime performed. V2 store/UI/migration paths, LIVE and old
VOLBODY remain untouched.

