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

## Tests and Remaining Proofs

242 focused pass; 11/11 mutation controls RED. Full unit pair on the requested
f9c9bd33 base: 56 failed /7,076 passed -> 56 failed /7,112 passed, identical
failed names. Ruff and whitespace checks pass. Raw hashes, test names, replay
proxies and restart requirements are in `docs/review-artifacts/nfq2/BUILD_2026-10-07.md`.

DRAFT, NOT ready for pin: uncertain-dispatch terminal recovery, legacy-unbound
intent disposition, all delayed-feedback/cancel and simultaneous-restart
orderings, current-main/Linux CI and exact NFQ2+SLOTCLEAR1 integration remain.
The BIYA reactive/AH variants are controls, not observed historical paths.
No broker orders, production writes, merge, pin, install or service action.
