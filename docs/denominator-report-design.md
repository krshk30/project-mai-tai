# The Denominator Report — DESIGN ONLY, awaiting operator review

**Status:** design. No code. Nothing scheduled. Written 2026-08-14.
**Extends** `ops/health/fleet_health_check.py` (F3) and `docs/fleet-health-validation-design.md` —
this is a new *section* in an existing system, not a parallel one.

> **The ask:** consolidate the ad-hoc morning checks into one scheduled report; express every check
> as a ratio so a zero can be read; flag the step change, not a threshold; a report, not a pager.

---

## 0. ⛔⭐⭐ READ THIS FIRST — the motivating premise does not survive the data

The design was requested to catch "three silent stoppages this week." I measured all of them before
building anything. **Two of the three are not stoppages, and the one real incident is invisible to
the design as specified.** The numbers are below; the design after them is built for what actually
happened.

### 08-07 "brackets stopped" — no. The denominator was zero.

Bracket ratio at symbol-day grain, `live:schwab_1m_v2` (symbols with a buy fill → symbols with an
`oco_exit`):

| day | 07-29 | 07-30 | 07-31 | 08-03 | 08-04 | 08-05 | 08-06 | **08-07** | 08-10 | 08-11 | 08-12 | 08-13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ratio | 100% | 88.9% | 80% | 100% | 100% | 100% | 100% | **no row** | 100% | 100% | 100% | 100% |

**08-07 has no row because there were zero Schwab buy fills that day** (15 resting entries placed,
0 filled — against a trailing fill rate of ~10%, `0.9^15 ≈ 21%`, unremarkable). Zero fills ⇒ zero
brackets required ⇒ **0 brackets was the correct output.** This is precisely the operator's own
principle, and it points the other way here: the numerator *and* the denominator were zero, so the
day is quiet, not defective.

### 08-11 "resting entries stopped" — no. The ratio was normal.

`live:schwab_1m_v2`, resting share of all buy orders:

| day | 07-29 | 07-30 | 07-31 | 08-03 | 08-04 | 08-05 | 08-06 | 08-07 | 08-10 | **08-11** | 08-12 | 08-13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| resting % | 83.3 | 83.3 | 84.7 | 88.2 | 81.3 | 86.8 | 86.3 | 88.2 | 83.2 | **87.8** | 98.2 | 95.3 |

**87.8% sits mid-band.** The resting path never stopped. What happened on 08-11 was the P0
boot-hold suppressing entries for 2h22m
(`project_mai_tai_v2_post_boot_promotion_uncapped_fleet_hold`).

### ⛔ And the decisive finding: all three detector families miss 08-11

| detector | 08-11 | comparison | verdict |
|---|---|---|---|
| **ratio** | 87.8% | band 81–98% | mid-band — **misses** |
| **daily volume** | 49 buys | trailing median ≈ 51 | dead on median — **misses** |
| **longest intraday gap** | 67 min | 08-07 **144**, 08-04 **105**, 08-06 **80** | *below* three normal days — **misses** |

**A pure-ratio report would not have caught the one incident it was commissioned to catch.**

### Why — and this is the whole design

Hourly resting placements, `live:schwab_1m_v2`:

| ET hour | 07 | 08 | 09 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|---|
| 08-10 | – | – | 3 | 12 | 15 | 25 | 3 | 25 | 11 |
| 08-11 | – | – | 2 | 13 | 1 | 10 | 7 | 9 | 1 |
| 08-12 | – | – | 7 | 20 | 51 | 66 | 50 | 62 | 74 |
| 08-13 | – | – | 12 | 15 | 29 | 53 | 13 | 27 | 34 |

**Hours 07 and 08 are structurally empty on every day** — extended hours arm a soft rest, they do
not place a broker order. The 08-11 loss was *in that window.* The entry stream physically cannot
show a suppression that happens where the entry stream is already zero.

> ### ⭐⭐ THE RULE THIS YIELDS
> **A denominator must live UPSTREAM of the failure it is meant to detect.**
> Counting entries cannot detect a suppressed entry — the suppression removes the thing you are
> counting, numerator *and* denominator together, and the ratio stays perfect while output goes to
> zero. The denominator for a suppression defect is the **flip/arm that should have produced an
> order**, never the order.

This generalises the ask rather than contradicting it: "of N entries, M rested" is the right shape,
but only when N is measured *before* the step that can fail.

---

## 1. What gets measured

Each row is one check. `N` is always sourced upstream of the failure mode.

| # | check | denominator N (upstream) | numerator M | baseline today | catches |
|---|---|---|---|---|---|
| 1 | **entry conversion** | flips/arms that passed the gates | entry orders placed | *to be established* | 08-11-class suppression |
| 2 | **resting share** | entry orders placed | placed as `STOP_LIMIT` | 81–98% | routing regression |
| 3 | **fill conversion** | resting orders placed | filled | ~7–15% | dead/mispriced rests |
| 4 | **bracket coverage** (RTH) | symbol-days with a buy fill | symbol-days with `oco_exit` | schwab **100%**, orb 75–100% | 08-13-class attach failure |
| 5 | **protection coverage** | positions held >60s | with broker-side protection | *to be established* | naked-position window |
| 6 | **exit level quality** | exits filled | filled at level vs below | *to be established* | slippage regression |
| 7 | **orphan rate** | resting orders placed | left non-terminal at EOD | ~0 | FRTT-class orphans |

**Check 1 is the one that matters and the one that does not exist yet.** It needs a flip/arm
denominator that the order tables cannot supply — the source is v2's own arm markers, cross-checked
against the gate's suppression reason. Scoping it is the first build step, not an afterthought.

### ⛔ Structural zeros must be declared, not discovered

`live:orb` resting share is **0.0% on every one of 12 days** — the fan-out leg does not place
`STOP_LIMIT`. A naive "was non-zero, went to zero ⇒ page" rule pages on it daily. Every check
therefore carries an explicit `applies_to` set, and a ratio outside it is printed as `n/a
(structural)` — never as 0%.

### ⛔ Aggregation hazard on check 4

`live:orb` bracket coverage is 75–100% and **the variance is the EH/RTH mix**, not a defect —
pre-market is 0% bracketed by broker limitation (`0/34`, `0/13` over 14d). Computing it across both
sessions lets EH fills dilute the ratio and **mask a real RTH failure**. Check 4 is RTH-only, with
the EH count reported beside it as a separate, explicitly-unbracketable population.
See `feedback_aggregation_masked_the_event`.

---

## 2. Step change, not threshold

For each check, per account:

```
today's ratio   vs   previous trading day
                vs   trailing 5-day median (weekends/holidays dropped, never zero-filled)
```

Report bands:

| condition | label |
|---|---|
| N = 0 | `QUIET` — denominator empty, nothing to conclude |
| ratio within ±1 median-absolute-deviation of trailing median | `STEADY` |
| ratio moved > 2 MAD | `MOVED` — printed with both numbers |
| ratio was non-zero for ≥3 of the last 5 days **and is now 0 with N > 0** | `STOPPED` — **the only pageable state** |

MAD rather than standard deviation: these ratios are small-count and skewed, and one 100% day
should not widen the band. `STOPPED` deliberately requires **N > 0** — that single clause is what
separates a defect from a quiet day, and it is the clause 08-07 would have failed.

⛔ **Volume gets its own row, separate from ratio.** A ratio holding steady while N collapses is
the 08-11 shape. Both are reported; only the ratio is pageable.

---

## 3. Report, not pager

**One scheduled run, one artefact.** Extends the F3 `CHECKS` registry pattern
(`(level, name, detail)` + a single `VERDICT:` line) rather than inventing a second convention.

- **When:** once daily, **after the close and after 20:00 ET.** #699 removed the rotation deadline —
  rotated log siblings are read, so a post-close run sees the complete day. An intraday run is
  explicitly *not* a result (the day has not happened yet).
- **Where:** a dated file under `/var/log/project-mai-tai/reports/` plus the same text on stdout;
  the existing ntfy path is used **only** for `STOPPED`.
- **Volume:** ~7 checks × 2 accounts, one screen. Every line carries `M/N` — never a bare percentage
  and never a bare count.

> ⚠️ **The no-new-pager constraint is a first-class requirement, not a preference.** There are
> already 8 crons on ntfy. `STOPPED` is scoped to fire on the order of *once per real incident*; on
> the 12 days measured it would have fired **zero times** — which is the correct answer, since two of
> the three alleged stoppages were quiet days and the third is invisible to these signals until
> check 1 exists.

---

## 4. Every check proves it can see — the #699 pattern, per check

A check that cannot demonstrate it would see the thing is not a check. Each carries a **control**:
a past date with a known non-zero population, asserted at run time.

```
✓ CONTROL bracket coverage — 2026-08-12: 7/7 schwab, 7/8 orb   (>= 5 expected)
✗ CONTROL entry conversion — 2026-08-12: 0 arms visible        ⇒ CHECK VOID
```

A failed control makes that check **VOID**, never `QUIET` and never `STEADY` — the §0b/`CONTROL_VOID`
mechanism already merged in #699, reused, not rebuilt.

⛔ **Control days must be chosen on population, not on narrative.** 2026-08-07 has zero filled rests
and zero brackets: picking it as a "before the change" control would pass **vacuously**. The
candidate control day is **2026-08-12** (330 rests, 31 fills, 32 brackets, 25 orb fills).

⛔ **Control days age out.** Log-backed controls die with retention (~6 days observed); DB-backed
controls persist. Where both exist, the control is DB-backed and the log path is controlled
separately. The report states which of the two it used.

### What this report cannot see — stated on every run

1. **The broker's book.** Webull OCO children are broker-created and never land in `broker_orders`.
   "A pair is resting at Webull right now" is unknowable here — **the operator's screen is the
   primary source and beats our records.**
2. **Per-lot attribution.** Everything above is symbol-day grain; the per-lot gap is open and blocks
   finer analysis, not just finer reporting.
3. **Manual activity.** The broker's book is shared; manual buys appear in these denominators.
4. **Intent that never reached an order.** Until check 1 exists, a suppression upstream of order
   creation is invisible — **this is the 08-11 hole and it stays open until check 1 is built.**

---

## 5. Build order

1. **Check 1's denominator** — scope the arm/flip source. Without it the report cannot catch the
   incident that motivated it. Everything else is already computable today.
2. Checks 2, 3, 4, 7 — pure SQL over `broker_orders`/`fills`, baselines above already measured.
3. Step-change + `STOPPED` gate, backtested over ≥30 days before anything is wired to ntfy.
4. Checks 5, 6 — need definitions the operator should set (below).
5. Schedule, after a week of the report running silently to establish real fire-rates.

## 6. Open questions for the operator

1. **Check 1's denominator** — is an arm/flip the right unit, or should it be "confirmed-watchlist
   symbol-minutes inside the trading window"? This decides whether 08-11 is catchable.
2. **Check 5** — what counts as "protected"? A Schwab native OCO and a Webull attached pair are
   different objects; the Webull one is unverifiable from our side (§4.1).
3. **Check 6** — "filled at the level vs below": which level — the decided price, the trigger, or
   the NBBO at submit? `project_mai_tai_resting_does_not_avoid_the_spread` warns a fill-vs-quote
   number is uninterpretable without the tape.
4. **Report delivery** — file + stdout only, or also a daily ntfy at low priority? The stated
   constraint argues for file-only, with ntfy reserved for `STOPPED`.
