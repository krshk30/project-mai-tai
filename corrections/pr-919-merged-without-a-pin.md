# PR #919 merged with `independent-review-pin` RED and no record in this ledger

**Recorded 2026-09-08 by `claude-1`. Append-only, per the `pr-898-two-child-bound.md`,
`pr-902-uncovered-current-count.md` and `pr-905-paper-not-live-settings.md` precedents.**

## What the ledger shows, and why that is misleading on its own

A sweep of every PR from **#910 to #923** against `records/*/pr-<N>--*.json`:

    #910 #911 #912 #913 #914 #915 #916 #917 #918 #920 #921 #923   pinned at their exact head
    #919                                                          ZERO records, gate RED

**#919** (`fix(dashboard): a filled order must not duplicate a position the fills already priced`,
head `4a7816ce`, base `80b7eeeb`) merged as `5862b89c` at **2026-09-08 18:56:49 UTC** while
`independent-review-pin` reported **fail**. `validate` passed.

## ⛔ This was an explicit operator waiver, not a bypass

The operator's words, after two rounds of dashboard fixes had already gone through the full gate:

> *"it's just a dashboard,, can you not review and pin it then merge and deploy"*

⇒ The gate was not defeated, circumvented, or unknowingly skipped. It was **waived by the person
whose money is at risk**, for a change he judged to be presentation-only. That judgement was
reasonable: `trade_episodes.py` builds the Completed Positions table and places no orders.

## What the waiver actually cost, stated plainly

**Dashboard code reached production without a second agent ever reading it.** That is precisely the
property the gate exists to guarantee, and no amount of "it's only a dashboard" changes what was
given up — only whether giving it up was worth it.

It is worth naming what the same author had already got wrong on this exact file *that same day*,
both caught by review:

- I verified **#918** against a payload I had **reconstructed** rather than the live one. It passed
  while the phantom rows were still on the operator's screen.
- I then reported a **13-second settle-lag near-miss** as the cause. Wrong — the two sources were
  never comparable (ISO vs display-ET), so **both dedupe paths had always been dead**.

⇒ The reviewer was doing real work on this file hours before #919. Waiving the gate on the third
change to the same code is the least safe place to waive it, not the most.

## ⛔ There is no way to pin it now

`base.sha` for a merged PR is frozen at the actual parent of the merge commit, and the audit can
only grade a head that contains its merge base. **A record written now would be graded against a
base that has moved.** ⇒ Do not attempt a retroactive pin; it would be a stamp, not a review.

**The only honest close is a post-hoc review of `5862b89c` recorded here as a correction** — a
review whose finding, if any, becomes a new PR through the normal gate.

## What this record is for

So that a future audit reading a hole at #919 finds **a decision with a reason**, and not evidence
that the gate silently failed. It did not fail. It was switched off, once, on purpose, and the
person who switched it off was entitled to.

⭐ The lesson is not "never waive". It is that **a waiver leaves no trace unless someone writes one**
— the ledger records what passed, never what was excused.
