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

### ✅ RESOLVED BY THE OPERATOR 2026-08-17
1. **REAL COLUMNS**, not payload. `lot_id` on `oms_managed_positions` and on `fills`;
   `closes_lot_id` on exit orders/fills. Indexable, queryable, migration required.
2. **Scales in scope, and they behave exactly as the live system does.** A lot may be closed by
   several sells; each carries `closes_lot_id` and the recorder sums per lot. No special-casing —
   whatever live does with a scale, the lot record reflects.
3. **The Webull fan-out leg gets its OWN `lot_id` plus `parent_lot_id`** pointing at the Schwab lot.
   They exit independently and the divergence is a known, measured cost — one id could not represent
   two different exits.
4. **Backfill ACCEPTED for backtesting**, with the boundary below.

### ⛔⭐⭐ THE BACKFILL BOUNDARY — where inference is and is not allowed
The operator's rationale is sound: a backtest is a model already, and historical analysis needs
history. But the *use* decides the standard, and tonight proved it:

| use | standard |
|---|---|
| **LIVE attribution** (the recorder, P&L, per-lot studies going forward) | **CAPTURED ONLY.** No inference, ever. Unchanged. |
| **HISTORICAL backfill** (validating the engine, pre-deploy analysis) | reconstructed — but **bounded, labelled, and quality-measured** |

⛔ **Why the boundary is not optional.** The backfill's intended job in Part B is to be the ground
truth the replay is validated against. If it is itself a guess, the control checks a model against a
guess — which is exactly how route 1 produced a plausible table with >50pp per-trade errors.

**A bounded reconstruction is far better than what failed tonight.** Route 1 paired each entry to
"the first sell on that symbol after entry_time" — no upper bound, no quantity check. We already have
a lot record: **`oms_managed_positions` IS the lot**, with `entry_time`, `original_quantity`,
`current_quantity` and a close time. Reconstruct within those bounds:
- candidate sells restricted to `[row.entry_time, row.updated_at]` on the SAME account+symbol,
- quantity accounted against `original_quantity` (a lot is closed when its quantity is satisfied),
- ⛔ joined on **our own order ids** first — the broker's book is shared.

**And publish the ambiguity rate.** Every backfilled lot is classified:
`UNAMBIGUOUS` (exactly one quantity-consistent sell set in the window) · `AMBIGUOUS` (more than one)
· `UNPAIRED` (none). ⭐ **The rate is the gate, not a footnote**: if ≥95% are UNAMBIGUOUS the
baseline is usable with the remainder EXCLUDED (never guessed); if a large share are AMBIGUOUS the
backfill is not a baseline and Part B waits for captured data.

⛔ Backfilled rows must be **marked as reconstructed** (`lot_source='backfill'` vs `'captured'`) so no
later study silently mixes the two — the same provenance discipline as `source='live'` on bars.

**Acceptance:** a mutant that drops `closes_lot_id` from the exit path must turn a test red; a test
must prove a sell with no `closes_lot_id` is recorded as *unasserted*, never guessed into a pair; and
a test must prove a backfilled lot can never be written with `lot_source='captured'`.

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

### B4. ⭐ THE MOMENTUM SCANNER IS PART OF THE MODEL (operator requirement, 2026-08-17)
A name is only tradeable **while the momentum scanner has it CONFIRMED**. The scanner confirms on a
squeeze and prunes on the fade rule (change% < 30), re-confirming and pruning repeatedly — CLRO
flickered 8× in one session. **Do NOT scan the whole session.**

This must be carried through BOTH halves of the work, not just the universe list:
- **Universe** — `v2_qualified_symbols` already derives from `scanner_confirmed_events`; keep it.
- **Windows** — entries count only if the break/decision timestamp falls inside a CONFIRMED window
  (`CONFIRM -> FADE | RETENTION_DROP`). Source: `strategy_core.momentum_confirmed` log events, and
  the extractor already exists at `backtest/scanner_windows.py`.
- ⛔ **This bites re-entry specifically.** The old note that the window restriction was a NO-OP was
  measured for the SLOW wait-3-candle entry, which self-selects into stable windows. It explicitly
  "WOULD bite an immediate-flip entry" — and once the cap is removed, **reclaims are exactly the
  short-interval re-entries most likely to land near a prune boundary.** A reclaim fired while the
  scanner had pruned the name is not a trade we would have taken.
- **Backfill** — the reconstruction must record which confirmed window each lot opened in, so a
  later study can tell a genuine reclaim from an out-of-window artefact.

⇒ Removing the one-round-trip cap without the window restriction would manufacture trades the live
system could not have taken. The two changes are **one piece of work**, not two.

### B5. Known fidelity limits to re-check, not assume
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
