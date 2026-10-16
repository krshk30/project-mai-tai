# PULLBACK1 — the KKV A1 / A2 pullback scalp, read for project-mai-tai

**Source:** the operator's thinkorswim study "KKV A1 + A2 MOMENTUM SCALP — V4 DIAGNOSTIC" (saved verbatim as
`docs/strategy/pullback_scalping/tos/KKV_A1_A2_V4.ts`) and his LPCN 1-minute screenshot of 2026-10-07 07:12 ET.
**Written:** 2026-10-07 07:3x ET, claude-1. **Status:** STEP 1 = this document and a board task. No build, no rule change.
**Operator's words (07:2x ET):** "This is a perfect example of how the pullback can happen … I want to use this as a
scalping pullback kind of strategy, nothing momentum … we can use it when the stock shows up in our momentum scanner,
mostly, but throughout the day … any of our strategies with a pullback: then we don't have to enter at the time, we
don't have to exit at the time … we are going to create a simple strategy at some point; this is where you can help me."

---

## 1. What the script does, in plain words

It paints two kinds of BUY arrows on a 1-minute chart of a small-cap momentum stock.

### A1 — "20/20 continuation" (green arrow)
The stock is in a strong regime (the slow 60-bar stochastic is high), the fast 9-bar stochastic has just dipped
(a small rest) and is turning up again. It is a "the dip is over, momentum resumes" signal.

| Condition | Default | Meaning |
|---|---|---|
| Regime | S60 ≥ 80 | 60-bar stochastic (smoothed 10) says the stock is near the top of its 60-minute range |
| Strict regime (off) | S40 ≥ 60 and S14 ≥ 40 as well | optional |
| Recent fast reset | lowest S9 of the last 3 bars ≤ 25 | the 9-bar stochastic dipped hard recently |
| Fast turn | S9 > S9[1] and the rise ≥ 2 points | it is turning up now |
| Fire | first bar the three hold together | one arrow, not every bar |

### A2 — "impulse → pullback → reclaim" (cyan arrow, with a 0–10 quality score)
A move of at least 5% in the last 8 bars, then a pullback of 1.5–18% from that high, then a green reclaim bar that
closes above the previous bar's high with a decent body and a close in the top 40% of its range. That is the entry bar.

**Base setup (must hold):** impulse ≥ 5% over 8 bars AND the prior bar's close sits 1.5–18% below the impulse high.

**Trigger bar (must hold):** green; body ≥ 0.20% of open; close in the upper 40% of the bar; close > prior bar high.

**Quality score (10 points, diagnostic — none are hard filters by default):**

| # | Point | Default | Why it matters |
|---|---|---|---|
| 1 | Fresh high | high was ≤ 5 bars ago | the impulse is recent, not stale |
| 2 | Retrace fraction | 10–60% of the impulse range given back | a healthy pullback, not a collapse |
| 3 | EMA structure | EMA9 ≥ EMA20, EMA20 rising over 3 bars, prior close ≥ EMA20 − 2% | trend intact |
| 4 | Relative pullback volume | 2-bar pullback volume ≤ 0.80 × 10-bar average | sellers are quiet |
| 5 | Absolute pullback volume | ≥ 10,000 shares | still liquid (the operator's low-float floor) |
| 6 | Trigger volume returns | trigger volume ≥ 5,000 and ≥ 1.1 × pullback volume | buyers come back on the reclaim |
| 7 | Anti-chase | close within 3% of EMA9 | not buying an extended bar |
| 8 | Pullback duration | 2–3 bars (1 or >3 get no point) | the operator's preferred rhythm |
| 9 | Range compression | 2-bar pullback range ≤ 0.85 × 8-bar average range | a tight rest |
| 10 | Pivot reclaim | close > highest high of the last 2 bars | stronger than beating one bar |

Score ≥ 8 prints "A2++", ≥ 6 "A2+", else "A2". A 3-bar cooldown blocks a second A2 right after one fires, and an A2
is suppressed on a bar where A1 fires.

### What the screenshot shows (LPCN, pre-market 10-07)
From 2.05 the stock ran to 3.71. Arrows, read off the chart: A2+ 7/10 06:31, A2+ 6/10 06:41, A2 5/10 06:47, A2+ 7/10
06:52, A1 20/20 07:02, A2 5/10 07:08, A2 5/10 07:19. Each cyan arrow sits on the first green bar after a 2–3 bar red
rest on lighter volume; each was followed by a new high within a few bars. The 07:02 A1 came after the fast stochastic
dipped during the 06:55–07:00 rest and turned. The label row at 07:12 reads "A1 READY / A2 SETUP 4/10": regime is on,
a pullback is in progress, the reclaim bar has not printed. Every signal is decided at BAR CLOSE, so the first
executable moment is the next bar's open (≈ one minute after the arrow). The script does not repaint: it reads closed
values only (`close`, `high[1]`, `S9[1]`).

---

## 2. Why it fits what we already know

- Our entry today is the ATR BUY flip, executed by a resting stop-limit just above the short line. Our own studies say
  the entry is the weak link: "we buy moves already spent" (selection memo), the last-30-minutes-down rule (27% winners),
  the first-trade sequence matrix. The A2 is a different kind of entry: **it waits for the move, waits for a rest, and
  buys the first sign of resumption** — with the risk defined by the pullback low.
- It answers a gap we hit every day: a stock gets confirmed while the ATR line is already LONG (APUS, OLOX 14:14, MOBX
  14:21 on 10-06). Today the bot does nothing until a full SELL→BUY cycle. An A2 reclaim is exactly the entry for "line
  is long, we hold nothing".
- It is also the natural entry for the ALERT-SCALP idea (15 of 32 squeeze alerts reached +5% first): the alert is the
  impulse, the A2 is the timing.
- It can time ANY of our strategies' entries without changing their exits: the ATR sell flip, the +5% / −8% bracket,
  the confirmation exit all stay. The operator's point: "we don't have to enter at the time, we don't have to exit at
  the time".

## 3. How it would plug into the fleet (design sketch, nothing built)

```
scanner CONFIRMED → symbol on the watchlist
        │
        ▼
 1-minute bars (schwab_v2 stream, strategy_bar_history)
        │
        ▼
 PULLBACK1 detector (new, pure Python, per symbol, per closed bar)
   stochastics S9/S14/S40/S60, EMA9/EMA20, impulse/pullback/reclaim, score 0–10
        │
        ├── observer mode: log [PB-A1] / [PB-A2 score=k] + forward outcome (+5% / −8% / time) — NO ORDERS
        │
        └── entry mode (later, after a card): emit an OPEN intent to the OMS at the next bar open
             only when: ATR state is LONG, no position, no resting first-slot order in flight,
             score ≥ threshold, liquidity floor ok, inside 07:00–15:45 — exits unchanged
```

Where the code would live: `strategy_core/pullback_a2.py` (pure functions over `Bar` lists; same `Bar` type the
backtest oracle uses), a thin observer in the v2 bot that calls it on every closed bar, and one env switch
`MAI_TAI_STRATEGY_PULLBACK_A2_OBSERVER_ENABLED` (observer) — the entry switch comes with its own card later.

## 4. What is different between thinkorswim and our data (must be settled before any number is trusted)

| thinkorswim | project-mai-tai | Consequence |
|---|---|---|
| Candles from the TOS feed, extended hours included | Schwab stream CHART_EQUITY bars + REST warm-up; LEVELONE prints are not trades | Volume and bar bodies will differ slightly; the "bar-source" memo says only 54% of ATR flips agree between sources |
| Stochastic `slowing = 1`, D smoothing 3/3/4/10 | we implement the same formula | must be unit-tested against a TOS export of one session |
| Signal at bar close; execution next bar open | same (our ATR flip works the same way) | a 1-minute lag is built in, like today |
| Volume floors 10,000 / 5,000 shares | our ATR_FLIP_VOL_FLOOR is 10,000 | consistent |

## 5. The plan — steps, not timing

1. **STEP 1 (done): this document + the script saved + board task PULLBACK1.**
2. **STEP 2 — observer, dark:** codex builds the detector + observer; it logs every A1/A2 on watched names with the
   score and the forward outcome (did +5% come before −8%, bars to each, MFE/MAE). Zero trading impact. Runs in the v2
   bot or the momentum paper bot; the paper bot is the safer host.
3. **STEP 3 — replay on stored bars:** run the same detector over `strategy_bar_history` for the last 10–15 sessions of
   watched names (the data exists), so we get a denominator before any live claim: signals per day, by score bucket,
   +5% / −8% first, median move. Pre-registered, like RETRY1/TREND1. The parked backtest engine is NOT needed for this.
4. **STEP 4 — the card:** only after 2 and 3 agree, a 2–3 sentence rule in the operator's words (when the bot may take
   an A2 entry, which score, which stop), then build behind a switch, paper first.

## 6. Open questions I will not guess at
- Which host first: paper bot (no money, fastest) or v2 observer (same bars the live bot sees)? Default: paper bot.
- Stop placement for a live A2 entry: the pullback low, or our existing −8%? The script is entry-only.
- Interaction with "one trade per segment" (RETRYOFF1): an A2 entry after an ATR close in the same segment would be a
  second entry — the card must say whether the pullback entry counts as the one trade.

## 7. Board task
**PULLBACK1** — owner codex (build) / claude-1 (review + study design). Step 2 observer first; Step 3 replay in
parallel; no card, no live entry until both report. Added to the handoff as row T111.
