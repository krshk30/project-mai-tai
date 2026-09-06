# PR #902 pin stands, but its stated regression mechanism was false

**Recorded 2026-09-06 by claude-1. Append-only, per the `pr-898-two-child-bound.md` precedent.**

My pin of **PR #902 @ `eb44cb35`** closed with a limitation it said the pin did not cover:

> the recovery path's interval finalization (`resolution=flat`, `:3981`) has NO controlling test —
> it is inert to mutation. **In production, skipping it would leave the symbol in
> `_confirmation_unprotected_since` forever, inflating `released_unprotected_current` and the max
> duration**, which is precisely the "count of positions currently in that state" the operator
> asked for.

**The bolded mechanism is FALSE.** `codex-2` corrected it while submitting #904, and I verified the
correction independently rather than accepting it:

1. **Code.** `_close_confirmation_flat_leg` calls `_clear_exit_reservation_release` on a successful
   close. That function pops the key from `_confirmation_unprotected_since` and emits
   `[OMS-V2-CONFIRMATION-EXIT-COVERAGE-RESTORED]`. The live current count is already cleared on this
   path, independently of the finalizer.
2. **The mutant's own output.** With the finalizer removed, the summary reads
   `released_unprotected_seconds_max=0.000 released_unprotected_current=0` — `current` is **0 in both
   the passing and the failing run**. It was never inflated.

**What the real regression is:** the elapsed interval is lost from the decision summary
(`released_unprotected_seconds_max` reports `0.000` instead of the true `4.000`). Narrower than I
described, and a measurement loss rather than a leaked counter.

**What stands:** the branch genuinely had no controlling test — that part was correct and is why
#904 exists. #904 supplies the control, and the mutation that was inert on #902 now turns it RED.
**The #902 pin itself is unaffected**; only this one stated consequence was wrong.

⇒ **The lesson, for me:** I asserted a runtime consequence from reading one call site without
tracing the other mutator of that state. The counter had a second writer — the clear path — and
I did not ask who else writes it before describing what would happen.
