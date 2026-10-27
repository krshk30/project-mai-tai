# L9 Observation Contract: Source Handoff, All New Controls UNRUN

Task L9-WBATTACH-TERMINAL-SCAN-20261022; same sole WB source author.
Parent d196df3df221a8b79f444a8cc90f72f422a3c157, immutable foundation.
Worktree /private/tmp/mai-tai-l9-wbattach-terminal-observation-20261022.
Branch codex/l9-wbattach-terminal-observation-20261022.

## Ruling And Causal Assessment

Human main clarification and fully read Claude TOP2026-10-22 10:37:16 explicitly
resolve the earlier d196 blocker. A validated CANCELLED/REJECTED sibling row
establishes that it was already terminal by its own acquisition. This is an
OBSERVATION UPPER BOUND, NOT an exact native terminal event timestamp. Both own
row acquisitions must be STRICTLY AFTER winning filled_at. A pre-fill page cannot
borrow another page's completion/acquisition. Native cancel time stays UNKNOWN.
This supersedes the earlier requirement to obtain a native cancel event clock;
it does not supersede missing actual packet/native acceptance or other guards.

Claude reports actual STAK Tcancelled0/Sfilled189@1.46 at14:11:49.025Z with both
rows read later. Those are reviewer-reported facts, not a raw packet inspected by
author. This source is intended to settle that exact actual case IF every existing
identity, strategy/account/journal/managed/economic fill precondition also holds.
No actual account lineage, full UUID or native receipt is invented from prefixes.

## Source Changes And Call Graph

_sync_broker_state_pass (UNCHANGED) -> existing position refresh/false-flip signal
-> reconcile_cached_protection_intents -> ONE asyncio.to_thread worker -> existing
nonblocking service sweep lock -> bounded8-intent query/frozen ceiling -> new
bounded refresh preparation -> existing native terminal/economic settlement.
No HTTP runs on the OMS event loop. Position/signal ordering remains unchanged.

New terminal-only peek copies current complete, nonfailed generation independently
of age. It retains each row's own acquisition and binds the scan session to the
row's acquisition date; it does not expire proven terminal rows merely because
today changed. A newer started/failed/incomplete scan cannot borrow the previous
generation's completeness. Ordinary peek/read freshness and normal two-page
reader behavior remain; ordinary reads do not gain age-independent authority.

Webull cached pair validation keeps the native validator, both exact coids,
distinct native IDs, requested quantity/status/filled aliases and symbol/side.
It rejects partial/overfill/working/ambiguous winners, requires same generation,
nonfuture own acquisitions, and EACH own acquisition > winner filled_at.
The proof records terminal_observed_at with explicit
acquisition_upper_bound_not_native_event, native_terminal_at=None and
native_terminal_time_state=UNKNOWN for zero-filled cancelled/rejected siblings.
It never manufactures a native cancellation timestamp.

The settlement helper independently checks contract tag, same generation/account,
complete/nonfailed/nontruncated rows, observation/acquisition equality, nonfuture
clock and strict post-fill ordering. Original entry/managed/journal/combo/quantity,
zero-vs-existing-fill, closed managed row, unique economic order/fill, parent,
strategy/account/symbol/side/quantity/price/filled-time/broker-fill-ID checks remain.
Only the matched protection intent lifecycle/receipt is updated, as before.
Economic writer/service.py is byte-UNCHANGED (cab807ea...). No fills/VP/PnL writes,
reparenting, broad UPDATE, native cancellation or live order action is added.

## Refresh Budget And Worker Ownership

Existing instantiated Webull adapter exposes a physical key (host,account_id).
Routing forwards only to an ALREADY instantiated Webull provider; no factory.
Each scope's frozen-ceiling sweep retains its physical-account claims across all
8-intent batches. Aliases of one physical account cannot obtain another attempt.
Claims are recorded BEFORE refresh and remain through errors, budget denial,
typed/returned429, cancelled awaiting task and failed commit. Failed commit retains
the tentative original sweep ceiling without publishing cursor progress. Claims
clear only on successful completion of that entire sweep, not after a batch.

Bounded candidate identities are copied before HTTP; the read transaction is
rolled back before the first refresh (no mutations exist yet), so no DB transaction
is held over native I/O. Expired ORM state is reloaded for settlement afterward.
Malformed legacy metadata is skipped without starving later cursor rows.
No pending selected intents means no refresh; no cached-proof miss means no refresh.

refresh_one_page is <=1 budgeted list HTTP attempt, never a two-page hidden read.
Incomplete traversal retains cursor/acquisition/generation for later sweeps and
cannot settle. Common endpoint budget covers attempts from aliases/ordinary reads.
Returned429 (foundation) and typed SDK429 (new wrapper) both establish shared30s
conservative application backoff. No vendor quota claim, DETAIL fallback, automatic
retry or new provider implementation. Budget-denied attempts conservatively consume
the sweep's opportunity even if no HTTP was possible; retry is a future sweep.

The25ms budget remains cooperative: it bounds work between candidates/refreshes,
not wall duration of an in-flight SDK request or shared reader lock wait. This
source does not claim a hard25ms deadline, new performance PASS or quiet-host proof.
The worker owns the service lock through commit even after caller cancellation.

## Tests And Fixture Disclosure

NEW test_l9_wbattach_terminal_observation.py:11 functions/24 hand-enumerated inputs,
UNCOLLECTED/UNRUN. Together with foundation10 there are34 newly authored inputs,
not a collected count or PASS receipt. New controls cover:

- STAK-shaped189@1.59 entry /189@1.46 stop exit at2026-10-22 14:11:49.025Z,
  cancelled-zero target, closed managed row and actual economic-wrapper-created
  fill. All full identities/base/combo/native IDs are explicitly SYNTHETIC. Setup
  data is adapted before invoking the unchanged economic wrapper, not reparented
  during settlement. Economic rows are snapshotted before/after bookkeeping.
- Old complete post-fill rows settle; ordinary fresh peek still rejects age.
  Missing scan obtains exactly one worker-thread fake list response then settles.
- Pre-fill/equal/sibling-old-page acquisition, working/partial/overfill,
  failed/incomplete/future proof never settles; missing/mismatched economic fill
  or reopened managed row remains UNKNOWN with identical economic snapshots.
-20 pending synthetic intents/three batches/two physical accounts/alias prove
  one list attempt per physical account per WHOLE sweep, partial cursor across
  sweeps and no HTTP on the loop; commit failure/cancelled waiter retain claims.
- Returned/SDK429, no pending/unmatched scope, no provider construction, and
  malformed legacy front rows cannot relax proof or block later cursor progress.

An earlier AUTHOR-OWNED fixture in test_wbattach1_terminal_bookkeeping.py gets a
strictly post-fill observation clock (+1ms), not a fake cancel timestamp, and an
explicit503 fake page instead of any real SDK. Existing assertions are unchanged.
Its historical damage="stale" negative now explicitly retains pre-fill/equal
acquisition: age alone is no longer UNKNOWN under the new ruling. The new aged
post-fill positive asserts that intended policy difference. This fixture input
correction must be reviewed; no existing assertions/skips are weakened/removed.

Main-only proposed NEW review/claim selection (NOT EXECUTED):

```sh
python -m pytest -q tests/unit/test_l9_webull_scan_acquisition.py tests/unit/test_l9_wbattach_terminal_observation.py tests/unit/test_wbattach1_terminal_bookkeeping.py tests/unit/test_wbattach1_bounded_bookkeeping.py tests/unit/test_webull_list_primary.py
```

Then source-based wider WBATTACH/routing/economic-preservation/composed full gates
remain main-owned and separately claimed; no borrowing L7's79/a910's290/foundation
source clearance as successor qualification. Actual STAK packet replay/native
post-install autonomous8ea0e126 acceptance still required. POMbb1feb75 remains its
separate guarded SQL path; no POM rehearsal/replay/SQL execution or migration.

## SHA256 And Restrictions

- broker_adapters/webull_order_reads.py:
  1338b51da302b071da5acf7f6c7e52366c2d133ef560a902d95f121515972408
- broker_adapters/webull.py:
  f42dc190fa4b4517b0802b40ed999cf7914fcad3934dea8dd6a65289c5f9de5b
- broker_adapters/routing.py:
  f10e8e2b6a35b27e9347eef94d893fde579e57411a6a6b48f71803468bbdd3ac
- oms/webull_attach_riskoff.py:
  6e5b728c2a57036de34baa6fdf436e160cf779bc8b8337f7b644cd5ceca45685
- tests/unit/test_l9_wbattach_terminal_observation.py:
  381703d27ea617465435f8adfde8e621d6652086920e1230594783379df712c7
- tests/unit/test_wbattach1_terminal_bookkeeping.py:
  f13f77d3609640bdba7f8468061d67836477776af5e712a99ace9ea104cb6546

No worker imports/tests/AST/compile/parser/app/DB/provider/SSH/runtime/orders,
push/install/restart, no shared queue/status/LATEST or other owner file writes.
Frozen d196/a910/L7 worktrees unchanged. Static diff inspection is not PASS.
Exact head, counts and bundle pin reported separately after commit.
