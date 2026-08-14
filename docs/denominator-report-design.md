# The Denominator Report — DESIGN ONLY, awaiting operator review

**Status:** design. No code. Nothing scheduled. Written 2026-08-14.
**Extends** `ops/health/fleet_health_check.py` (F3) and `docs/fleet-health-validation-design.md` —
this is a new *section* in an existing system, not a parallel one.

> **The ask:** consolidate the ad-hoc morning checks into one scheduled report; express every check
> as a ratio so a zero can be read; flag the step change, not a threshold; a report, not a pager.

---

## §0. ⭐⭐ THE MOTIVATING INCIDENT — 2026-08-14, 07:40 → 07:47 ET

**Both failure modes, from one missing denominator, inside fifteen minutes.** This is not a
hypothetical: it is a live, reproducible instance that happened while this document was being
written, and it is a better motivating case than anything in §1.

### 07:40 ET — the false RED

```
🚨 RED v2-bar-continuity  schwab_1m_v2 BAR HOLE 22min on 1 symbol(s)
[backfill] LBGJ: filled 21/21 missing bar(s) from REST
```

LBGJ did not lose data. **LBGJ left the watchlist**, so v2 correctly stopped receiving bars for it:

```
11:15:56 UTC  watchlist count=4  AKAN,LBGJ,SXTC,WETO    <- on the list
11:16:33 UTC  watchlist count=3  AKAN,SXTC,WETO         <- GONE   -> [V2-WS-SUB] UNSUBS LBGJ
11:38:06 UTC  watchlist count=4  AKAN,CGTL,LBGJ,WETO    <- BACK   -> [V2-WS-SUB] ADD   LBGJ
```

07:16 → 07:38 ET, matching the reported hole to the minute. **The designed behaviour, reported as a
defect.** No position and no resting order existed on LBGJ (zero `live:schwab_1m_v2` orders all day;
the only LBGJ orders were two `paper:polygon_30s` fills), so the alert's own restart criterion was
correctly unmet.

### 07:45 ET — the tautological GREEN

```
✅ OK v2 bar series contiguous again  (130 bars, no gaps)
```

The watch reads **only the last 30 minutes** (`bar_gap_watch_cron.sh:65`). At 07:45 that window is
07:15–07:45 — **exactly the range the auto-repair had backfilled five minutes earlier.**

⇒ **The watch confirmed its own INSERT.** The identical GREEN would print whether or not the
condition persisted. **And it did persist:** LBGJ churned off the watchlist again at 11:47:35 UTC —
**two minutes after the all-clear.**

### ⭐⭐ THE SHARPEST FORM — put this in front of anyone who defends the current shape

> **A symbol that churns off produces a gap and reads RED; a symbol that stays off produces no gap
> and reads GREEN. Same underlying state, opposite verdicts, and neither says anything about feed
> health.**

Measured in the same 07:00–07:40 window: **LBGJ 35 bars → RED.** **BOXL 4 bars, IPW 2 bars → no
alert at all.** The symbols with almost no data were the ones that read clean, because a symbol that
is mostly absent never presents a "hole" to find.

### And a third instance in the same incident — the guard confirmation

The RED instructs: *"confirm `[V2-ATR-BAR-GAP]` fired for these names."* It did — once, at 06:04 ET.
Decoding the bar timestamps in that line:

| field | value | decoded |
|---|---|---|
| `prev_bar_ts` | 1784238300000 | **2026-07-16 21:45 UTC** |
| `cur_bar_ts` | 1784238660000 | **2026-07-16 21:51 UTC** |
| live bar at 11:15 | 1786706040000 | 2026-08-14 11:14 UTC |

**It fired on warmup-replay bars 29 days stale**, for a 6-minute gap in July. It never fired for the
07:16–07:36 hole — correctly, because there was no live gap to span. But the confirmation the alert
asks for is **satisfiable by a firing that has nothing to do with the gap in question.** A
confirmation step that any historical firing satisfies is not a confirmation.

### ⛔ THE TWO INVARIANTS THIS INCIDENT BUYS

> **I1 — THE DENOMINATOR IS WATCHED MINUTES, NOT WALL-CLOCK MINUTES.**
> *Of N minutes the symbol was on the watchlist, M bars arrived.* Off-list minutes are excluded from
> N, not counted as missing. A ratio in this shape **cannot be silently zero**: if N = 0 the symbol
> was not being watched and the day is quiet; if N > 0 and M = 0 the feed is genuinely dead. The
> current check conflates those two into one RED.

> **I2 — A VERIFICATION MUST NOT BE SATISFIABLE BY OUR OWN ACTION.**
> The re-check window must **not overlap the range we just wrote to**, or must extend strictly wider
> than the repair. Verifying a backfilled range against the backfill is a closed loop that always
> closes. **This is the same class as the dead guards dominated by an earlier return** — it reads as
> verification and provides none. See `project_mai_tai_dead_guards_dominated_by_an_earlier_return`.

I2 generalises past this watch: **any** check that both repairs and verifies needs it, and the
"detect → fix → confirm fixed" pattern is exactly where it hides.

> **I3 — AN ALERT MUST NOT DELEGATE A CHECK IT CANNOT ITSELF PERFORM.**
> Either mechanise the check and print the answer, or delete the line. An instruction that cannot be
> carried out has exactly two outcomes, both bad: the reader burns time discovering it is
> unanswerable, or the reader learns to skip instructions in that alert — and the *next* instruction
> in the same body is the do-not-restart rule, which is the one that must never be skipped.

### ⛔ I3 applied — what the RED body should say instead

The current line is unanswerable as written:

```
Live ATR guarded by #620 — confirm [V2-ATR-BAR-GAP] fired for these names.
```

Three separate reasons it cannot be confirmed by a human reading the alert:

1. **It is unscoped in time.** Any firing today satisfies it, including one on 29-day-old
   warmup-replay bars — which is precisely what happened on 08-14.
2. **ABSENT is the EXPECTED answer for the most common cause.** A DB gap and an in-memory gap are
   **different universes**: when the symbol is off the watchlist there is a DB hole and *no*
   in-memory gap, so the guard correctly never fires. The instruction invites reading a correct
   silence as a failure.
3. **It asks for the wrong limb first.** The question that decides the action is *"were we exposed?"*
   — position or working order on the gapped symbol — which is a mechanical DB lookup. On 08-14 that
   limb answered NO, at which point the guard's state was irrelevant.

**Replacement — the watch computes both limbs and prints the conclusion:**

```
EXPOSURE:  LBGJ  position=none  working_orders=none   -> NOT EXPOSED
           (guard state is irrelevant when nothing was held or resting)
```

and only when exposure exists, a **scoped** guard check — the log line carries `prev_bar_ts` and
`cur_bar_ts`, so a firing can be bound to the detected gap instead of merely to the day:

```
EXPOSURE:  ABCD  position=200  working_orders=1       -> EXPOSED
GUARD:     [V2-ATR-BAR-GAP] ABCD with cur_bar_ts inside 11:16-11:36Z : PRESENT (1)
           -> live ATR refused to span the gap. No restart.
GUARD:     ... : ABSENT, and the symbol WAS watched throughout
           -> unguarded true range on a held position. THIS is the restart case.
```

⛔ **Note the dependency: the ABSENT branch is only meaningful given I1.** Distinguishing "absent
because there was no in-memory gap (symbol off-list)" from "absent because the guard failed"
requires knowing whether the symbol was watched. **I1 is a prerequisite for making this confirmation
mean anything** — without it, ABSENT stays ambiguous and the line should simply be deleted rather
than mechanised.

### ⭐ WHAT IS WORKING, AND MUST NOT BE BROKEN BY THE FIX

This is a **scoping defect in one predicate, not a broken watch.** Explicitly preserve:

1. **The repair path works end to end** — detect → REST-backfill → provenance-stamp
   (`source='rest'`) → re-verify → GREEN. Every stage did its job on 08-14. The provenance stamp in
   particular is doing real work: it is what lets parity studies exclude repaired bars.
2. **The RED's do-not-restart instruction is genuinely good.** *"A restart punches a fresh hole of
   its own, which is the very condition this watch exists to catch"* — an alarm that names how it
   could be misused is rare, and it was correct here. Keep it verbatim.
3. **The halt-downgrade logic** (`REST answered for every holed symbol and had none of the bars ⇒
   the market produced no prints`) already encodes the denominator idea correctly for one case. I1
   is that same reasoning applied to the watchlist axis.

⛔ **One correction to the alert body:** the undo it prints,
`DELETE FROM strategy_bar_history WHERE source='rest'`, is **unscoped** — it deletes every
rest-sourced bar ever written, not the 21 from this repair. It needs a symbol + time bound.

⛔ **And a provenance nuance worth recording:** here `source='rest'` means something stronger than
"a bar was missed" — it means **v2 was not watching the symbol at all.** Parity studies should
exclude those windows outright rather than merely prefer `source='live'`, or a backtest can take a
setup live v2 was structurally blind to.

---

## §1. ⛔ The originally-stated premise also does not survive the data

The design was additionally requested to catch "three silent stoppages this week." I measured them
before building anything. **Two of the three are not stoppages, and the one real incident is
invisible to the design as specified.**

### 08-07 "brackets stopped" — no. The denominator was zero.

Bracket ratio at symbol-day grain, `live:schwab_1m_v2` (symbols with a buy fill → symbols with an
`oco_exit`):

| day | 07-29 | 07-30 | 07-31 | 08-03 | 08-04 | 08-05 | 08-06 | **08-07** | 08-10 | 08-11 | 08-12 | 08-13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ratio | 100% | 88.9% | 80% | 100% | 100% | 100% | 100% | **no row** | 100% | 100% | 100% | 100% |

**08-07 has no row because there were zero Schwab buy fills that day** (15 resting entries placed,
0 filled — against a trailing fill rate of ~10%, `0.9^15 ≈ 21%`, unremarkable). Zero fills ⇒ zero
brackets required ⇒ **0 brackets was the correct output.**

### 08-11 "resting entries stopped" — no. The ratio was normal.

`live:schwab_1m_v2`, resting share of all buy orders:

| day | 07-29 | 07-30 | 07-31 | 08-03 | 08-04 | 08-05 | 08-06 | 08-07 | 08-10 | **08-11** | 08-12 | 08-13 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| resting % | 83.3 | 83.3 | 84.7 | 88.2 | 81.3 | 86.8 | 86.3 | 88.2 | 83.2 | **87.8** | 98.2 | 95.3 |

**87.8% sits mid-band.** What happened on 08-11 was the P0 boot-hold suppressing entries for 2h22m
(`project_mai_tai_v2_post_boot_promotion_uncapped_fleet_hold`).

### ⛔ And the decisive finding: all three detector families miss 08-11

| detector | 08-11 | comparison | verdict |
|---|---|---|---|
| **ratio** | 87.8% | band 81–98% | mid-band — **misses** |
| **daily volume** | 49 buys | trailing median ≈ 51 | dead on median — **misses** |
| **longest intraday gap** | 67 min | 08-07 **144**, 08-04 **105**, 08-06 **80** | *below* three normal days — **misses** |

**A pure-ratio report would not have caught the one incident it was commissioned to catch.**

### Why — and this is the same lesson as §0's I1

Hourly resting placements, `live:schwab_1m_v2`:

| ET hour | 07 | 08 | 09 | 10 | 11 | 12 | 13 | 14 | 15 |
|---|---|---|---|---|---|---|---|---|---|
| 08-10 | – | – | 3 | 12 | 15 | 25 | 3 | 25 | 11 |
| 08-11 | – | – | 2 | 13 | 1 | 10 | 7 | 9 | 1 |
| 08-12 | – | – | 7 | 20 | 51 | 66 | 50 | 62 | 74 |
| 08-13 | – | – | 12 | 15 | 29 | 53 | 13 | 27 | 34 |

**Hours 07 and 08 are structurally empty on every day** — extended hours arm a soft rest, they do
not place a broker order. The 08-11 loss was *in that window.*

> ### ⭐⭐ THE GENERAL RULE (I1 generalised)
> **A denominator must live UPSTREAM of the failure it is meant to detect.**
> A suppression removes numerator *and* denominator together, so the ratio stays perfect while
> output goes to zero. Counting entries cannot detect a suppressed entry, exactly as counting bars
> cannot detect an unwatched symbol.

---

## §2. What gets measured

Each row is one check. `N` is always sourced upstream of the failure mode.

| # | check | denominator N (upstream) | numerator M | baseline today | catches |
|---|---|---|---|---|---|
| 0 | **bar continuity** | **minutes the symbol was WATCHED** | minutes a bar arrived | *to be established* | 08-14-class false RED |
| 1 | **entry conversion** | flips/arms that passed the gates | entry orders placed | *to be established* | 08-11-class suppression |
| 2 | **resting share** | entry orders placed | placed as `STOP_LIMIT` | 81–98% | routing regression |
| 3 | **fill conversion** | resting orders placed | filled | ~7–15% | dead/mispriced rests |
| 4 | **bracket coverage** (RTH) | symbol-days with a buy fill | symbol-days with `oco_exit` | schwab **100%**, orb 75–100% | 08-13-class attach failure |
| 5 | **protection coverage** | positions held >60s | with broker-side protection | *to be established* | naked-position window |
| 6 | **exit level quality** | exits filled | filled at level vs below | *to be established* | slippage regression |
| 7 | **orphan rate** | resting orders placed | left non-terminal at EOD | ~0 | FRTT-class orphans |

**Checks 0 and 1 are the ones that matter and neither exists yet.** Check 0 replaces the current
bar-gap predicate per I1. Check 1 needs an arm/flip denominator the order tables cannot supply.

### ⛔ Structural zeros must be declared, not discovered

`live:orb` resting share is **0.0% on every one of 12 days** — the fan-out leg does not place
`STOP_LIMIT`. A naive "was non-zero, went to zero ⇒ page" rule pages on it daily. Every check
carries an explicit `applies_to` set; outside it, print `n/a (structural)` — never 0%.

### ⛔ Aggregation hazard on check 4

`live:orb` bracket coverage is 75–100% and **the variance is the EH/RTH mix**, not a defect —
pre-market is 0% bracketed by broker limitation (`0/34`, `0/13` over 14d). Blending sessions lets EH
fills dilute the ratio and **mask a real RTH failure**. Check 4 is RTH-only, with the EH count
reported beside it as a separately-labelled, explicitly-unbracketable population.
See `feedback_aggregation_masked_the_event`.

---

## §3. Step change, not threshold

```
today's ratio   vs   previous trading day
                vs   trailing 5-day median (weekends/holidays dropped, never zero-filled)
```

| condition | label |
|---|---|
| N = 0 | `QUIET` — denominator empty, nothing to conclude |
| within ±1 MAD of trailing median | `STEADY` |
| moved > 2 MAD | `MOVED` — printed with both numbers |
| non-zero for ≥3 of the last 5 days **and now 0 with N > 0** | `STOPPED` — **the only pageable state** |

MAD rather than standard deviation: these ratios are small-count and skewed, and one 100% day should
not widen the band. `STOPPED` requires **N > 0** — the clause that separates a defect from a quiet
day, and the clause 08-07 would have failed.

⛔ **Volume gets its own row, separate from ratio.** A ratio holding steady while N collapses is the
08-11 shape.

---

## §4. Report, not pager

**One scheduled run, one artefact.** Extends the F3 `CHECKS` registry pattern
(`(level, name, detail)` + a single `VERDICT:` line) rather than inventing a second convention.

- **When:** once daily, **after the close and after 20:00 ET.** #699 removed the rotation deadline —
  rotated log siblings are read, so a post-close run sees the complete day.
- **Where:** a dated file under `/var/log/project-mai-tai/reports/` plus stdout; the existing ntfy
  path is used **only** for `STOPPED`.
- **Volume:** ~8 checks × 2 accounts, one screen. Every line carries `M/N` — never a bare percentage,
  never a bare count.

> ⚠️ **The no-new-pager constraint is a first-class requirement.** There are already 8 crons on ntfy,
> and this morning demonstrated the cost of a noisy one: a RED and a GREEN fifteen minutes apart,
> neither carrying information. On the 12 days measured, `STOPPED` would have fired **zero times**.

⛔ **This report does not replace the existing bar-gap cron** — that cron's repair action is
load-bearing and must keep running. What changes is its *verdict predicate* (I1) and its *re-check
window* (I2).

---

## §5. Every check proves it can see — and cannot satisfy itself

Each check carries a **control**: a past date with a known non-zero population, asserted at run time.

```
✓ CONTROL bracket coverage — 2026-08-12: 7/7 schwab, 7/8 orb   (>= 5 expected)
✗ CONTROL entry conversion — 2026-08-12: 0 arms visible        ⇒ CHECK VOID
```

A failed control makes that check **VOID**, never `QUIET` and never `STEADY` — reusing #699's
`CONTROL_VOID`, not rebuilding it.

⛔ **Control days are chosen on population, not on narrative.** 2026-08-07 has zero filled rests and
zero brackets: picking it as a "before the change" control would pass **vacuously**. Candidate
control day is **2026-08-12** (330 rests, 31 fills, 32 brackets, 25 orb fills).

⛔ **Control days age out.** Log-backed controls die with retention (~6 days observed); DB-backed
controls persist. Where both exist the control is DB-backed, and the report states which it used.

⛔⭐ **I2 applies to the controls too.** A control must not be satisfiable by a repair this run
performed. Where a check both repairs and verifies, the verification range must strictly exceed the
repair range, and the report must print both ranges so the reader can see they differ.

### What this report cannot see — stated on every run

1. **The broker's book.** Webull OCO children are broker-created and never land in `broker_orders`.
   "A pair is resting at Webull right now" is unknowable here — **the operator's screen is the
   primary source and beats our records.**
2. **Per-lot attribution.** Everything is symbol-day grain; the per-lot gap blocks analysis, not just
   reporting.
3. **Manual activity.** The broker's book is shared; manual buys appear in these denominators.
4. **Intent that never reached an order.** Until check 1 exists, a suppression upstream of order
   creation is invisible — **the 08-11 hole stays open until check 1 is built.**
5. **Whether an ABSENT guard means "no gap to span" or "the guard failed."** Distinguishing them
   needs I1's watched-minutes source. Until that exists the guard-confirmation line is deleted
   rather than mechanised — an ambiguous check printed as a check is worse than no check.
6. **Whether a symbol *should* have been on the watchlist.** I1 excludes off-list minutes from N —
   which is correct for feed health, but it means **watchlist churn itself is not measured by check
   0.** If churn is the defect, it needs its own check with its own denominator.

---

## §6. Build order

1. **Check 0 (I1 + I2 + I3)** — smallest, highest-value, and it stops a live false-alarm source
   today. I3's alert-body change ships with it: the unanswerable guard line is either mechanised
   (needs I1) or deleted, and **deleted is the correct interim state** if I1 lands later.
2. **Check 1's denominator** — scope the arm/flip source. Without it the report cannot catch the
   incident that motivated the original ask.
3. Checks 2, 3, 4, 7 — pure SQL, baselines above already measured.
4. Step-change + `STOPPED` gate, backtested over ≥30 days before anything reaches ntfy.
5. Checks 5, 6 — need operator definitions (§7).
6. Schedule, after a week running silently to establish real fire-rates.

## §7. Open questions for the operator

1. **Check 0's watched-minutes source** — the `watchlist updated` log line is the obvious one, but it
   is a log (retention-bound) and only prints a `sample=`. Is there a durable record of watchlist
   membership per minute, or does one need adding?
2. **Check 1's denominator** — arm/flip, or "confirmed-watchlist symbol-minutes inside the window"?
   This decides whether 08-11 is catchable.
3. **Check 5** — what counts as "protected"? A Schwab native OCO and a Webull attached pair are
   different objects and the Webull one is unverifiable from our side (§5.1).
4. **Check 6** — which level: decided price, trigger, or NBBO at submit?
   `project_mai_tai_resting_does_not_avoid_the_spread` warns a fill-vs-quote number is
   uninterpretable without the tape.
5. **Is watchlist churn itself a defect?** LBGJ churned off, back, and off again within 31 minutes;
   DFSC 3× in 45 minutes. §5.5 notes check 0 deliberately cannot see this.
