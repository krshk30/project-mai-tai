# v2 entry-count + exit-poll — one workstream

**Status:** DESIGN, not built. Found live 2026-08-03 on real money.
**Deploy:** ATTENDED, after the close. Core live OMS logic + the live entry path.
**Why one workstream:** they compound on the same symbol. The entry bug makes THREE positions where
two were intended; the exit-poll bug then fails to record the extras. Fixing either alone leaves the
other half of the HYFM picture unexplained.

---

## ⭐ THE THROUGH-LINE — this is the month's recurring class

**Something believes a thing, ground truth says another, and nothing reconciles them.** Same shape,
four surfaces:

| instance | belief | ground truth | reconciler |
|---|---|---|---|
| phantom managed rows (today) | in-memory poll set | `oms_managed_positions` | **none** |
| #565/#566 P&L blackout | our `fills` record | broker execution | added after the fact |
| F3 fleet health | a service's own heartbeat | does its function run | that IS the reconciler |
| entry counter (today) | `cw_entries_this_flip` | entries actually filled | **none** |

Framing both fixes as *closing this class* is the point. Three separate one-off patches this month
did not stop the fourth instance appearing today.

---

# FIX 1 — the entry counter is reset by the arm that its own fill caused

## What happens

The resting buy sits AT the ATR line and fills the instant price touches it, INTRABAR. The bot
confirms the cross at the BAR CLOSE, seconds to minutes later. The arm handler then runs:

```python
state.cw_entries_this_flip = 0     # strategy_core/schwab_1m_v2.py, BUY-flip arm block
```

…which wipes the entry that just caused the cross. Counting restarts at zero, so the cap of two
allows two MORE — three total on one cross.

## Live, three times on 2026-08-03 (all HYFM, all real money)

| resting fill | ARM | gap | then | total |
|---|---|---|---|---|
| 12:09:41 @1.76 | 12:10:02 | 21s | 1.905, 1.95 | **3** |
| 17:31:22 @2.12 | 17:32:03 | 41s | 2.225, 2.2787 | **3** |
| 18:11:13 @2.15 | 18:14:02 | **169s** | 2.24, 2.365 | **3** |

Confirmed ONE `[V2-CW-ARM]` and ONE `[V2-CW-DISARM]` across each run — the operator's chart showed a
single unbroken trail and was right.

## The fix

**On arming, do not reset to zero — start the count at the number of entries already taken on this
cross.** If the resting fill already happened, the count starts at 1 and only the reclaim remains.

⛔ The boundary is the whole design: a genuinely NEW later cross has taken zero entries, so it still
starts fresh at 0 and gets its full two. The fix must not over-correct into blocking legitimate
re-arms.

## Acceptance criteria

1. Each of the three live cases above yields **2** entries, not 3.
2. **BOUNDARY:** a real second cross after a completed first still gets a clean fresh two.
3. A cross with NO pre-arm resting fill is unchanged (byte-identical).

---

# FIX 2 — the exit poll iterates memory, not the table it services

## What happens

```python
for key in list(getattr(self, "_managed_v2_symbols", set())):   # oms/service.py
```

An open row in `oms_managed_positions` whose key is absent from that set is **never polled, never
logged, never closed** — and it blocks fan-out re-entry via `fanout_webull_collision_managed` for as
long as it lives. Because the loop body never runs, there is not even a miss line: from outside,
"never polled" is indistinguishable from "polled and found nothing".

## Evidence (2026-08-03)

Three phantoms: `live:orb` FUSE (2h17m), `live:orb` HYFM (1h41m), `live:schwab_1m_v2` HYFM. Each:
filled entry, OCO bracket emitted, broker flat, row open, **zero miss lines**.

The account is NOT the discriminator — the same account polls fine for other symbols:

| account · symbol | miss lines | captures |
|---|---|---|
| live:orb UPC | 4 | 3 |
| live:orb EZRA | 2 | 2 |
| **live:orb FUSE** | **0** | **0** |
| **live:orb HYFM** | **0** | **0** |

⛔ **Ruled out, each checked:** the collision-skip path (enrollment happens at open for both accounts,
`service.py:2015`, immediately before the `MANAGED-OPEN` log) · all five discard sites (each closes
the row first, or fires only when no open row exists) · rehydrate (OMS up since 07-31, `NRestarts=0`)
· `_v2_accounts()` (does include the Webull account when the fan-out flag is on) · the store lookup
(filters only account+symbol+open, so it WOULD find these rows) · a loop-abort (no exception follows
any capture).

**How the keys left the set is still unpinned.** That is deliberately NOT a blocker — see below.

## ⭐ The fix is CAUSE-AGNOSTIC — do not gate it on finding the eviction path

Whatever evicts a key, driving the poll from the open rows closes the class. Chase the "how" only far
enough to know whether there is a fast leak worth plugging separately.

## Shape: keep the set, but only for the job that justifies it

`_managed_v2_symbols` is a **bare `set[tuple[str, str]]` — no per-symbol state.** Its documented
purpose is the QUOTE hot path: *"a quote only opens a session/evaluates when its symbol has an OPEN
v2 managed row."* That is a real performance justification — a DB session per quote tick would be
unacceptable.

The exit poll then REUSES it as a work-list, and that use has no such justification: the poll runs off
a periodic sync with a ≥30s per-key throttle, where a query is free.

⇒ **Keep the set as the quote guard. Eliminate its use as the poll's work-list.** The poll iterates
open managed rows directly, and RE-ENROLLS any key it finds missing — which repairs the quote guard
as a side effect, so the hot path self-heals instead of silently under-protecting.

This is stronger than reconciling set←table on a timer: there is no second mechanism to keep correct,
and the repair happens exactly where the divergence is observed.

## Acceptance criteria

1. ⛔⭐ **THE ONE THAT MATTERS — artificially remove an open row's key from the set and prove the poll
   still finds it, polls it, closes it, and re-enrolls the key.** A fix that only works when the set
   is already correct does not close the failure mode; it assumes it away.
2. A row with no open position is not resurrected.
3. Poll cadence and throttling unchanged (no new broker load).

---

## Rollout

Both fixes, one attended deploy after the close. Entry-counter first (it reduces position count), then
the poll fix (it records what remains). Verify on the next live cross that entry count reads **two**,
and that no managed row outlives its position by more than one poll cycle.

## Related

[[project_mai_tai_v2_entry_segment_identity]] · [[project_mai_tai_oco_exit_fill_blackout]] ·
[[feedback_a_watch_that_fails_to_a_false_clean]] · [[project_mai_tai_reconciler_detects_nobody_listens]]
