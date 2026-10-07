# RETRYLEFT1 candidate install scope

Not authorized or staged by this PR. The existing October7 paired job is
immutable: APP994f08ae, planf2ed49e8, v2/OMS only; ORB stays running.
SLOTCLEAR1 is pinned for tomorrow's batch; NFQ2 still needs review of its final
two items. No merge or deployment of this branch is implied by build release.

After exact-head review/pin and a reviewed joint plan, RETRYLEFT1 needs v2
restart only. No schema change, new env key, OMS restart or tick-path change.
Keep RETRY_ONE_ENABLED=true and RETRY_ONE_MAX_RETRIES=0; no new catalogue row.
The usual fresh BOT-flat, zero working buys, zero managed/virtual rows and
zero in-flight intent gates remain. The actual joint merge order, APP SHA,
baseline PIDs, manifest and release approval must be bound at that window.

Post-start: verify exact APP/PID/start/NRestarts0, retry true/zero in /proc,
restored cancellation requests and per-account receipts without granting new
entry ownership; zero startup buys and new-process errors; bounded Redis and
catalogue proofs, bar-continuity/scanner checks as the joint restart list needs.
Re-pin preopen to the actual final identities only after those proofs.

Rollback/recovery is not pre-authorized. Code revert requires an operator
decision and v2 restart. Pending purpose=retry_exhausted records are NOT
safe input for older scanner-only code: drain/prove receipts first or approve
an explicit compatibility plan. Do not purge/edit them or relabel them as
scanner removals, and do not turn RETRY_ONE off (it restores legacy release).
