## Independent Assessment and Scope

AGREE on the recorded BIYA abandonment, with two independently measured
corrections: BIYA used the software-rest path, and OMS/v2 use different quote
producers. Historical OMS cache age/cause is UNMEASURED. Assessment committed
first at 0239726f; subsequent operator replies authorize both EH paths and the
fresh reactive cap change 1.0% -> 0.5%. Full evidence is in
`docs/review-artifacts/nfq2/ASSESSMENT_2026-10-07.md`.

Durable account/slot holds enqueue only through the serial intent lane on the
next fresh ask. A claimed held order gets a 1% cap; fresh entries keep 0.5%.
The reactive ask +0.3% buffer remains bounded by that cap. ORB and NFQ1 unchanged;
mirror-on-fill retains its separate 1% default. Restart/cancel tokens fail closed.
Default-off NFQ2 flag; catalog expects ON only at a reviewed install.

## Tick-Path Review Follow-Up

Quote ticks now match held symbols and check ask freshness/trigger/cap entirely
in memory before opening a session. Ownership/fill/order checks run off-loop on
the periodic turn at the existing broker-sync cadence; the serial pre-wire claim
still re-checks proof. Eligible saves/retirements use `_run_db` and a durable CAS,
with per-slot in-flight fencing and no worker mutation of the runtime hold map.

Isolated active-hold receipt: 14,800 events /60.199 seconds, 245.85 events/s,
maximum loop stall 22.052 ms, 12 periodic DB transactions (0.1993/s), zero wires.
**Not a whole-OMS performance PASS:** the initial full-handler control failed
with 14,882 transactions from the unchanged drift-cancel path. The passing run
disables that path in the test only; integration with HOTFIX1 remains required.
Both receipts are preserved. A 150-ms worker-save control also verifies the
loop yields and concurrent quotes enqueue only once.

44 fast focused tests pass (benchmark deselected); the benchmark-file run
before two additional failure controls passed 7/7. N1-N17 are all RED, including
symbol/freshness admission, periodic cadence, CAS, concurrency and restoring
synchronous loop persistence.

## Tests and Remaining Proofs

Follow-up frozen full unit: **56 failed /7,121 passed**, versus the requested
f9c9bd33 baseline **56 failed /7,076 passed**. Sorted failed IDs are exactly
identical. All nine new tick-path cases pass, including the sustained benchmark.
Ruff and whitespace checks pass. No current-main/Linux CI PASS is claimed.

Previous head 16c435b0: 242 focused pass; 11/11 mutation controls RED. Full unit pair on the requested
f9c9bd33 base: 56 failed /7,076 passed -> 56 failed /7,112 passed, identical
failed names. Ruff and whitespace checks pass. Raw hashes, test names, replay
proxies and restart requirements are in `docs/review-artifacts/nfq2/BUILD_2026-10-07.md`.

DRAFT, NOT ready for pin: uncertain-dispatch terminal recovery, legacy-unbound
intent disposition, all delayed-feedback/cancel and simultaneous-restart
orderings, current-main/Linux CI, exact NFQ2+SLOTCLEAR1 integration and combined
HOTFIX1+NFQ2 quote-path performance remain.
The BIYA reactive/AH variants are controls, not observed historical paths.
No broker orders, production writes, merge, pin, install or service action.
