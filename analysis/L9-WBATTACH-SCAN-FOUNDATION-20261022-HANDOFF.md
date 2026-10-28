# L9 Scan Foundation: Partial Source Handoff, Not Settlement Acceptance

Task: L9-WBATTACH-TERMINAL-SCAN-20261022. Same sole WB author.
Base: a91021579740ce564b1348715ccbdb782cda17e3 (frozen, unchanged).
Worktree: /private/tmp/mai-tai-l9-wbattach-terminal-scan-20261022.
Branch: codex/l9-wbattach-terminal-scan-20261022.
Status: preparatory source/controls only; terminal-time contract BLOCKED.

## Source Assessment

Read Claude10:22:51 full card and main clarification: ONE fresh list read means
<=1 HTTP attempt per PHYSICAL account per WHOLE cursor sweep, not per intent,
not per eight-intent batch, not two pages hidden in one method invocation.
The current ordinary TodayOrderReader.read can consume two pages. It retains
per-row acquisition clocks across pagination, resets completed/failed scans on
a later cycle, and rejects conflicting identities/no-progress/max20 traversal.
peek_complete requires a complete current-session nonfailed scan and <2s rows.
Both cached_attach_pair_terminal and settle_cached_protection_intent independently
enforce the age gate. Existing general exit reads/freshness must not be relaxed.

Read full cached pair/settlement helper and bounded sweep, routing cache-only
forwarder, query budget/ordinary/detail proof reader, sync placement after
positions/false-flip signal, _persist_oco_exit_fill, _owned_exit_detail_matches,
_close_resolved_oco_managed_row and existing terminal/bounded/list-primary tests.
Settlement currently preserves exact native pair identity/quantities/statuses,
entry/journal/combo/managed binding and existing order/fill identity/price/time.
It does not create/reparent economic rows. Those source functions are unchanged.

## Missing Provenance: No Guessed Sibling Clock

The inspected native execution validator supports last_filled_time/lastFilledTime
for executions. No inspected source/fixture supplies a validated cancellation or
rejection terminal timestamp contract. A scan's final completion time cannot
re-date the older page containing the other leg. A newly read terminal status
has an observation/acquisition time, but this must NOT be silently called its
native terminal time or used to claim an older row was acquired after the fill.

Actual STAK8ea0e126 full strategy/account/entry/managed/fill/scan packet is pending
from Claude through main. Reported189 stop@1.46 /14:11:49.025Z, native combo and
order53300156 are reviewer-reported facts, not a raw receipt inspected by author.
No full UUIDs/native IDs/terminal timestamp fields are reconstructed from prefixes.
Cancelled/rejected native terminal time remains UNKNOWN; no acceptance from it.

Needed before wiring age-independent settlement: actual sibling terminal-time
field/source/semantics in the existing native packet, or an explicit reviewed
ruling defining sufficient terminal-observation proof without inventing a native
clock. No such disposition is inferred by this commit. Therefore this head does
NOT wire refresh into OMS or switch settlement to an age-independent proof.
It does NOT fix or qualify STAK autonomously yet. No weakening of the old gate.

## Committed Foundation

- New TodayOrderReader.refresh_one_page uses the SAME read traversal and budget,
  with a one-HTTP-attempt limit. Partial cursor/pages continue on later cycles;
  complete-before-proof behavior, max20 traversal and ordinary two-page reads
  remain. No alternative provider implementation, detail fallback or new endpoint.
- A returned HTTP429 attempt remains counted and activates a conservative30s
  application backoff on the shared list-today endpoint budget. Other aliases/
  accounts sharing that budget cannot bypass it; DETAIL remains independent.
  This is not a claim about vendor quota or SDK-exception handling (new API is
  unwired; eventual adapter caller must also defer typed429 exceptions).
- Each scan has a new process-local generation; evidence includes that generation
  and failed flag. New generations never borrow the previous scan's completeness.
  Native row data is deep-copied when retained so caller mutation cannot rewrite
  its provenance. Each page keeps its own original acquisition timestamps.
- This is a scan support API, NOT the per-sweep claim. A future reviewed caller
  must own the once-per-physical-account claim across batches/aliases, preserve
  eight-intent/25ms cooperative fairness, lock ownership/cancellation, frozen
  cursor ceiling and commit-only progress. No such claim is falsely supplied here.

New test module:8 functions/10 hand-enumerated inputs, UNCOLLECTED/UNRUN. Controls
cover <=1 HTTP per explicit refresh, unchanged two-page ordinary reads, cursor
continuation, unchanged older-page acquisition, generation invalidation, copied
rows, HTTP/malformed/conflicting page failure, budget denial and returned429
shared backoff. These are synthetic reader controls, not complete STAK rows,
economic settlement proof, sweep once-per-account acceptance or native replay.

## Next Wiring Review Contract

After terminal provenance is resolved: add an isolated terminal-proof peek without
changing ordinary peek/read freshness; require full same-generation complete /
nonfailed/nontruncated rows acquired AFTER winning fill AND proven sibling
terminal time (not scan completion). Exact identity/quantity/working/partial/
overfill/economic guards stay. Split eligible bounded DB traversal from I/O;
use existing instantiated Webull provider/physical scope and <=1 page per physical
account per entire sweep, mark attempts before call, keep claims through failure/
cancel/commit retry, no per-intent HTTP loop. Pending-account-only routing must
not instantiate a provider, query POM dump, or broaden lifecycle eligibility.

Then author faithful189@1.46 STAK-shaped controls with explicitly SYNTHETIC full
identities and labelled terminal-clock controls; actual packet still required
for acceptance. POMbb1feb75 remains explicitly on its separate SQL path.

## Pins And UNRUN Gates

SHA256:
- src/project_mai_tai/broker_adapters/webull_order_reads.py:
  339e8933315d26cd96253f2df1e89caf886950807dca1aac27feb2d739bb44ee
- tests/unit/test_l9_webull_scan_acquisition.py:
  34063d532520616fca24409d7395e9fd4188930c70299fdcb5c8501b70b5c1b2

Proposed foundation-only main command after new exact-head/source/claim review,
NOT EXECUTED:

```sh
python -m pytest -q tests/unit/test_l9_webull_scan_acquisition.py tests/unit/test_webull_list_primary.py tests/unit/test_wbattach1_terminal_bookkeeping.py tests/unit/test_wbattach1_bounded_bookkeeping.py
```

No worker test/import/compile/AST/parser/app/DB/provider/SSH/order/replay/push/
install/restart. No shared queue/status/LATEST writes or frozen L7/a910 edits.
Static whitespace/Git inspection is not PASS. Actual focused/preservation/full,
economic acceptance and post-install autonomous8ea0e126 remain UNRUN/pending.
Exact head, numstat and bundle pin reported after commit (no self-hash).
