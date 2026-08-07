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

## ⭐⭐ THE CAP IS COMPOSITION, NOT A COUNT — operator-revised 2026-08-03

**Exactly one RESTING and one RECLAIM per cross. Never two reclaims ("very bad"). Never two
restings.** A scalar "cap at 2" is the wrong shape: it permits `resting+resting` and
`reclaim+reclaim`, both of which are forbidden. Gate **per type**: `≤1 resting AND ≤1 reclaim`.

⇒ **This is what makes defect 2 the mechanism, not a footnote.** Because the resting fill never
increments the counter, the reclaim path sees TWO free slots and can fire TWO reclaims — the exact
composition the operator calls very bad. Counting the resting fill (defect 2) plus the per-type gate
is the pair that prevents it; either alone does not.

### The degenerate case — operator-confirmed

A cross where the RESTING never fills (Schwab rejects it via the API-open block, or it is repriced
away before price crosses): **RECLAIM ONLY. The resting slot is FORFEIT. A reactive entry may NOT
substitute into it.**

| | |
|---|---|
| chosen | resting rejected ⇒ at most ONE entry (the reclaim) on that cross; reactive BLOCKED |
| rejected | reactive-first fills the empty resting slot, then reclaim ⇒ still two |

Rationale on record: never trade a type the operator did not ask for. UPC today went fan-out-only
after an API-open reject — under this rule that cross gets one entry, not two.

⚠️ **Blast radius — state it before shipping.** This makes the REACTIVE path a reclaim-only path; it
can no longer serve as a first entry. Today's Schwab leg filled **11 resting and 10 reactive**
entries, so a material share of the reactive ones would not have fired. The exact count needs the
shared attribution logic and is part of the build, not a guess.

### Superseded: the earlier scalar reading

The first confirmation (kept for the record) was *"two entries per cross signal; an exit does not
refill the slot"* — that remains true about EXITS (an already-exited entry still consumes its slot,
so FUSE 17:03 is a true breach) but it is **not sufficient**, because it says nothing about
composition.

FUSE forced the question: its first entry had already exited on its own bracket before entries 2 and
3 fired. Two readings behave differently on exactly that shape, so the intent is now on record rather
than assumed:

| | rule | FUSE 17:03 |
|---|---|---|
| **CHOSEN** | ≤2 entries per cross; an already-exited entry still consumes its slot | **BREACH** (3 taken) |
| rejected | ≤2 positions held simultaneously; a re-entry after a clean exit is legitimate | mostly fine |

⇒ The fix counts entries **taken**, not entries **currently open**.

## ⛔ THERE ARE TWO DEFECTS HERE, NOT ONE

Found while scoping the fix — either alone still lets a third entry through:

1. **The arm resets the counter** (`cw_entries_this_flip = 0` in the BUY-flip arm block).
2. **The resting fill never increments it at all.** The only `+= 1` is on the REACTIVE path
   (`schwab_1m_v2.py:1535`). A resting entry — the live default since 07-22 — consumes **no slot**,
   so even without the reset it would not be counted.

### ⚠️ What defect 2 does to HISTORICAL entry-count data — stated precisely, not over-claimed

The resting path READS the counter and stamps `cw_entries_this_flip + 1` (`:1645`); it just never
advances it. So:

- a resting entry's OWN label is correct for its position in the segment
- but any entry that FOLLOWS it on the same leg in the same segment is **under-numbered by one**,
  because the counter never moved

⛔ **Do not claim "cw_entry_n is pinned at 1 since 07-22" — that is false.** Checked: resting
(`STOP_LIMIT`) entries since 07-22 carry n=1 (19), n=2 (4) and n=3 (9), because prior REACTIVE
entries in the same segment did advance the counter.

⛔ **And do not read duplicate labels as proof of corruption.** Segments with two entries sharing a
label (AMIX, AXTU, APLX, AXTL) turned out to be **fan-out twins** — Schwab qty 2 plus its Webull
qty 1 two seconds later, identically stamped BY DESIGN (#570). That is correct behaviour, not a bug.

⇒ Honest position: the under-count is real **in mechanism**, but its historical scale is
**unquantified** — resting entries carry `cw_arm_bar_ts=0`, so they do not group with their own
segment and the corruption cannot be counted from the recorded data. Treat resting-path entry counts
as unreliable in a KNOWN DIRECTION (too low); do not build a study on them and do not put a number
on it.

### ⛔ The increment must land on BOTH legs

The fan-out is where today's phantoms also live, so increment-on-fill has to cover the Webull leg as
well as the Schwab one. If only the primary counts, a fan-out-only cross (Schwab rejected — which
happened to UPC today via the API-open block) would still never consume a slot.

## The fix

**Two changes, both required:**

1. **Move the reset from ARM to DISARM.** A cross's entries begin when the previous segment ended, so
   zeroing at the disarm means the counter arriving at the arm already holds exactly the entries
   attributable to the cross being confirmed — including a resting fill that preceded it by minutes.
   This is the same attribution rule the detector needs; compute it once, use it in both.
2. **Make a resting fill consume a slot** — increment on fill, not on placement (placement can be
   repriced away without ever trading).

⛔ The boundary is the whole design: a genuinely NEW later cross has taken zero entries, so it still
starts fresh at 0 and gets its full two. The fix must not over-correct into blocking legitimate
re-arms.

## Acceptance criteria

1. Each of the four live breaches yields a legal composition, never 3 entries.
2. **COMPOSITION:** `resting+reclaim` allowed · `reclaim+reclaim` BLOCKED · `resting+resting` BLOCKED.
3. **BOUNDARY:** a real second cross after a completed first starts clean and gets its own
   resting+reclaim — the fix must not over-correct into blocking legitimate re-arms.
4. **DEGENERATE:** resting never filled ⇒ reclaim allowed, reactive BLOCKED, one entry max.
5. An exited entry still consumes its slot (no refill).
6. The increment lands on BOTH legs, so a fan-out-only cross is still bounded.

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
