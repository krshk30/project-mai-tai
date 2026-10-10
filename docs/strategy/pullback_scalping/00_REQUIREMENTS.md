# PULLBACK SCALPING — requirements and study plan (DRAFT, living document)

**Owner:** the operator. **Scribe / reviewer:** claude-1. **Builder (when approved):** codex-2.
**Status:** DRAFT. Nothing built, nothing live. This PR is the single place for everything about this strategy:
the operator's words, the script, the reading, the requirements, the study design, the results as they come, the
decisions. Nothing about pullback scalping lives anywhere else.
**Started:** 2026-10-07 07:2x ET.

Files in this folder:
- `00_REQUIREMENTS.md` — this document (requirements, study plan, decisions log).
- `01_script_reading.md` — what the thinkorswim study computes, line by line, and what the LPCN screenshot shows.
- `tos/KKV_A1_A2_V4.ts` — the operator's thinkorswim script, verbatim.
- `02_study_results.md` — (to be written) replay and observer results, with denominators.

---

## 1. The operator's intent, in his words (10-07 07:2x–07:4x ET)

> "I would call this strategy **pullback scalping**. We have to take it slow because this is a new one.
> First we have to identify entry points. What you saw were entry points. This stock kind of just came to the market.
> We need to first identify **when** it works, **where** it works — does it work when the stock is confirmed from our
> scanner, or throughout the day? It's not about this signal from this strategy, it's an **entry**.
> I need to find out the **trading hours** first — maybe only the regular market, or 7 to 4, or maybe from the time the
> stock is confirmed to the next one hour. Then within trading hours, **how many entry points** we are getting. Then,
> with multiple points, **what is our scope** — A1 or A2, and in A2 the score 4, 5, 6, 7, 8 — which is our entry.
> Then the fourth, the tricky one: **this is a scalp**. We are not here like the ATR; we cannot put a −8% on it.
> Once we have the entry point we need to **measure for a week or 30 days** whether a fixed target, a trail, or
> something fixed is the right exit. That is how I approach it — correct me, add, modify. Document everything,
> store the script, make it a workable requirement document, and keep it in one draft PR so we can share it."

Earlier (07:2x): "we can use it in any of our strategies with a pullback — then we don't have to enter at the time,
we don't have to exit at the time."

## 2. What this strategy is and is not

- It is a **scalp**: small, quick, many; entries are the A1/A2 bar-close signals from the script; exits are short.
- It is **not** the ATR bot. No −8% hard stop, no +5% target by default, no sell-flip exit by default. Those are
  candidates to be measured, not assumptions.
- The signal is an **entry-timing layer**. The same detector can later time entries for other strategies without
  changing their exits; that is a separate decision, after this study.
- Scope of data: our minute bars exist only for stocks that were on the watchlist (scanner-confirmed). So every
  answer below is "within the scanner's universe". That is also where the operator expects it to work most.

## 3. The four questions, as the operator framed them — and how each is answered

### Q1 — Trading hours: WHEN and WHERE does it work?
Candidate windows, measured side by side on the same signals:
- pre-market 07:00–09:30; regular 09:30–16:00; the full 07:00–16:00;
- **time since confirmation**: 0–15 min, 15–60 min, 60+ min after the scanner confirmed the stock;
- first 30 minutes of the regular session (09:30–10:00) vs the rest.
Output: signals, hit rate and median move per window, with the count as the denominator for every percentage.

### Q2 — How many entry points do we get?
Per session and per stock, by window, by signal type (A1 / A2) and by A2 score bucket (≤4, 5, 6, 7, ≥8).
Also how clustered they are (the script's 3-bar cooldown) and how many fall on bars we could actually trade
(liquidity floor 10,000 shares on the pullback bars; price ≥ $1 per the operator's floor).

### Q3 — Which signal is OUR entry?
Decided by Q1/Q2 outcomes, not chosen up front: A1, A2, or both; the A2 score threshold that separates winners.
The script's ten quality points are measured individually too, so we learn which ones carry the edge on our tape.

### Q4 — The exit (the tricky one)
Pre-registered candidates, measured on the SAME signals chosen in Q3:
- fixed target: +2%, +3%, +5%;
- stop: the pullback low (the script's natural risk), a fixed −2% / −3%;
- time stop: 3, 5, 10 bars;
- trail: close below EMA9; close below the prior bar's low; the ATR sell flip (our existing line);
- combinations: target + pullback-low stop; time stop + trail.
Measured per signal: maximum favourable move (MFE) and maximum adverse move (MAE) at 1, 3, 5, 10 bars; which came
first, the target or the stop; bars to target. Then the operator picks the exit from the table, not from opinion.

## 4. My corrections and additions (reviewer)

1. **Run the four questions as one pre-registered study, not four sequential ones.** One replay pass over the
   stored minute bars answers Q1–Q4 together with one denominator; sequencing them would mean re-running on
   different data each time and comparing numbers that are not comparable. Pre-registration stopped a wrong
   conclusion once before (D20); same discipline here.
2. **Data parity first.** thinkorswim candles are not our candles (our bar-source study: only 54% of ATR flips
   agree between sources). Before any count is trusted, the Python detector must reproduce the arrows on one
   exported thinkorswim session (e.g. LPCN 10-07) bar for bar. That is step 0.
3. **Measure "where" by context, not just clock.** Add: price band (sub-$2, $2–5, $5+), how far the impulse had
   already run from the day's open (the operator's own finding that we buy spent moves), and whether the ATR line
   was long or short at the signal. These are free in the same pass.
4. **Define the entry price honestly.** The arrow is known at bar close; the fill is at the next bar's open (or a
   stop-limit at the trigger bar's high). Measure both; the difference is the slippage of this strategy.
5. **One trade per segment.** The operator's RETRYOFF1 rule is live. A pullback entry inside an ATR segment that
   already had a trade would be a second entry. The card must say whether pullback scalps count against that rule
   or run as their own book (separate strategy code, separate rows). Default proposal: separate book, paper first.
6. **Scalps need the Webull/Schwab fill reality.** Our resting-order study shows fills land at the trigger, not
   better; the Webull leg prices ~390 ms after Schwab; sub-$1 names need 100 shares on Webull. These bound the
   target: a +2% scalp cannot survive a 1% spread. Measure the spread at each signal bar.
7. **"A week or 30 days":** replay gives 30 sessions of history today (whatever `strategy_bar_history` holds for
   watched names); the live observer gives the forward week with zero trading impact. Both before any card.

## 5. Delivery plan (slow, as the operator said)

| Step | What | Owner | Trading impact |
|---|---|---|---|
| 0 | Python detector `strategy_core/pullback_a2.py` + parity test against one thinkorswim export | codex | none |
| 1 | Replay over `strategy_bar_history` (all watched names, as many sessions as stored) → `02_study_results.md` with Q1–Q4 tables | codex builds the runner, claude-1 reviews, both report | none |
| 2 | Dark observer in the paper bot: log every A1/A2 live with score, context and forward outcome | codex | none, no orders |
| 3 | The operator reads the tables and writes the card (entry, window, score, exit) | operator | none |
| 4 | Paper trades under the card for ≥5 sessions; then live, smallest size, its own book | codex / claude-1 | after approval only |

## 6. Decisions log
- 10-07 07:5x: **own book** — pullback scalping shares nothing with the ATR bot and is not tied to ATR segments; the one-trade-per-segment rule does not apply to it. **Measure before building anything**: today and yesterday first (readable on the chart), then a week, then more sessions, then build. Spread is measured, not assumed.
- 10-07 07:2x: strategy named **Pullback Scalping**; take it slow; step 1 = document + task (T111).
- 10-07 07:4x: the four questions above are the study; exit is measured, never assumed; one draft PR holds everything.

## 7. Open points for the operator (not guesses)
- Paper bot or v2 as the live observer host? (default: paper bot)
- Which thinkorswim session to export for the parity test? (default: LPCN 10-07 pre-market, the screenshot)
