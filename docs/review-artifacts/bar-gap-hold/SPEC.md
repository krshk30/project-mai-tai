# SPEC — GAPHOLD: no entry on a symbol whose bars are missing; hold, repair, resume (operator 2026-09-23)

**Written by `claude-1` 2026-09-23 ~14:55 ET. Operator's words: "when we don't have a bar then we have to hold on or block
symbols to be traded… fix the bar gap then start trade again." For `codex-2` to build; priority set by the operator.**

## The incident that names it — BENF 2026-09-23 12:53 ET, −8.1% in 10 s on both brokers

Root cause (both agents, on the box): **Schwab-side per-symbol data sparsity.** 12:00–12:50 ET BENF printed 3.8k–10k trades
per 10 min on the tape, yet our Schwab feed delivered **8 of 51 minutes** of CHART_EQUITY bars and 7 of 51 of LEVELONE
trades — the same minutes silent on both services, no reconnect, no callback error, subscription intact. On that sparse
series the 12:24 bar (a 20-minute hole compressed into one bar, 2.94 → 2.09) flipped the trail to 2.62 → 2.58; the
operator's chart, with every minute present, showed the line near 2.80. The rest sat at 2.58 for 7 min; a 5-s spike
touched it and reversed. **Schwab REST returned none of the missing candles**, so no repair from Schwab was possible.
Same class: `project_mai_tai_bar_source_defect` (54% flip agreement), NUWE 07-30 (restart hole → order 8% off the line).

What exists today: `[V2-ATR-BAR-GAP]` detects a gap > 90 s (35× on BENF, 166× fleet-wide that day) and stops True Range
from spanning it. **It does not stop trading on the hole.** The REST repair (`_fetch_recent_closed_bars` →
`_handle_bar_from_rest`) runs at promotion/restart and only accepts bars newer than its cursor; older bars are rejected by
the deque; `report_bar_gaps.py` repairs the database, not live memory. The gap watcher **misclassified BENF as a halt**
because "Schwab REST empty" was read as "no prints" — false here.

## The rule (v2 strategy, per symbol)

**HOLD.** When `[V2-ATR-BAR-GAP]` fires for a symbol during the entry window:
- cancel that symbol's resting order (Schwab + the Webull mirror) with `reason=bar_gap`, and refuse new entries on it
  (`[V2-GAP-HOLD] SYM gap_min= last_bar= held_since=`);
- an OPEN position is untouched — its exits keep running on whatever bars arrive; this rule is about ENTRIES.

**REPAIR.** Attempt a contiguous reconstruction of the hole from Schwab REST price history for that one symbol, inserted
into live memory in order (the deque must accept back-filled older bars for this purpose — a code change, tested), then
recompute the trail on the contiguous series. Log `[V2-GAP-REPAIR] SYM missing= fetched= contiguous=0|1`.

**RESUME.** Re-arm only when the last `gap_hold_min_contiguous_bars` (spec: 15) bars are contiguous AND the repair was
complete. If Schwab REST cannot supply the hole (BENF), **the symbol stays held for the session** — never resume on the
sparse series. Log `[V2-GAP-RESUME] SYM` / `[V2-GAP-HELD-FOR-SESSION] SYM reason=rest_empty`.

Settings: `strategy_schwab_1m_v2_gap_hold_enabled` (default False = byte-identical), `gap_hold_min_contiguous_bars` = 15.

## Open question for the operator (not a proposal — the standing rule is "no data-source change")
Our own captured tape (`market_capture_trades`, Massive) had every BENF print. Rebuilding the missing minutes from it
would make REPAIR possible where Schwab REST is empty. That is a bar-source question the operator has closed before; it is
recorded here only so the trade-off is visible: with Schwab-only repair, a BENF-shaped symbol is simply not traded that day.

## Tests codex-2 should write
1. Control: enabled=False ⇒ byte-identical (the 166 gap markers still print, nothing held).
2. A gap marker on a symbol with a resting order ⇒ cancel with `reason=bar_gap` on both legs, `[V2-GAP-HOLD]`, no new
   emit on the next flip while held.
3. REST returns the full hole ⇒ bars inserted in order, trail recomputed, `[V2-GAP-REPAIR] contiguous=1`; after 15
   contiguous live bars ⇒ `[V2-GAP-RESUME]` and the next flip may rest again.
4. REST returns 0 (the BENF shape) ⇒ `[V2-GAP-HELD-FOR-SESSION]`; a later flip that day does not rest; the next session
   clears the hold at the 04:00 roll.
5. An OPEN position on a held symbol still exits (target / stop / confirmation) — the hold never blocks a sell.
6. Mutation: resume without the contiguity check ⇒ test 4 RED; hold not cancelling the mirror ⇒ test 2 RED.

## Grade after deploy
Per session: `[V2-GAP-HOLD]` count, held-for-session count, and for each held name what the sparse-series line would have
done (the flip it would have taken and that trade's outcome from the tape). The denominator is stated on the line.
