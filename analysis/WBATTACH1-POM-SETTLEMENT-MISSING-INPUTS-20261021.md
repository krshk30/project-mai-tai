# WBATTACH1-TERMINAL-20261021: POM settlement remains blocked

Status: BLOCKED_INPUTS; DRAFT ONLY scope retained. No SQL authored with an
invented identity, and no SQL executed. No dump read, extraction or query by this author.

The actual assessment SHA is
43393efcb1cb33e7bd83c7f45db196f4b2c9a1259820469619579905e36caa53.
The PG-copy readiness note is main's catalog checkpoint, not a full case packet.

Reviewer-reported facts, not independently verified native receipts:
POM protection intent prefix bb1feb75, entry intent prefix 9a3faa83,
accepted at 2026-10-21 16:37:25Z; combo BFJKSOQ4RH1BF71O2JB0H7BR5A;
target child 6UJKK64KPV461C77QQM1AQSR7A recorded under entry at 16:44:40Z.
None supplies the missing full intent/entry UUIDs or cancelled sibling proof.

Required exact packet from main's authorized PG-copy extraction:

1. Full protection intent UUID, complete row preimage including payload, status,
   quantity, account/strategy UUIDs, timestamps; full entry intent and entry order
   UUIDs/preimages and complete original WBATTACH attempt journal.
2. Managed row UUID/preimage and exact entry bindings; virtual/account position
   row identities, quantities and as-of evidence. Do not infer historical state
   from a flat post-close snapshot.
3. Every original/retry base and both deterministic child COIDs; combo identity
   provenance; exact native child IDs, final states, cumulative quantities,
   prices, times and complete sibling proof with account/native as-of binding.
4. Existing economic child order/fill/event rows and immutable attribution:
   full row preimages and hashes; no replacement, reparenting or replay.
5. Producer manifest/capture start/end UTC/snapshot and schema/head provenance,
   actual row-count coverage, independently bound to delivered dump SHA
   fe396666bec6bdbf622862f8e2a4667839ac59010405ea8977b63cf38a94d4bb.
6. At the actual stopped window: fresh stopped/exclusive-writer proof, fresh
   native terminal proof, backup identity/hash and current exact preimage.

When all inputs exist, the one-row SQL draft must begin a transaction, lock and
assert full exact identities and full preimages, validate ledger/sibling outcome,
update only that intent's bookkeeping status/provenance (and ordinary updated_at
if required by schema), assert exactly one affected row and unchanged payload
outside the intended provenance field, and end ROLLBACK by default. Missing,
extra, mismatched, uncertain or already-divergent rows must raise and abort.
No prefix LIKE, symbol/time matching, default empty preimages, or inferred fill.
Any idempotent already-settled branch must prove the exact reviewed postimage.

Main/Claude must review the concrete draft once bound; Claude alone executes in
the stopped window. This missing-input contract is NOT an executable settlement
draft, migration, acceptance result, repair, or permission to execute.
