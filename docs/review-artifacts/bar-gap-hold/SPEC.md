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

## The rule (v2 strategy, per symbol) — operator's correction 15:05 ET: never "held for the day"

> "We need 10 mins of good bars to resume. Be smart enough to find the missing bars as soon as it happens, block trading,
> and resume once we have good bars without a gap."

**DETECT, immediately.** Today the gap marker fires only when the NEXT bar finally arrives (≥ 90 s late) — during the hole
nothing fires. GAPHOLD detects on the clock: a subscribed symbol in the entry window whose last bar is older than
`gap_hold_detect_seconds` (spec: 90) **while the symbol is still printing** (a LEVELONE quote/trade or an OMS quote newer
than the last bar) is GAPPED now. A symbol that is genuinely quiet (no prints either) is not a gap. `[V2-GAP-DETECT] SYM
last_bar_age_s= last_print_age_s=`.

**HOLD.** On detect: cancel that symbol's resting order (Schwab + the Webull mirror, `reason=bar_gap`), refuse new entries
(`[V2-GAP-HOLD] SYM`). An OPEN position is untouched — its exits keep running; this rule is about ENTRIES only.

**RESUME on fresh good bars — no repair of the old hole required.** Count contiguous live bars since the hole (each within
90 s of the previous). When `gap_hold_resume_bars` (spec: **10**) contiguous bars have arrived:
- **re-seed the ATR / trail from those bars only** — the pre-hole ATR state is discarded, so the trail is never computed
  across the hole (the NUWE 07-30 / BENF 09-23 mechanism). Wilder(5) is fully re-seeded by 10 bars;
- re-arm; the next flip on the fresh series may rest again. `[V2-GAP-RESUME] SYM contiguous_bars=10 trail=`.
A new gap inside the 10 restarts the count. REST back-fill (`_fetch_recent_closed_bars`) is a nice-to-have that can
shorten the wait when Schwab has the candles; it is NOT required and its absence never blocks resume.

Settings: `strategy_schwab_1m_v2_gap_hold_enabled` (default False = byte-identical), `gap_hold_detect_seconds` = 90,
`gap_hold_resume_bars` = 10.

**What this would have done to BENF:** gapped from ~12:05 → held; bars 12:45, 12:46, 12:47 then 12:52 (a hole) → count
restarts; 12:52, 12:53 … → resume no earlier than ~13:02 with a trail seeded from 12:52 onward. No order at 12:53.

## Open question for the operator (not a proposal — the standing rule is "no data-source change")
Our own captured tape (`market_capture_trades`, Massive) had every BENF print. Rebuilding the missing minutes from it
would make REPAIR possible where Schwab REST is empty. That is a bar-source question the operator has closed before; it is
recorded here only so the trade-off is visible: with Schwab-only repair, a BENF-shaped symbol is simply not traded that day.

## Tests codex-2 should write
1. Control: enabled=False ⇒ byte-identical (the 166 gap markers still print, nothing held).
2. Clock detect: last bar 91 s old while a quote is 5 s old ⇒ `[V2-GAP-DETECT]` + `[V2-GAP-HOLD]`, resting order cancelled
   on both legs, no emit on the next flip while held. Control: last bar 91 s old AND last print 91 s old (quiet stock) ⇒ no hold.
3. Resume: 10 contiguous bars after the hole ⇒ ATR/trail re-seeded from those 10 only (assert the trail equals a fresh
   5-period Wilder on those bars, NOT the value carried across the hole) ⇒ `[V2-GAP-RESUME]`; the next flip may rest.
4. A second gap at bar 7 of 10 ⇒ the count restarts; no resume at bar 10 of the original count.
5. An OPEN position on a held symbol still exits (target / stop / confirmation) — the hold never blocks a sell.
6. BENF 09-23 replay from the stored bars ⇒ no rest at 12:46, no fill at 12:53; resume ≥ 13:02.
7. Mutations: resume without re-seeding (trail carried across the hole) ⇒ test 3 RED; resume at 9 bars ⇒ test 4 RED;
   hold not cancelling the mirror ⇒ test 2 RED; detect ignoring "still printing" ⇒ control in test 2 RED.

## Grade after deploy
Per session: `[V2-GAP-HOLD]` count, held-for-session count, and for each held name what the sparse-series line would have
done (the flip it would have taken and that trade's outcome from the tape). The denominator is stated on the line.
