# W3 Cold Normal-Append Caller: Preparation Only

`case_collision_append.append_reviewed_collision_plan(store, plan_bytes, approval,
price_source_bytes=..., current_source_head=..., apply=False)` defaults to fresh
validation DRYRUN and never appends unless apply is the literal booleanTrue.
`CollisionAppendApproval` pins planSHA/planID/approvedsourcehead/providerpriceSHA/
reviewID and a quiesced writer. Production authorization defaultsFalse. No DSN,
CLI/live-execute shortcut, provider call, price UPDATE or event DELETE exists.

## Reviewed Main Mechanics

Main must compose the source on its cumulative W1/W2/W3 release and obtain exact
source/mechanics/plan approval. Before first apply, stop/verify the old paper writer
quiesced and record current process/source/module hashes/flags, existing authorized
DB role/schema and exact214 native population. Do not forge the approval/quiescence
fields: they are trusted main-preflight inputs, not independently discovered here.
The source helper performs its own fresh DB checks in the guarded transaction.

Pass the existing `MomentumPaperStore` and original sealed DRYRUN bytes
SHAa49060c7a4d48e460148465d129aca6cde6602776df3df9a9728414d42e653e7,
plus original retained provider source bytes SHA99b7...df738. Plan fixes213 exact
native targetIDs and one cache audit; it does not widen scope. Source bytes are
rehash-checked and the exact COMMON prevDay values/exclusions recomputed before
any insert. Every native preimage/complete population and ratio/ID/reason/reviewer
binding is rechecked. Driver formatting differences compare as UTC-equivalent
typed instants from the sealed UTC native capture; naive PG timestamps refuse.

PG DRYRUN uses SERIALIZABLE/read-only. Explicit approved apply uses SERIALIZABLE,
transaction-local250ms lock timeout/2000ms statement timeout, a nonblocking
transaction advisory lock and NOWAIT native-row locks. Quiescence is still required;
the advisory lock does not coordinate ordinary paper writers. No automatic retry
or isolation downgrade. Changed/new/missing native rows, altered reference bytes,
conflicting or partial prior audit sets refuse before append.

Apply calls the existing `MomentumPaperStore.append_many(records, session=session)`
API. Its optional session injection does not change the default service calls or
their SQL/commit behavior; the injected path stages only INSERTs inside the caller's
single transaction. All214 audits commit together or roll back on failure. Original
rows are never modified. A complete identical prior batch is idempotent0; a partial
batch requires main review, never automatic completion. Receipt is returned only
after commit succeeds and contains actual generated audit IDs for that context.

Main must use a new cold process/import path matching the reviewed cumulative
release; the provided current_source_head is a trusted fresh main observation.
After apply, retain real append/audit IDs and verify original rows, page/cache and
the approved W3 gateway+strategy+paper+control runner. C v2 guards join W2 once.
Scanner rule9b/0ERROR/off-by-window nonsticky A state remain main/reviewer checks.
Install deadline03:30ET enforced; no later blind retry/production rearm.

## Worker Evidence Limit

Worker unit and native214 rehearsals use only private in-memorySQLite. Default
DRYRUN0, explicitCOPY214, replay0, originals214unchanged. Fault after staged append
rolls back the entire batch. Approval/hash/source/head/quiescence/expiry/partial/
preimage/ratio/cache/UTC controls refuse invalid inputs. Actual PG transaction,
permissions/timeouts/serialization and box execution are NOT tested or performed.
No PG or production pass is inferred from SQLite or SELECT compile controls.
