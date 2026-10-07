# Parent Mechanics Review

As-of 2026-10-06 20:43 ET, codex-2. Local assembly only; not staged or executed
on production. Application candidate remains frozen at
aed1a358c0da4043ce248550c2b1d02cecdc76ff, pending batch review/pin and merge.
No final application binding, release manifest or approval is asserted here.

Imported runner commits 065a3fab3bf62a16fb79c9a419f9e9c8bf80dbfa and
eb56c5287a6a930a3af0cbbbc8b4dd214bebddf9 as parent commits1620edd3/878dceec.
Own read found Sequence.run rechecking the date after the first service stop.
This violated the approved completion-through-clock-change rule. Removed that
later fence; initial preparation and immediately before the first v2 stop still
check the date/window. Trading, freshness, identity and census gates unchanged.

Tests added:
- test_first_stop_clock_fence_does_not_abort_stopped_sequence: isolated clock
  control crosses midnight after first stop and completes the literal phases.
- test_first_stop_clock_fence_still_blocks_before_stop: preparation crosses
  midnight before any stop; the fence refuses without service actions.

Actual full local runner result: 74 PASS in9.74s. Own mutation_probe.py run:
4/4 semantic kills, all control rc0 and mutant rc1 (not collection errors):
start order, after16 accepted-noise restriction, duplicate catalog identity,
clean stop's nonzero exit. These are runner controls, NOT application ALL-ON
acceptance or live installation evidence. An initial clock test mistakenly
reused stale fake account stamps and was interrupted during its180s proof wait;
the final isolated clock test does not claim those fake stamps are fresh.

No production file edit, service action, migration, ledger write, automatic
recovery, pin-verifier change or Install2 deployment occurred in this review.
