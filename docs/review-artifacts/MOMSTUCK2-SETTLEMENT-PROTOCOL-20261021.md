# MOMSTUCK2: legacy SPHL settlement protocol for main/Claude review

Status: SOURCE AUTHORING ONLY / ALL RUNTIME UNRUN. This is a proposed adaptation
of the reviewed O-1019-26 v2 protocol, not approval or execution of a new repair.
Claude's 09:58 acknowledgment (relayed by main at 10:02 ET) assigns actual SPHL
settlement and the box copy to Claude after close. No in-session restart.

## Why the source fix does not automatically repair legacy rows

At exact base 99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1, both quote-change
refusals in momentum_live/execution.py occur after durable_order and before
submit. The first leaves claimed/pending; the second has already called
begin_submit and saved submitting/pending. Neither settles the order or parent.
The actor skips claimed/no-submit reservations; start rejects claimed rows
with ANY order_count. Dates do not exclude active parents in restore.

The patch's new settlement port is called only inside that pre-submit lexical
boundary. It atomically aborts the pending order, cancels the parent intent and
sets its episode terminal_no_fill, then the executor reconciles/saves RAM and
refreshes quote ownership. Source IDs, quantity, route and existing submit clock
are preserved. Its diagnostics retain the failing quote read. Database refusal
or failure keeps RAM reserved. Submit/report handling remains outside the catch.

Do not call this live-only helper on exported or legacy rows: a pending order
with null native ID and null submitted_at can also be a lost ACK. In particular,
do not infer an unsent order from the episode's first_submit_ns alone: it is set
before wire. No legacy boot classifier or blanket expiry is added.

The existing reviewed precedent is retained outside this branch at:
`/Users/velkris/.codex/task-queues/analysis/O-1019-26-7FA2-V2-STATIC-REVIEW-20261019.txt`
and
`/Users/velkris/.codex/task-queues/analysis/O-1019-26-MOMLIVE-F723-UNSENT-RELEASE-DRAFT-v2-20261019.sql`.
That approval covers exact WORX preimages only; it does not authorize SPHL.

## Required inputs and fail-closed review gates

1. Claude captures full typed rows and immutable export hashes: SPHL parent
   (prefix 9ba4a2c0), order (prefix 75e3889e), known coid
   `ml1-SPHL-e-9ba4a2c0aacc5dbaa444`, strategy/account bindings, every linked intent,
   broker order, event, fill, bot_control_attempt, owned virtual/managed position,
   armed stop and parent runtime/protection payload. Full UUIDs must come from
   the export; never expand prefixes by guesswork. Include current schema and
   installed source/settings identities, export time and consistent snapshot ID.
2. Retain the original refusal log with episode/coid identity, not merely the
   symbol. Independently verify dispatch-permit persistence on the exact installed
   entry path. Absence of a permit is useful only with that path proved. Retain
   fresh account-native positions/working-order receipts for BOTH accounts and
   broker order-history evidence over the attempted-send interval. The reported
   09:45 zero-position/working-order read is historical, not a commit gate.
3. Before any mutation, main/Claude review an exact dry-run-only transaction
   adapted to these FULL SPHL preimages. Require a verified backup and exclusive
   stopped OMS with no in-flight owner/call, ONLY during the separately authorized
   after-close stop window. A receipt string is an attestation, not proof of this.
4. The transaction uses SERIALIZABLE, bounded lock/statement timeouts, shared
   `momentum_warn_live:capacity` and `orb-v2:live:orb:SPHL` advisory locks, then
   locks the exact parent/order. Compare complete typed preimages. Census by
   intent ID OR episode metadata OR exact coid and inspect all dispatch journal
   associations, events and fills. Require exactly the reviewed order/intent set,
   pending buy/open state, zero filled/exited, no native ID/submit receipt/permit,
   no runtime protection/exit work, no own exposure/armed stops. Any discrepancy,
   missing evidence, or ambiguous broker receipt means ROLLBACK and no restart
   acceptance; do not clear real sent/unknown episodes.
5. Mutate ONLY the reviewed SPHL order pending -> aborted with explicit client
   `refused_before_wire` audit context, parent status -> cancelled, and episode
   state -> terminal_no_fill. Preserve every other episode field, including
   source_ids, session_date, interval, route, clocks and quantities. Add a unique
   repair-once receipt with prior states, backup/export hashes and reviewer ID.
   Do not invent a broker event, execution, cancellation ACK, closed_ns or fill.
   Full before/after postassertions must show foreign/other rows unchanged.
6. Retain the ROLLBACK audit and copied-table boot outputs for reviewer approval.
   A commit copy may differ ONLY in the final ROLLBACK -> COMMIT and must be
   separately authorized by Claude/operator. An already-repaired row is a
   verified no-op after matching its receipt/postimage; never apply twice or
   overwrite changed fields. Exact preimage changes require a fresh review.

## Copied-table boot acceptance owned by main/Claude

Use the reviewed after-image on a consistent OFFLINE copy of tonight's actual
tables, including unrelated real claimed/sent owners. No provider access. Boot
the exact proposed installed head at the next session clock through actual OMS
restore/start, then strategy/control registration. Retain source/settings/schema
identities, copy manifest, command, exit status, logs and structured outputs.
Require SPHL terminal/no-fill, no revived order/intent, no admission blocked by
SPHL, source idempotence on same-day restore and zero extra submit calls. Preserve
all real claimed/sent reservations and native protection/unknown-ACK ownership.
Never change their state to make boot pass. Final after-close live readiness,
guard and native receipts are Claude's separate acceptance, not an offline claim.

## Evidence limits at author handoff

No full SPHL copied rows, dispatch journal, current native receipts, or tonight's
table-copy manifest were supplied to this worker. Actual SPHL settlement and
boot acceptance therefore remain UNKNOWN/PENDING, not passed or waived.
New tests copy only their own synthetic SQLite tables. Their SPHL prices,
identities and rows are synthetic; WORX/FRGT share the source branches but are
not claimed as exact historical fixtures. No tests, imports, replays or builds
were run by this worker; main is sole executor after full session 5918.
