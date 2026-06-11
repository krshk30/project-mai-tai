# Overnight Validation — Bar Parity + Entry-Criteria Audit — 2026-06-11

**Read-only.** Findings, not fixes. No production changes, restarts, or env edits were made.
Scripts: `analysis/overnight_bar_parity.py` (Phase A), `analysis/overnight_entry_audit.py`
(Phase B). Raw outputs: `analysis/reports/phaseA-2026-06-11.json`, `phaseB-2026-06-11.json`.

## TL;DR — both phases PASS

| | Result |
|---|---|
| **Phase A — bar assembly fidelity** | **24/24 symbols byte-exact** vs Schwab `pricehistory` (OHLCV + timestamp). 0 OHLC flags, 0 volume flags, 0 timestamp misalignment. |
| **Phase A — coverage** | 5 full-coverage; 19 with gaps fully explained by watchlist rotation. 2 (UBXG, BQ) flagged `HIGH-GAP-VERIFY` — isolated gaps that align with fade/re-confirm churn, not bad data. |
| **Phase B — false positives** | **35/35 fired signals REPRODUCED** from the bot's own logged inputs. 0 MISMATCH, 0 REVIEW. |
| **Phase B — false negatives** | 37 path-satisfying candidates, **all freshness-suppressed (stale warmup bars), 0 UNEXPLAINED**. |
| **Phase B — determinism cross-check** | Independent MACD recompute from Phase-A-validated vendor bars: **median \|Δ\| = 0.000000**, max 0.022 (deque-window reconstruction on gappy symbols). |

**Gate decision:** bar fidelity + signal logic are sound → **the replay study (expectancy / MFE /
MAE / haircut) is cleared to proceed** on tonight's bars + audited signals.

The **only standing finding** is the previously-noted GLXG-class `UniqueViolation` at the
REST/streamer persist seam — confirmed root-caused below; it is a benign dup, not a data error.

---

## PHASE A — Bar parity (stored bars vs Schwab pricehistory)

**Method.** For each symbol v2 stored bars on 2026-06-11 (RTH 13:30–20:00 UTC), fetched Schwab
`pricehistory` 1-min candles for the same day and compared per bar. Bucket convention confirmed
**from code, not assumed**: `ChartBar.timestamp_ms` = pricehistory candle `datetime` =
`_persist_bar`'s `bar_time` = **bar-start, epoch-ms UTC**. So alignment is exact by construction
for REST bars, and any off-by-one on streamer bars would be a real finding.

**Population note.** Today's store is **~99% streamer-built** (persist-lag median 2–3 s; the REST
warmup-seed bars are in-memory-only and not persisted). So Phase A is effectively a **bar-ASSEMBLY
check on the v2 CHART_EQUITY streamer** — exactly the spec's "most important possible finding".

### Assembly fidelity — PASS, 24/24

Every bar v2 stored matches vendor data **exactly** — open/high/low/close to the cent, volume to
the share, timestamp to the minute. Zero tolerance-level diffs, zero misalignment, zero extra bars.
There is **no flagged-bar drill-down because there are no flagged bars.**

### Coverage — two axes kept distinct

"Missing" bars (in pricehistory, not in store) were classified, **not papered over**:

- **not-watched** — minutes before the symbol's first / after its last stored bar (watchlist
  rotation). Not a gap.
- **de-subscription blocks** — long contiguous runs of in-window missing minutes = the symbol
  faded out (scanner 30% rule) then re-confirmed. Expected.
- **isolated real-print gaps** — short runs (≤3 min) bordered by stored bars where the vendor
  printed (vol>0). The only category that *could* indicate a streamer drop.

| Verdict | count | symbols |
|---|---|---|
| PASS (full coverage) | 5 | CCHH, PPCB, GLXG, QH, EDHL |
| PASS-with-notes | 19 | RKDA, WTO, GELS, MTEN, TMDE, ADIL, LASE, WBX, JZXN, PPBT, FGL, PMAX, RGNT, FRGT, VSA, DAIC, ASTI, **UBXG**, **BQ** |
| FAIL (corrupt bar values) | **0** | — |

**`HIGH-GAP-VERIFY`: UBXG (18 isolated gaps), BQ (22).** These were investigated, not left
ambiguous: **BQ's gap minutes fall exactly inside its documented fade/re-confirm churn** —
`[CONFIRMED] removed 1 faded candidate below 30.0%: BQ` at 14:29 UTC, re-confirmed 14:54, removed
again, re-confirmed 15:02, … BQ was subscribed/de-subscribed repeatedly; the "gaps" are
de-subscription boundaries shorter than the 3-min block threshold, **not streamer drops**, and
every bar BQ *did* store is exact. UBXG shows the same pattern (heavy de-sub blocks + boundary
gaps). **Limitation (honest):** bars alone can't perfectly separate "streamer dropped a subscribed
minute" from "brief de-subscription" sub-minute; a per-symbol SUBS/UNSUBS timeline cross-check would
close it definitively. Given assembly is exact and the churn is documented, this is **a verify item,
not a data-trust failure.**

### Operator's manual TOS spot-check (10 min) — store == vendor, all exact

Open TOS 1-min charts and confirm these specific bars (UTC; v2 store and Schwab vendor are
identical, so TOS should match exactly):

| Symbol | Bar (UTC) | O / H / L / C | Vol |
|---|---|---|---|
| GLXG (PASS) | 13:41 | 2.965 / 3.240 / 2.9601 / 3.195 | 814,503 |
| GLXG (PASS) | 15:00 | 2.985 / 3.010 / 2.980 / 2.9885 | 41,159 |
| PPCB (PASS) | 14:30 | 5.479 / 5.800 / 5.440 / 5.760 | 460,515 |
| QH (PASS) | 15:08 | 5.7215 / 5.7215 / 5.527 / 5.590 | 8,398 |
| UBXG (HIGH-GAP) | 19:05 | 4.630 / 4.710 / 4.620 / 4.710 | 11,019 |
| UBXG (HIGH-GAP) | 19:08 | 4.680 / 4.680 / 4.660 / 4.680 | 318 |

For UBXG/BQ also eyeball whether the **missing** minutes around these times were genuinely
no-trade vs the chart showing a candle TOS rendered that v2 lacks.

---

## PHASE B — Entry-criteria audit (Phase-A-passing symbols only)

All 9 signal-bearing symbols (GELS, GLXG, MTEN, PPCB, QH, RKDA, TMDE, UBXG, WTO) are
**assembly-exact** → none quarantined. The `[V2-MACD-PROBE]` log (11,802 lines, 06:58–20:24 UTC)
is the bot's own per-bar computation, logged immediately before the decision — used as ground truth
and cross-checked against an independent recompute from vendor bars.

### B-1 — False-positive / determinism: 35/35 REPRODUCED

For each fired signal, the full entry condition was re-derived from the bot's **own logged values**
(re-implementing the v1.32 gates read from `schwab_1m_v2.py`: `trend=close>ema9`,
`macd_strength=hist_pct≥0.02`, `green`, `rel_vol>1.5`, `vol_abs>5000`; stoch-overbought and
dead-zone are disabled-by-default → pass-through; `path_macd = cross_macd_above & macd_inc &
(close>vwap | vwap_cross) & gates`; `path_vwap = vwap_cross & macd_above_sig & macd_inc & gates`).

- **35 REPRODUCED, 0 MISMATCH, 0 REVIEW.** No signal fired against its own inputs.
- C2 pending-cross reconciliation included (consume conditions verified against
  `[V2-PENDING-CROSS-CONSUMED]`); none required it this day.
- **Independent vendor recompute** (MACD/EMA/VWAP from Phase-A-validated `pricehistory` bars, last
  300 ending at the signal bar, VWAP from the 04:00-ET anchor): **median \|Δ\| = 0.000000** across
  35 signals. Two small outliers — MTEN (\|Δ\|MACD 0.022, values near zero) and UBXG (\|Δ\|MACD
  0.003 but the bot had only `n_bars=146` vs the dense 300 → different EMA seed and VWAP base) — are
  **deque-reconstruction differences on gappy / late-warmup symbols, not computational errors.** The
  bot reproduced its *own* decisions exactly; the vendor recompute corroborates the math to ~0 in
  the common case.

### B-2 — False-negative / missed-signal sweep: 0 UNEXPLAINED

Swept **all** probe symbols (not just the 24 compared — so a miss on a low-bar-count symbol can't
be silently dropped), for bars where a path + all gates hold and state is flat (`pos_qty=0`,
`cooldown=0`) but no intent fired:

- **37 candidates, every one `freshness-suppressed`** (stale warmup-batch bars, `age_s` from
  ~44k to ~220k s — crosses detected during the cold-start replay, correctly not fired live; the
  freshness guard / C2 carryforward handles these by design).
- **0 UNEXPLAINED.** No real signal was dropped. (Note: today `pos_qty`/`cooldown` were 0 all day —
  no fills — so the state-machine gate never suppressed anything; this audit therefore exercises the
  freshness/warmup suppression path, not the cooldown path.)

### B-3 — Per-signal context sheet (35 signals)

Full sheet in `phaseB-2026-06-11.json` (`context`). Each row: symbol, time (ET), path, entry price,
MACD, and the next-30-min max gain / max draw from vendor bars (excursion only — **not P&L**; OMS
owns exits, and these signals were all OMS-rejected on the pre-P1 routing, so no real fills).

**5 marked for the operator's TOS "would I take this?" review** (a deliberate spread):

| # | Signal | Why it's representative |
|---|---|---|
| 1 | **PPCB 09:17 ET — MACD Cross** (px 3.15, fwd30 **+115%**) | Best forward excursion; clean MACD-cross winner. |
| 2 | **GLXG 09:41 ET — VWAP Breakout** (px 3.195, **+50%** / −3%) | Strong VWAP-path winner, shallow draw. |
| 3 | **QH 07:39 ET — VWAP Breakout** (px 5.83, macd **−0.054**, +37%) | Fires on the VWAP path with **MACD below zero** (path 2 only needs macd>signal + rising) — verify this is a chart you'd take. |
| 4 | **QH 09:26 ET — MACD Cross** (px 7.25, +6% / **−27.6%** draw) | A whipsaw: signal then deep adverse excursion — exit-logic stress case. |
| 5 | **UBXG 15:05 ET — MACD Cross** (px 4.71, +3.8% / −6.4%, `HIGH-GAP-VERIFY`) | The coverage-flagged symbol — confirm the chart around the signal looks right despite the de-sub churn. |

---

## Findings & recommendations (no overnight fixes — daylight gates apply)

1. **[confirmed, benign] GLXG-class `UniqueViolation` at the persist seam.** `_persist_bar` does a
   non-atomic SELECT-then-INSERT; when REST and the just-subscribed streamer deliver the same minute
   concurrently (both via `asyncio.to_thread`), both find no row and both INSERT → one raises
   `UniqueViolation` on `uq_strategy_bar_history_strategy_symbol_interval_time`. The bar survives via
   the other writer; it logs an ERROR but does **not** trip `loop_health`. **Recommend:** make the
   upsert atomic (`INSERT … ON CONFLICT … DO UPDATE`). Small follow-up, already on the small-items
   list — **not tonight.**
2. **[verify, not a fault] UBXG / BQ coverage gaps.** Align with fade/re-confirm churn (BQ proven).
   To make Phase A fully self-certifying on coverage, a future enhancement could cross-check the
   per-symbol streamer SUBS/UNSUBS timeline so isolated gaps are auto-attributed to de-subscription
   vs delivery. Optional.
3. **No entry-logic findings.** 35/35 reproduced, 0 false positives, 0 unexplained misses.

## What this gates

Phase A & B are clean → **the replay study (expectancy / MFE / MAE / slippage-haircut) is cleared**
to build on tonight's verified bars + audited signals. Recall the execution caveat from the P1
deploy: sim fills are idealized (no slippage/partials) and these signals were OMS-rejected — so the
context-sheet excursions are **opportunity**, not realized P&L. Realistic expectancy still needs the
real-fill path.

---

## Methodology, re-runnability, limitations

- **Re-run any day:** `analysis/overnight_bar_parity.py --day YYYY-MM-DD` then
  `analysis/overnight_entry_audit.py --day YYYY-MM-DD --probe-file <prefiltered> --phasea <json>`.
  Both need the service env sourced (DSN + Schwab token); the entry audit needs the day's
  `V2-MACD-PROBE` + `V2-PENDING-CROSS` log lines pre-filtered. Exact invocations are in each script's
  module docstring.
- **Vendor = `pricehistory`, not TOS directly.** TOS renders Schwab market data; the IDE can't open
  TOS, so the authenticated Schwab REST endpoint is the same-vendor proxy. The operator's manual TOS
  spot-check (above) closes the literal "against TOS" loop.
- **Honest limitations:** (a) coverage can't perfectly separate streamer-drop from brief
  de-subscription without the SUBS timeline; (b) the vendor MACD recompute uses a dense vendor window
  as a proxy for the bot's actual (possibly gappy) deque, so small \|Δ\| on gappy symbols is expected
  — B-1's primary determinism check is the re-derivation from the bot's *own* logged values, which is
  exact; (c) probe coverage drives B-2 — it was complete today (06:58–20:24, 11,802 lines, single
  un-rotated log).
