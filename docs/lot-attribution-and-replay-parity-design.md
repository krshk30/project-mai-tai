# Lot attribution + replay parity — DESIGN ONLY, awaiting operator review

**Written 2026-08-17 after two withdrawn geometry studies in one evening.** Operator-approved
direction: close the per-lot attribution gap, then bring the replay engine to live-v2 parity, then
re-run the exit-geometry question.

⛔ **No code in this PR.** The engine is CI-gated with golden fixtures precisely because throwaway
backtests changed strategy conclusions three times before; two more were added tonight. This is the
design that has to be agreed before any of it is touched.

---

## 0. Why this exists — the two withdrawn studies

**Attempt 1 — the replay engine, +1/−3 vs +2/−5.** Produced a clean table. Invalid: the engine caps
each symbol-day at ONE round trip, and on 08-17 it emitted 6 trades where we actually took 14
(IPST 1 vs 7, IVF 1 vs 6). The whole thesis for a tighter target is **more turns per day**, and the
capped population *cannot express that effect*. A smaller target could only ever give up upside.

**Attempt 2 — route 1, re-walk actual entries on the real tape.** Failed its own control:

```
paired trades 225 · |modelled − actual| median 0.54pp · within 0.5pp: 107/225 (48%)
FGI  actual +54.98%  modelled  +1.93%      BOXL actual +1.96%  modelled +46.38% (eod)
```

Two causes, and the second is the important one:
1. the walker never modelled the **flip** exit (`flip_pending=False`), so positions reality exited on
   an ATR flip ran to end-of-day;
2. ⛔ **the "ACTUAL" baseline was INFERRED, not captured** — each entry was paired to *the first sell
   fill for that symbol after entry_time*. On a day where IPST round-trips seven times that pairing
   is simply wrong. This is the FIFO failure the board already records
   ([[feedback_capture_attribution_never_infer]]: FIFO **invented** a −8.40% trade).

⭐ **Both aggregates looked plausible** (LIVE median +1.47% vs ACTUAL +1.52%) while individual trades
were off by >50pp. A broken model with offsetting errors produces a believable headline — read the
control first, always.

⇒ **Attribution is the prerequisite, not a nicety.** Without a captured entry↔exit link we cannot
build a trustworthy multi-trade-per-day ground truth, so we could remove the engine's cap and have
no way to validate the result. We would be repeating tonight's error one layer up.

---

## 1. PART A — capture the lot link (unblocks everything else)

**The defect** (open item, unchanged): a sell is not durably linked to the entry lot it closes.
Symptom 1: ~9 trades/day sit in `<day>.unpaired.jsonl` as `close_candidate_*`. Symptom 2 — the one
that raises priority: **every per-position study is stuck at symbol-day grain**, and a symbol-day can
mix a bracketed lot with an unbracketed one.

⛔⛔ **DO NOT INFER THE LINK.** Not FIFO, not nearest-time, not quantity matching. The rule is
explicit and it has already cost us once. **Capture at emit time, carry on the order, record on the
fill.**

### Proposed shape
| stage | carries | notes |
|---|---|---|
| entry intent emitted | mint `lot_id` (uuid) | one per intended position, minted where the intent is built |
| `broker_orders` (entry) | `payload.lot_id` | survives restart; already the durable row |
| `fills` (entry) | `payload.lot_id` | copied from the order |
| `oms_managed_positions` | `lot_id` column | the position IS the lot |
| exit intent / order | `payload.closes_lot_id` | set from the managed row being exited |
| `fills` (exit) | `payload.closes_lot_id` | the assertion the recorder needs |

**Open questions for review:**
1. **Column or payload?** `payload.lot_id` needs no migration but cannot be indexed cheaply. A real
   column on `oms_managed_positions` + `fills` is the honest home if we intend to query it per-lot.
   ⭐ Recommend: **column on `oms_managed_positions`, payload on orders/fills** — the managed row is
   the natural lot identity and is already the ownership discriminator (#704).
2. **Partial exits / scales.** A lot can be closed by more than one sell. `closes_lot_id` on each
   exit fill handles it; the recorder sums per lot. Confirm scales are in scope.
3. **The Webull fan-out leg** is a *separate* lot on a *separate* account driven by the same signal.
   Does it get its own `lot_id`, or a `parent_lot_id` pointing at the Schwab lot? ⭐ Recommend
   **its own `lot_id` plus `parent_lot_id`** — they exit independently and divergence is a known cost.
4. **Backfill.** Historical rows have no lot_id and cannot get one honestly. Accept: per-lot analysis
   starts from the deploy date forward. ⛔ Do not backfill by inference.

**Acceptance:** a mutant that drops `closes_lot_id` from the exit path must turn a test red; and a
test must prove a sell with no `closes_lot_id` is recorded as *unasserted*, never guessed into a pair.

---

## 2. PART B — replay parity with live v2

### B1. The cap is not one line
```python
590:  filled = False        # one entry per symbol
593:  exit_done = False
696:  for eff_ts, kind, payload in events:
697:      if exit_done: break        # the event loop STOPS after the first round trip
```
The replay does not merely decline a second trade — it **stops consuming the tape**.

### B2. ⭐ The real divergence: the strategy's close transition never fires
Live, when a position closes (`strategy_core/schwab_1m_v2.py`):
```python
state.cw_v2_emit_claimed   = False   # release the emit claim -> a SECOND entry in the SAME segment
state.cw_v2_bars_since_exit = 0      # reclaim gap counts from the exit
state.fanout_webull_claimed = False
state.fanout_claim_ms       = 0
# ⛔ LOAD-BEARING -- these are what actually enables reclaim.
```
plus `_poll_atr_guard(state, prev)` for re-arm.

**The replay never executes this**, so it cannot produce a reclaim — the exact mechanism behind
IPST×7 / IVF×6 on 08-17. Removing `if exit_done: break` alone would NOT fix it: the strategy would
still hold `cw_v2_emit_claimed=True` and refuse the second entry. **B2 is the fix; B1 is a
consequence.**

### B3. Work items
1. Drive the strategy's position-close transition from the replay when a modelled exit completes
   (mirror `position_qty -> 0`), so the live reset runs.
2. Then reset the replay's own per-position locals (`filled`, `exit_done`, `entry_rec`, `geometry`,
   `eh_armed`, `eh_flip_pending`) and **continue** the loop.
3. Let the EXISTING live bounds do the limiting — `cw_entries_this_flip < 2`, arm-on-flip, the 1-bar
   reclaim gap. ⛔ Do not invent a replay-side cap; that is how the two drift apart again.
4. Audit the remaining engine-vs-live deltas before trusting the output (see B4).

### B4. Known fidelity limits to re-check, not assume
- **Sparse Schwab feed** — the engine's own note: trustworthy for SHAPE/DIRECTION, not penny-exact.
  4 of 17 names on 08-17 were skipped `sparse_schwab_feed`.
- **Universe drift** — the replay traded CDTG/MYSZ/WFF on 08-17 which we never took on that account.
  Worth understanding before parity is claimed.
- **Latency band** — the memory says Schwab/v2 must get its OWN measured band; do not reuse Webull's.
- **Golden fixtures** — removing the cap changes trade counts, so the CI golden gate must be
  re-anchored deliberately, not "fixed until green".

---

## 3. Sequencing and acceptance

| # | step | gate to the next |
|---|---|---|
| A | capture the lot link | a day's trades pair with **zero inference**; unpaired stays unpaired |
| B | replay parity (B2 then B1) | replay trade COUNT per symbol-day matches live within a stated tolerance, validated against A |
| C | re-run the geometry sweep | only now can turnover be expressed, so only now is +1/−3 answerable |

⛔ **Do not run C before B, or B before A.** Tonight demonstrated both failure modes: C-before-B gave
a confident wrong answer, and a B-shaped shortcut without A failed its control.

⭐ **Every stage reports its control first.** Tonight's route-1 control is the model: it caught a
broken walker that would otherwise have produced a publishable-looking table.

## 4. What is NOT proposed
- No change to the strategy rules. The question is exit geometry; the entry stays as-is.
- No inferred backfill of historical lots.
- No widening of the entry trigger to reduce rejects (that is strategy, not execution).
