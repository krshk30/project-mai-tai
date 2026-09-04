# Correction — PR #898 @ d3f2c7cf, claude-1

**The pin STANDS. One stated justification inside it was false.**

- Original record: `records/d3f2c7cfdafe40cc7448cb0f5f5e5a33ee868e07/pr-898--184cd8e52ea10b4a72a8ee05e354e31d001fae07--claude-1.json`
- That record is **unchanged and remains valid**. This file is additive and is deliberately named so
  it cannot match the gate's record glob (`records/*/pr-<n>--*.json`); it is a note, not a record.

## What was false

The record accepted #898's behaviour change with this reasoning:

> "Bounded at one DELETE per stamped working child with no retry (an OCO carries two), so the write
> pattern stays bounded..."

⛔ **"an OCO carries two" was an assumption asserted as a fact.** `release_native_oco_for_close`
issues one DELETE per working SELL child found in the fetched order tree. There is no code-level
child cap, and an existing test already exercises **three** children. Corrected by `codex-2` after
the merge.

⭐ The failure is not the number. It is that the number was **load-bearing** — "only two" was the
entire reason the change was waved through — and it was co-signed without being checked.

## What the correction changes

`_reconcile_confirmation_exit_protection` is **re-entrant across ticks**: `confirmation_inflight`
prevents concurrent overlap only, and an `unanswerable` result leaves the confirmation pending for
the next tick. Measured on the live box 2026-09-04: **2,290 invocations, peaking at 4 per second**.

On a stuck path — working legs present, DELETEs non-2xx, reread still showing working:

| | DELETEs per tick |
|---|---|
| before #898 | 1 (returned on the first non-2xx) |
| after #898 | **N** (one per working SELL child) |

⚠️ **Unbounded by child count and unthrottled by tick.** Same family as NCRA 145 / CHPT 205 — the
harm there was rejected orders rather than cancels, but the #885 ceiling's own wording is about
sustained write volume risking broker API access, and cancels are writes.

## Why the pin still stands

#898 is correct and necessary. The code it replaced converted a **complete success** into
`unanswerable` every time the OCO cancelled as a unit — the exact defect the PLUG probe was bought
to find. Keeping that would be far worse than an unexercised amplification.

⚠️ **UNEXERCISED, not benign.** On 2026-09-04 this path issued **zero** DELETEs: every invocation
hit `if not working: return "released"` before the loop. Zero is not evidence of safety.

## Disposition

Boarded and watched, **no speculative throttle** (operator ruling, 2026-09-04). #898's own new
`[SCHWAB-NATIVE-OCO-DELETE-NON2XX]` marker makes the first real occurrence visible; repeated firing
on one symbol is the trigger to add a bound. A guard built without an instance is how the last
several unexercised guards were written.
