# Codex Install2 Candidate Runner

Isolated sidecar at plan base `f4a61ea89d47e0dc6f97e6be3dab496ef40f9cfc`.
No production reads/actions, SSH, merge, deployment or rollback authority.
Parent owns exact merged source integration, approval, staging and attendance.

## Binding Before Use

There is deliberately no `binding.json` or release/approval in this checkout.
APP and TREE remain `UNBOUND_PARENT_APP` / `UNBOUND_PARENT_TREE`; BOX is actual
`4805ddc81184c76b4d5cef5c483c809edb666fe6`. No old TREE fallback.
`binding.template.json` is documentation, not an executable binding.

Parent supplies three actual merge receipts in order: completed #1102 at
`52659779bcf4b6e28a2896e6eef3002488a6d2c9`, completed #1106 at
`f80a9c4af5e79beaa214e99f448bfe408c596df1`, then the actual reviewed integration
PR/merge covering governing PRs `[1104, 1099, 1105, 1101]` in that order.
The batch row includes `governing_prs` with those four IDs; it is not four
synthetic individual merge SHAs. Parent supplies final APP/TREE,
an exact changed-path allowlist and a VERIFIED source-combination JSON receipt
bound to those same fields. Receipt must establish full same-environment unit
pair, actual failed-name resolution, CI, ALL_ON and mutation acceptance; runner
tests are not application acceptance. Partial candidates must not be labeled
combined PASS. Parent's top-level combination plugin is outside this job scope.

Parent also supplies the exact existing T43 ON catalog name, old deployed
isolated helper hashes, and five hash-bound Install1 input paths under
`/home/trader/`: original snapshot, actual before fleet, runner journal,
continuation journal and a newly DERIVED human-review receipt. The receipt quotes
the user VERIFIED authorization with provenance, binds all four raw hashes and
five current PID/start pins, and retains Install1 INCOMPLETE/original_complete=false.
No existing install-record/final-fleet JSON or original COMPLETE is assumed.
Adapt the receipt to actual reviewed records;
do not synthesize production identities, timestamps, history or acceptance.
The actual original JSONL contains six command receipts: stop v2, stop ORB-Schwab,
restart OMS, restart strategy, restart control, start ORB-Schwab. The separate
continuation text log is hash-preserved as provenance, including its disclosed
receipt collision, not parsed as fictitious JSONL. V2's new start is positively
proved by its changed PID/start and the human VERIFIED review; its command
receipt is explicitly UNAVAILABLE. No stop/start-OMS commands are invented.

Local assembly (never production staging):

```sh
python job/derive_install1.py --inputs actual-input-paths-and-hashes.json \
  --current-fleet actual-readonly-current-fleet.json --provenance quoted-human-review.json \
  --output LOCAL_NEW_DERIVATION_DIRECTORY
python job/make_release.py --plan FULL_RUNNER_COMMIT --binding parent-binding.json \
  --source-proof parent-source-combination-proof.json --package LOCAL_NEW_DIRECTORY
```

Run `make_approval.py` from that package after exact candidate review, with the
published release hash. Only the parent may stage the root-owned immutable
package and attend `attended.py RELEASE_SHA256`. Unbound verification happens
before lock/attempt creation or effects. The literal runner cannot withdraw
control restart by a manifest toggle: a withdrawal needs revised tested code.

## Literal Scope

Stop v2 -> strategy -> OMS; start OMS -> v2 -> strategy; application proof;
one plain control restart last, then page-only LIVE/SCHWAB proof. No token-owner
or control-owner page gate. Never act on ORB-Schwab, gateway, ORB, paper, guard,
capture, reconciler, Redis or Postgres; no migration or trading write. Daily
closeout alone installs/enables its checks-only timer; it does not invoke the
real preopen gate tonight or start a trading service.

Four explicit new true environment keys only: KEEP_REST_AFTER_BUY,
REMOVED_WAIT_CLEAR, OMS_V2_WEBULL_MIRROR_RETAINED_HOLD and WEBULL_LIST_PRIMARY_READS
(full names in release_policy.py). Existing retry-enabled=true / maximum=0
must already be present and remain unchanged. T43 is read from the final source
catalog, not changed by the environment writer. Catalog identities/counts come
from actual source owner identities plus the deterministic numeric zero-budget
overlay. No invented additional catalog owners. Missing four-flag/T43 ON catalog
blocks assembly; attended proof independently requires the four explicit values
in the actual new v2/OMS process environments.

Before every service action: current fleet identity, fresh direct broker-flat
proof, zero working/open managed/virtual rows, all-date ticket census, zero
requested/price_wait/submitting, read-only Redis and SQL checkpoints, exact
clean source. MI/NXL and recorded reject-no-id client_abort helper admission
remain byte-for-byte inherited from Install1. UNKNOWN reads retry only rc2,
three total attempts with 60-second gaps; both stdout and stderr retained.
Failed clean stop blocks; no CancelledError/reset-failed waiver. Exclusive
backups, hashes, deploy lock, attended signal traps and actual STOP census
remain; abort does not restore/restart anything.

## Cumulative Daily Proof

Capture actual Install2 initial fleet and official before-restart snapshot.
Retain original Install1 snapshot bytes separately. Cumulative schema1 record
references both real journal chains and declares the union of five reviewed
restart owners. ORB-Schwab stays unchanged in Install2 but its future preopen pin
is updated from reviewer-proven Install1 PID612486/start, not the stale old pin.
Gateway PID2907 and other unaffected identities must remain exactly unchanged.
No fresh fake baseline and no declaration that Install2 restarted ORB-Schwab.

Closeout replaces the still-old `8cdafcde` preopen file and not-yet-installed
catalogs, then actually creates the previously absent daily root, immutable
runtime pins, unit files and timer. ET date/report are dynamic. Paper admission
requires today's03:40-or-later start ordered after its active daily guard,
NRestarts0 and a pre07:00 check. Latest v2/OMS/strategy/control exact pins come
from actual new processes; the original snapshot remains the cumulative basis.

Only exact after16 anchored-history ValueError tracebacks from the reviewed
schwab_v2_rest_client logger and four-frame source shape are accepted open.
Both exact reasons are counted and hash-reported as ACCEPTED_OPEN_LINESRC1.
The collector wrapper filters only those traceback headers in memory, preserves
raw hashes and discloses the disposition, never zero-error/PASS. Other errors,
startup failure, chained/foreign/malformed tracebacks, before16 cases or unknown
evidence block. No broad logger/ValueError waiver. A pinned LINESRC fix yielding
no such errors needs no exception. Live source correctness remains UNMEASURED.

## Handoff Blockers

Final two-merge-plus-reviewed-batch APP/TREE, parent source-combination receipt, actual installed
helper hashes and complete reviewed Install1 chain are not supplied here.
Therefore this candidate is NOT production-ready and must fail closed.
Future daily live delivery and next-session trading behavior are UNMEASURED.
## Oct6 attended continuation

Direct operator continuation: start OMS before evaluating its own book freshness,
then v2, strategy, control; a failed start alone authorizes the 4805ddc8 fallback.
Original STOP and receipts remain immutable history. The phase-seven continuation
performs no further service actions. Application remains 5b8b4f64.

After20 no scheduled bars arrive. The new-process log proof may instead record
HELD_EXPECTED_NO_SCHEDULED_BARS, requiring a same-day after20 start, a fresh
literal BOOT-RESTORE with evaluated=confirmed=pending>0, warmed=0, timeout=0,
distinct complete pending names and a BOOT-HOLD HELD without release. This is not
restoration PASS: restoration and release remain UNMEASURED until the next
scheduled bars (Wed07:00 read). Other process, error, account, Redis, no-buy,
catalog and timer proofs are unchanged. Local runner tests:105PASS.
The raw official collector report is retained. Only its exact two absent
warmup/release failures may be labelled ACCEPTED_HELD_OFFSESSION when the
independent fresh literal hold and N/A_OFF_SESSION bar result are present;
any other failure or unknown still blocks. Control is declared in the actual
restart set, with its original identity and action receipt. No check is called
PASS on pending evidence. The daily gate itself is not run early or waived.

At22:00:12ET the installed369s seeded fallback ran (elapsed373.1), with all7
DB-confirmed names capped/entry slots consumed and reconstructed_uncapped=0;
BOOT-HOLD then released literally. This existing ERROR-level event is matched
by its complete message, bound, after20 timestamp and exact positive population;
another error still refuses. The official collector already validates this
bounded fallback. It is not fresh-bar restoration; next-session line/scanner
proof remains UNMEASURED. No change to application, switches or the fallback.
