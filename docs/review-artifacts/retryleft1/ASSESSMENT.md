# RETRYLEFT1 independent assessment

As-of 2026-10-07 15:49-15:57 ET. Application base: 994f08ae.
Read the handoff case first; this assessment is disclosed independent replication,
not blind discovery. Own raw sources: collect_ncpl.sql, ncpl-own-read.json,
ncpl-own-logs.txt, production /var/log/project-mai-tai/schwab-1m-v2.log.
All production reads are read-only. No broker write, ledger change or service action.

## Verdicts

AGREE on NCPL's issue and missing-cancel cause. Own broker-order pull shows the
primary buy364 at19:01:03.916UTC and mirror182 at19:01:04.574, both stop1.64/
limit1.65. Own fill read shows primary364@1.63 at19:08:15 and sell364@1.6054
at19:10:10. The durable owner becomes consumed at19:10:18.047; own v2 log
records retry_budget_exhausted at19:10:18.069. The mirror is ultimately
cancelled at19:32:49.902. There is no intervening v2 resting-cancel line.
The actual close-to-cancel gap is22m39.902s; budget-log-to-cancel is22m31.833s.
This is a missing step, not a failed cancellation request in this evidence.

AGREE on the proposed scope, with one safety implementation detail: reuse
CLEARWAIT's serial cancellation barrier but NOT its unfilled-episode release
proof. Existing assess_removed_wait intentionally refuses filled history and
apply_removed_wait_proofs can retire/unconsume the opportunity. The new
purpose=retry_exhausted confirms cancellation only; consumed/bound owner, fill
accounts, managed-row ids and retry count stay untouched. No new switch or
schema migration. RETRY_ONE_ENABLED=true/MAX_RETRIES=0 remain as installed.

Own code read: _retry_one_release_or_hold persists the close budget and holds
the owner, but has no cancellation of existing sibling orders. The flat
consumed-owner fast return also omitted restart recovery of those leftovers.
A confirmed-BUY bound episode must retain that state: KEEPREST's installed
controls caught an initial attempt to mark it consumed, which was removed.
The fix adds cancellation only to that confirmed, closed, zero-retry episode.

UNMEASURED on Schwab's precise trigger source and Webull's broker trigger rule;
no assertion that captured quotes prove a particular broker trigger.
The RETRYLEFT scope does not change triggers, prices, quantities or sizing.

DISAGREE on the APUS 09-24 later-fill acceptance case. Own direct verification
at20:03:19.511UTC (apus-parent-verification.json), independently of the sidecar
interpretation, finds Webull order3cf34c23 filled1@5.11 at19:54:58.742UTC.
Schwab close9f499ac2 filled2@5.3486 at20:26:30UTC (16:26:30ET). Both brokers
were already filled; there was no waiting Webull buy to cancel after that close.
The sibling order updated_at=23:55:10.146963 is NOT a fill time. Cancelling a
still-held sibling position would violate this card's both-filled exclusion.
APUS is a negative control, not a positive leftover-cancel acceptance replay.

UNMEASURED on the claimed actual-broker-live27 denominator. Own bounded
strategy-specific extraction yields157 closed broker-leg lots (68primary,
89Webull including one attributable manual close), not159. Two other-strategy
Schwab closes were excluded. Retained lifecycle history infers two waiting
cases (ARTL472.889s; NCPL1359.899s) but does not attest continuous historical
broker status. The inferred2 are not substituted for an actual-live count.
Scope Sep22 04:00UTC through Oct7 19:49:20UTC; this is not a complete Oct7 day.
See HISTORICAL_READOUT.txt and class-counts.json for lineage/caveats.

Old/new: AGREE the missing sibling cancellation is an old class, independently
corroborated by ARTL09-23. DISAGREE that APUS proves a209-minute second trade.
The two known versions are stated above, rather than fabricating a positive
APUS replay. Following the standing stop-on-disagreement rule, RETRYLEFT source
work and review publication STOPPED pending corrected acceptance disposition.
NCPL's confirmed defect and the approved operator card are not withdrawn.
Local unfinished implementation is preserved, not merged, pinned or deployed.

Before discovery:32new tests and261combined controls passed;10assertion
mutations RED, including cancel-not-emitted (23fail), mirror barrier removed
(20fail), software arm not disarmed (8fail), consumed owner cleared (2fail),
token/age/purpose/budget/close guards (1fail each), unknown cancel admitted
(3fail). These are recorded-state/explicit simulated receipt controls, not
live broker cancellation latency. Full-unit run was interrupted on the
assessment STOP; no complete failed-name pair or readiness claim is made.

## Safety / install

Cancel only after fresh, exact managed-episode terminal close evidence, when
the same causal SELL cycle has exhausted its budget. A BUY flip without a
fill/close does not trigger this path. A later SELL cycle is not spent by an
older close. Both filled legs have no leftover and receive no new request.
Unknown receipts remain blocking. Restored pending requests re-emit exact-slot
buy-only barriers; proof never cancels sells or protective exits. Scanner
removal's original unfilled proof and manual-stop code are unchanged.

Durable cancellation requests use existing DashboardSnapshot records and
existing v2 periodic off-thread proof read. OMS source and per-tick handlers
are unchanged. A reviewed install requires v2 restart only. This PR is not
part of the already staged LINESRC1/HOTFIX1 exact-SHA job.
