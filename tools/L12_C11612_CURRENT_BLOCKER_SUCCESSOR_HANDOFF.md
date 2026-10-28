# 7ed6 follow-up: no segment-wide admission scope after retirement

Main's scope finding is real. 7ed6's remembered segment could keep consulting L12
after the OLD note was successfully retired. This successor supersedes ONLY that
scope/provenance implementation; 7ed6 remains immutable for its pending receipt.
No author tests/imports/PG/provider/runtime; no migration/native cadence changes.

## Exact correction

- exception_scope now examines ONLY current OLD cap/owner blocker. All writes and
  reads of _brokerdecides1_exception_segment are removed. Successful retirement
  does not leave a symbol-wide latch, including later BUY/reclaim in SAME SHORT.
- Admission entry_decision still delegates to actual legacy first-rest guards
  outside current scope. It does not consult an old snapshot or capability.
- Final-publication draft_uses_exception is current scope OR the exact draft's
  actual FreshJournalCapability. This is the existing single-use, process-local,
  identity-checked capability from a committed/read-back attempt, not a new
  capability, native permission, metadata flag or restored-row inference.
- An already-journaled exception draft therefore invokes the original native
  assessment in prepare_send even when its OLD note clears during L11's await.
  Missing/stale/working books still hold that draft. An unrelated ordinary draft
  has no such capability and bypasses L12 memory, even in the same segment.
- If owner retirement completes BEFORE any draft/attempt is made, the OLD blocker
  is gone. Subsequent ordinary creation/admission/publication follows OLD guards;
  it does not inherit the retired identity. This is C1's CURRENTLY-blocked scope,
  not an exemption from an already issued attempt's publication authority.
- Ordinary->exception DURING the guard still requires real fresh authority and
  refuses if absent. Before-XADD durable cleanup/post-XADD UNKNOWN retention,
  duplicate token/capability fences and all original native/economic checks stay.

## Controls

Existing new-module intended total becomes 31 (previous 27 plus three new draft
final-check cases and one actual same-segment cap-clear control). Source changes
to prior 27 controls are limited to:

1. actual cap success has no remembered segment attribute;
2. actual ordered owner retirement -> same-segment real first-rest ON==OFF,
   with poison memory proving no L12 read, plus ordinary reclaim prepare_send;
3. retirement leaves no scope in same or next segment.

The actual _clear_cw_slot_claims control also proves cap removal in the SAME
segment restores legacy admission/preparation with a poison native-memory port.
Three new draft cases: already-journaled draft after current blocker clears still
refuses missing/stale/working native proof, whereas a distinct same-symbol draft
has no inherited obligation. Actual service boundary/callback invoked; native
facts and state settlement explicitly CONTROLLED.

Main owns exact selection based on whether pending 7ed6 controls executed.
No previous PASS count is represented as qualification of this successor.
Readset/clone successor 0f1bf1229 and its 11 new / 23 changed controls remain a
separate tools-only checkpoint, not included in this APP root.

Next remains concrete WORX103 port list consuming Gibbs's shared day feed;
all 122 / 34 / Friday boot / manual20 / native/indexed-rate / final-full gates
remain OPEN. No lease/native headers/source approval is manufactured here.
