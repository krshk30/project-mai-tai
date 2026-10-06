# Four-Item Install Policy Checkpoint

Branch: `codex/1006-after-close-install-plan`, from handoff `63a06082`.
Application candidate remains exactly `c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`,
tree `e01851ac630ebf425de655c5c09dc11ae3c0304e`; this branch changes only plan,
local read-only gate policy, tests and evidence. No application/reconciler code,
shared handoff/C-row, production environment, ledger, service or broker order
is changed. No staging on the box, install, push, merge or extra restart.

## Controlled Policy, Not Observed Positive Activation

The October6 operator decision is the authorization input for IPDN1000 on
`live:schwab_1m_v2`. The positive fixture is explicitly CONTROLLED: direct
long1000/short0, fresh source/local account-book1000, managed/virtual0, exact
current-session bot net0. It models the current reconciliation source's
`position_quantity_mismatch` info schema, `broker_only_manual`,
`manual_not_ours`, account1000/quantity_delta1000/fill_delta1000 and
virtual/managed/our_quantity/net_fill_balance0, with the source's literal title.
This does not certify that classification was historically correct or prove
zero bot activity. The operator decision, not that classification or net-zero,
is the ownership basis. Any different finding shape remains blocked/UNKNOWN.

The helper retains complete bounded fill-balance groups with BUY/SELL totals,
including +127/-127. It prints `NOT_operator_only_from_net_zero` and
`broker_flat=false`, and returns a structured `dated_operator_residual`.
Only after all exact checks pass are that one position count and its exact
finding counts adjusted in an input copy for the existing general preflight.
Raw broker/books, findings, fills and summary remain unchanged in the receipt;
no ledger, reconciliation or environment write occurs. MI180/NXL2 admission
identities/current-session restrictions remain unchanged, and their original
116 controls still pass. When the residual is present, their audit explicitly
says symbol-broker-flat rather than falsely claiming the whole account flat.

Own actual12:59:25ET capture:
`/tmp/roundup1-ipdn-residual-shape-20261006.json`, SHA256
`8cd374a1f7b194745b185934e827e374b06a4093df0b97b49122d608a0e9d4ad`.
Direct Schwab132; account/managed/virtual Schwab132 and Webull66; bot net132/66;
OLOX accepted working order/submitted intent and two stuck warnings; no IPDN
residual finding. The test retains an exact recorded blocker subset inside
otherwise controlled context, not a fabricated positive1000 historical case.
Both actual quantities and OLOX remain blockers. No second Webull direct-read
claim is made from that capture: its66 is stored book evidence.
**Positive live activation remains UNMEASURED.** Parent owns shared reporting.

## Verification

Local controlled command, bytecode/cache disabled and correct venv PATH:

```sh
env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. PATH=/Users/velkris/Projects/project-mai-tai/.venv/bin:/usr/bin:/bin /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -p no:cacheprovider docs/review-artifacts/roundup1/job -q --tb=short
```

Final result: **197 PASS**, 0.66s: unchanged116 controls,77 IPDN controls and
four plan-contract controls. The collector positive is entirely fake broker/
SQL/overview/clock I/O and checks READ ONLY SQL, preservation of zero-net fill
groups, raw holding1000 and residual-not-flat output. There is no positive live
gate invocation. No full application suite is rerun or claimed by this lane.

Named load-bearing tests:

- `test_controlled_operator_rule1000_preserves_roundtrip_not_operator_only`
- `test_recorded_125925_ipdn132_66_and_olox_census_remains_blocked`
- `test_ipdn_coherent_nonzero_bot_fill_still_blocks`
- `test_ipdn_single_unowned_sell_finding_never_uses_residual_allowance`
- `test_collector_direct_read_failure_has_no_cached_fallback`
- `test_controlled_collector_preserves_zero_net_activity_and_reports_residual_not_flat`
- `test_candidate_catalog_has_actual151_process_checks_including_both_orb_consumers`

New `ipdn_mutation_probe.py`: **8/8 assertion RED**, no collection/setup errors;
date, direct1000, account1000, source freshness, bot net0, bot books0, pending
rows and matching-finding net0. Rechecked existing off-hours probe: **11/11
assertion RED**. Raw summaries:
`/tmp/roundup1-ipdn-policy-mutations-20261006.json` and
`/tmp/roundup1-offhours-policy-mutations-20261006.json`.
Ruff, `git diff --check` and existing mechanics `bash -n` PASS.

Read-only candidate catalog check binds actual c21 Git objects, not this plan
branch's old installed-source copy:130 boolean fields/143 process checks,
five numeric fields/eight process checks, total151. Both ORBPURPLE consumers
are checked. This is not a live151/151 receipt. The updated plans bind exactly
three new env keys and v2/OMS/orb-schwab's single scoped restart. T43 and
MIRRORHOLD Step0 are excluded; paper and all other application identities stay.

## Release Still Unready

No complete immutable attended runner/manifest/approval release, installed
daily wrapper/gate/units, fake full-sequence rehearsal or box receipt is
supplied by this checkpoint. Existing clock/retry mechanics are a sourced
component only. Unmodified OMS/v2 restart gates may reject a real IPDN residual;
that remains a separate explicit admission blocker, not an implied waiver.
Their clock-only reason must not falsely assert broker flatness. No armed
override, replayed admission or automatic recovery is authorized.

The old orb-schwab row47 shutdown failure is not blanket reset permission.
Only its exact previously reviewed cause after the intended stop and MainPID0
could qualify; unreadable/nonzero PID, a different exit or a startup failure
blocks. Exact reset/stop-signature mechanics are not implemented or tested here.
Final release assembly must bind those reviewed bytes, phase-aware health,
three-service snapshots/flags/log ranges, unchanged identities, exact fresh
post-close census and the checks-only daily timer before staging is considered.
