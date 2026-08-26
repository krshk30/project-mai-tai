# The fan-out leg lifecycle — one loop, five blind stages

**DESIGN ONLY. Nothing here mutates code, config, or production.** Written by `claude-1`,
2026-08-26, against `main` `0b4d5e23`. Every number carries the population it was measured over.
Needs `codex-2`'s review before any part of it is built.

> ## ⛔⭐⭐ THE FRAME
> These are **not five proposals.** They are five consecutive stages of **one loop**, and the loop
> is open: a fan-out leg is emitted, reaches a venue, and **its outcome never returns to the thing
> that decided to emit it.** Every symptom below — unattributable fills, a 12:1 order-to-fill ratio,
> duplicate legs, slots that do not deplete — is that one open loop observed from a different angle.
>
> ⇒ **Fixing them independently is how we get five partial fixes and no closed loop.** The order
> below is a dependency order, not a priority list.

```
  ARM ──▶ EMIT ──▶ VENUE ──▶ OUTCOME ──▶ NEXT DECISION
   │        │         │          │             │
   │        │         │          │             └─ §4 slots never deplete (a Webull-only fill
   │        │         │          │                consumes nothing) — an OPEN QUESTION
   │        │         │          └─ §3 the outcome never returns: the position query is
   │        │         │             Schwab-scoped, the leg fills on live:orb
   │        │         └─ §2 12:1 rows-to-fills, and most "rejects" are ours
   │        └─ §1 no identity survives the process that minted it
   └─ §5 venue truth: an undecided DEPENDENCY, deliberately not designed here
```

---

## §1 — A durable shared identity (stage: ARM → EMIT)

### What exists today

| mechanism | where | lifetime |
|---|---|---|
| `client_order_id` | per `broker_orders` row | one order. **A replacement gets a brand-new one and no link back** |
| `fanout_segment_id` (#790) | `SymbolState`, stamped into the payload at `schwab_1m_v2.py:2146` | **the process.** Reset at `:660`, `:1203`, `:1706`; gone on restart |
| `_combo_leg_coid` | Webull combo legs | one combo, 40-char cap |
| `watchdog_replaces_client_order_id` | payload, optional | exists; effectively never written (below) |

### Measured

- **Replacement-link coverage, 21 days:** `live:orb` **0 of 12,530**; `live:schwab_1m_v2` **4 of
  1,996**. A replacement is a new row with a new identity and **no edge** to what it replaced.
- **Rows vs identities, `live:orb` buy side, 08-19 → 08-25:** TNON 75 rows / **75** distinct
  `client_order_id`; PMI 50/50; EXYN 48/48; JUNS 46/46. One-to-one, every time — confirming that
  what looks like "one intent, many attempts" is stored as *many unrelated intents*.
- **Segment-id coverage in payloads, `live:orb`, last 7 days:** `fanout_segment_id` present on
  **0 of 660** orders. ⛔ That zero is **pre-#790** — #790 deployed 2026-08-26 01:15 UTC and
  `live:orb` has placed **0 orders since**. Post-#790 coverage is **UNMEASURED, not zero.**
- `fanout_segment_id` derives from `cw_arm_bar_ts`, which is **0% present on `eh_resting` fills**
  (0 of 26, 08-01 → 08-19) and 20% on Schwab resting — see `segment-identity-coverage.md`. When it
  is absent the code falls back to the last bar timestamp, then to a synthetic `max(...)`.
  ⇒ **the id is not merely process-local, it is derived from a field that is missing exactly where
  the attribution gap lives.**

### The design

**Mint one id per (symbol, cross) at the ARM, before either leg is emitted; persist it in Postgres;
stamp it on every order both legs place, including every replacement.** Three properties, each the
answer to a specific failure above:

1. **Minted before the emit** — not derived from a field a leg may lack (`cw_arm_bar_ts`).
2. **Persisted outside `SymbolState`** — survives the restart that today silently starts a new
   numbering, so a leg emitted at 09:31 and one emitted at 13:05 after a bounce stay distinguishable
   instead of colliding.
3. **Carried by replacements** — the missing edge, and the reason a 75-row symbol-day cannot be
   collapsed to the handful of decisions that produced it.

**Addresses:** signal-4 blindness (filled fan-out legs with no segment id); the per-lot attribution
gap; the replacement-link half of `no_replacement_link_in_order_chain`.

**Does NOT address:** ⛔ the fill rate, the reject rate, duplicate legs, or slot accounting. It
changes **nothing** a trader would see. It is the prerequisite that makes §2 and §3 *measurable*,
and claiming more for it would be the "observability sold as a fix" mistake.

---

## §2 — Why N orders make ~2 fills (stage: VENUE)

⛔ **This section explains; it does not propose.** No fix is designed here, because the number is
not yet one phenomenon.

⛔⭐⭐ **On the "81 orders → ~2 fills" figure: I could not reproduce it.** No symbol-day in
`broker_orders` since 08-19 reads 81 → 2 on either live account. The nearest measured pairs are
below. If 81 came from a different table, window, or union of accounts, **that population needs
naming before the ratio means anything** — and the mechanism below holds regardless of which
denominator turns out to be the right one.

### Measured — `live:orb`, buy side, whole sessions

| ET day | orders | fills |
|---|---:|---:|
| 08-19 | 188 | 9 |
| 08-21 | 156 | 9 |
| 08-24 | 67 | 9 |
| 08-25 | 79 | 10 |

Worst symbol-days: TNON 08-19 **75 → 3**; PMI 08-24 **50 → 4**; EXYN 08-21 **48 → 1**; JUNS 08-21
**46 → 3**. The same shape appears on the Schwab primary (TNON 75 → 8, XPON 08-24 66 → 8)
⛔ **so this is not a Webull property.**

### The ratio is three unrelated things added together

**(a) Our own aborts, recorded as the venue's rejects — 179 of 208 buy rejects, 7 days.**
The stored reason is `Webull order rejected: RuntimeError("Webull combo MASTER must be LIMIT or
MARKET...")`. That is **our client-side guard** firing; the order never reached Webull. This is
`broker_order_events_conflates_client_aborts` in the entry path: ⛔ **every reject rate quoted for
this leg is contaminated** — in the direction that flatters the venue and blames us, which happens
to be correct here, but was assumed rather than measured.

**(b) A real venue refusal on the EXIT side — 145 `sell market` + 11 `sell limit`,**
`NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K`. The venue is saying *you hold nothing
to sell*. ⛔ This is not an entry-conversion problem at all — it is **§3's open loop surfacing as
rejects**, and it is why (b) must never be counted into an entry ratio.

**(c) Replacement churn — the largest term, and the one that makes the ratio meaningless.**
08-25 `live:orb` STOP_LIMIT, by symbol: AIXI 23 rows / 9 cancelled / 4 filled · PMI 19 / 17 / **2** ·
BTCT 12 / 11 / 1 · DAIC 12 / 11 / 1 · WVVIP 11 / 11 / **0**. Every row is a distinct
`client_order_id` with no link to its predecessor.

⇒ **The denominator is not "attempts to trade". It is "rows we wrote."** A resting entry that
reprices 20 times and fills once is *one* decision that succeeded, stored as 20 failures and 1
success. Any ratio built on `count(broker_orders)` is measuring our own reprice cadence.

**Addresses:** nothing yet — deliberately. What it establishes is that the fix is **not** "make more
orders fill".

**Does NOT address:** ⛔ whether the reprice cadence is itself too high (that is the KUST exit-churn
question, a different workstream), and ⛔ whether (b) is a *protection* failure or an *accounting*
failure — decided in §3, not here.

⭐ **The honest ratio cannot be computed until §1 ships**, because the correct denominator is
"distinct (symbol, segment) intents" and that identity does not exist yet. ⇒ §2 is a **consumer** of
§1, which is why §1 is first.

---

## §3 — The Webull outcome loop (stage: OUTCOME)

### The gap, in code

`_fetch_position_maps` (`services/schwab_1m_v2_bot.py:1215`) scopes every read to
`self.settings.strategy_schwab_1m_v2_account_name` — the **Schwab** account — for both
`virtual_positions` and in-flight intents. The fan-out leg fills on **`live:orb`**.

⇒ **A Webull-only fill moves neither `position_qty` nor `position_qty_held`.**

⛔⭐⭐ And a load-bearing comment in `update_position` asserts the opposite — *"BOTH LEGS, for free:
SymbolState is per SYMBOL, not per account"*. `SymbolState` being per-symbol is **true and
irrelevant: the query that feeds it is per-account.** Anything resting on that comment is wrong too.

### Consequences, both already costed

- **Duplicate legs.** The emit claim releases on `position_qty == 0`, which for a Webull-only fill is
  *permanently* true, so the timeout matures and a second leg goes out. The gate meant to say
  *"nothing came of that emit"* actually says *"we cannot see what came of that emit."* Prior
  measurement: 22 duplicate legs, all filled worse, **median 4.58%**.
- **`fanout_webull_claimed` re-armed on a spurious close.** The close-detect block fires on the
  **union** reaching 0 — so one of our own resting intents going terminal triggers it. The correct
  discriminator is already computed one line above and thrown away into a log string:
  `spurious = prev_held == 0 and state.position_qty_held == 0`.

### The design

**Close the loop with positive evidence only, sourced from `fills`.**

> ⭐⭐ **The rule, operator-set and unchanged:** *a non-releasing counter trades a duplicate-fill
> defect for the silent-no-order defect.* **The latch may only be HELD by positive evidence that the
> leg exists; absence of evidence must keep RELEASING it.** That preserves today's failure direction
> (duplicate — visible and costed) and never introduces the invisible one. **Any held latch must
> log.**

⛔ **Source ranked: `fills` (append-only) OVER `virtual_positions`.** `virtual_positions` carries the
known `[VIRTUAL-CLEAR]` false-zero — a live row zeroed 0.7 s after the fill and never restored — so
building on it would bring the duplicate back **through the fix, while it behaved exactly as
designed.**

⭐ **Split, and build cause 3 alone first.** Cause 3 (the spurious re-arm) needs no new query, field,
or source — the discriminator already exists. Ship cause 3 and cause 2 together and the improvement
is **unattributable**; ship cause 3 first and the residual duplicate rate *attributes* how much
cause 2 actually owns.

**Addresses:** duplicate fan-out legs; the permanent-release defect; and the §2(b) exit rejects, to
the extent they are caused by us asking a venue to sell something our own books say we do not hold.

**Does NOT address:** ⛔ **protection of the leg after it fills.** A fixed mirror fills **bare**, and
#689's re-protect attach has **never once succeeded**. Closing the outcome loop makes the bare fill
*visible*; it does not make it *protected*. ⛔ Nor does it cover anything the venue did that we hold
no record of — that is §5.

---

## §4 — Is suppression-by-slot intended? (stage: NEXT DECISION)

⛔⭐⭐ **This is a QUESTION FOR THE OPERATOR, not a design. It must be answered BEFORE §3 is built,
because §3 changes the answer as a side effect if nobody decides it deliberately.**

### What is true today

The entry cap is **composition**, not a count (#644): exactly **one resting and one reclaim** per
cross. A scalar `entries >= 2` would permit reclaim+reclaim, which is why it is not used. The
degenerate case is settled: if the resting entry never filled, **its slot is forfeit** — reactive may
not substitute into it. Operator's rationale, on record: *never trade a type the operator did not ask
for.*

**But the slots are fed by the Schwab-scoped query in §3.** So today:

> **A Webull-only fill consumes NO slot.** A single cross can produce one Schwab entry *and* an
> unbounded number of Webull-leg entries, because nothing the Webull leg does ever depletes the
> composition.

### The two readings, and why the difference is not cosmetic

| reading | slots govern | consequence if we choose it |
|---|---|---|
| **A — intended** | the *v2 strategy's own* exposure at its own broker; the fan-out leg is a mirror accounted separately | today's behaviour is correct. §3 must then close the loop for **duplicate detection only**, and must NOT start depleting slots |
| **B — unintended** | the **cross's** total exposure across both venues | today's behaviour is a live defect: a cross exceeds its authorised composition on the Webull side, and §3 closing the loop **fixes it as a side effect** |

⛔ Under reading A, §3 quietly depleting slots is a **regression** — fewer entries than the operator
authorised. Under reading B, §3 *not* depleting them leaves the defect standing. **The same code
change is correct or incorrect depending only on an answer nobody has written down.**

⇒ ⭐ **Ask before building §3. Do not let the implementation pick.**

**Addresses:** nothing — it is a decision gate.

**Does NOT address:** ⛔ it does not tell us what the *right* composition across two venues is; it
only forces us to say which one we are currently claiming.

---

## §5 — Venue reconciliation: an undecided dependency, NOT designed here

**This section exists to name a dependency and refuse to design it.** Everything above reasons from
*our* records. Where a leg's outcome is not in our records at all, only the venue can answer — and we
do not yet know whether the venue's history endpoint can.

`get_order_history` is **not a reconciliation source** until five things are measured: coverage back
to 08-03 · combo exit-child visibility · partial-fill semantics · freshness versus detail · cursor
integrity.

Any probe that tries to establish this must, per the protocol already agreed:

- enumerate to discover, then **detail-call every listing miss** before concluding absence;
- page to a **proved terminal condition**, printing page and request counts;
- pace at **two requests per two seconds**, and run **after** the trading window;
- return exactly one of `found` · `confirmed-absent-via-detail` · `COULD_NOT_TELL` · `VOID`;
- ⛔ mark the assay **VOID** if the five known-positive 2026-08-21 combo IDs do not reproduce — a
  failing control voids the probe; it never becomes a negative result.

⚠ **One dependency deadline has MOVED and should not be quoted from memory.** The five control combo
IDs were expected to age out of the logs around **08-29** under a 7-day policy. Retention is now
**`daily`, `rotate 30`** (verified on the box today), so that pressure is off — the controls should
survive to roughly **09-20**. ⛔ The durable transcription in `docs/deploy-2026-08-24-window.md`
remains the primary source; the log is now a longer-lived backup, not a fresh deadline to race.

**Addresses:** nothing. Naming it is the deliverable.

**Does NOT address:** ⛔ it is **not** a prerequisite for §1 or §3 — both are buildable and gradeable
from our own append-only `fills`. Treating §5 as blocking would stall the loop on the one part we
cannot yet size.

---

## What this design cannot know

1. **Whether the venue ever saw the orders we aborted client-side.** 179 of 208 rejects never left
   our process. We are inferring Webull's behaviour from a population Webull never received.
2. **What the true intent count is, historically.** With no replacement link on 12,530 `live:orb`
   orders, ⛔ **no retrospective study can collapse rows into decisions.** Every ratio computed over
   history — including §2's — is a rows ratio and will stay one. §1 fixes this **forward only.**
3. **Whether post-#790 identity actually works.** Coverage is UNMEASURED: `live:orb` has placed no
   orders since #790 deployed. ⛔ Not zero. Not proven. No population yet.
4. **Whether the exit rejects in §2(b) are an accounting artifact or a real unprotected position.**
   Distinguishing them needs the venue's own book — §5, undecided.
5. **What the right cross-venue composition is** (§4). An operator preference, not a fact
   discoverable in the data.
6. **Whether closing the loop changes fill *quality*.** Everything measured here is counts. The 22
   duplicates filled at a median 4.58% worse; whether the *non*-duplicate legs are priced well is a
   separate study with a separate denominator.

---

## What would falsify it

Stated so each one is checkable, and so a negative result kills the relevant section instead of being
absorbed into it.

| # | claim | what would falsify it |
|---|---|---|
| 1 | the fan-out identity is not durable | post-#790 legs come back carrying `fanout_segment_id` that is **stable across a v2 restart** and **unique per cross** over ≥2 sessions ⇒ **§1 is already solved and must not be built.** ⭐ The cheapest check on this page; it runs on the next session with fan-out activity |
| 2 | the order-to-fill ratio is dominated by replacement churn | collapse a symbol-day by `(symbol, segment)` once §1 ships; if the collapsed intent count is still ≈ the row count, churn is **not** the term and §2 names the wrong mechanism |
| 3 | a Webull-only fill is invisible to the strategy | a fill on `live:orb` with no Schwab leg **moves `position_qty_held`** in a live read ⇒ the scoping claim is wrong and §3's premise collapses |
| 4 | duplicates are caused by the permanent release | build cause 3 alone; if the duplicate rate does not move **at all**, cause 3 owns none of it and cause 2 owns the whole defect — the split was designed to produce exactly this reading |
| 5 | slots do not deplete on a Webull-only fill | a cross that filled Webull-only is observed **refusing** a subsequent same-type entry ⇒ suppression already spans venues and §4's question is moot |
| 6 | `fills` is a safe source | a `fills` row is observed being **deleted or mutated** after write ⇒ the append-only assumption fails and §3 must be re-sourced |

⛔ **A zero on any of these is not a pass.** Each needs its denominator stated on the line — several
of these populations are empty today.

---

## The first increment

> ## ⭐⭐ Increment 1 — mint and stamp the durable identity. Observation only.
> **One window. No behaviour change. No new decision path.**

**Scope:** mint the id at the ARM, persist it, stamp it on both legs' orders and on every
replacement. **Nothing reads it to make a decision.**

**What it proves ALONE — the whole reason it goes first:**

1. **That both legs can be joined at all.** Today they cannot, on any historical population. One
   session with fan-out activity yields a coverage number: *N of M filled fan-out legs carry an id
   that matches a Schwab leg's.* ⛔ State M, or the number means nothing.
2. **That the id survives a restart** — the property `fanout_segment_id` does not have, checked by
   spanning any restart inside the window.
3. **That replacements chain** — the first non-zero reading of a link that is 0 of 12,530 today.

**What it deliberately does NOT prove:** ⛔ nothing about duplicates, fills, slots, or protection. If
increment 1 makes any of those numbers move, **something outside its scope changed** — that is a
finding, not a success.

**Why this and not §3 first:** §3's grade is a *rate* (duplicates per fan-out cross), and that rate
has **no trustworthy denominator until §1 exists**. Building §3 first means fixing it blind and
grading it against a rows ratio already shown to be dominated by our own reprice cadence.

**If it cannot ship in one window, it is scoped wrong.** The failure mode to watch for is increment 1
growing a *consumer* ("while we're in here, let the latch read the new id"). ⛔ That is §3, it is
gated on §4's unanswered question, and it must not ride along.

**Sequence after it — context only, not authorised by this document:** increment 2 = §3 cause 3
alone (needs §4 answered first) · increment 3 = §3 cause 2, graded on the residual · §5 stays
undecided until someone sizes the probe.

---

## Provenance

Measured read-only against production on 2026-08-26 between 08:05 and 09:10 ET, while the fleet was
flat and before the 09:30 open. Code read at `main` `0b4d5e23`. No production state was mutated.
Populations: `broker_orders` / `fills` joined on `broker_accounts.name`, windows stated per table.

`[[project_mai_tai_webull_mirror_born_broken]]` ·
`[[project_mai_tai_no_replacement_link_in_order_chain]]` ·
`[[project_mai_tai_virtual_positions_false_zero]]` · `[[project_mai_tai_entry_composition_cap]]` ·
`[[project_mai_tai_broker_order_events_conflates_client_aborts]]` ·
`[[project_mai_tai_per_lot_attribution_gap]]`
