# ATR first-loss follow-through study

Status: design frozen before the 2026-09-15 after-close run. This is a read-only study. It changes no scanner, strategy, broker, order, or exit behavior.

## Question

When the first genuine ATR trade for a momentum-scanner-confirmed stock loses at the -8% stop, do later genuine ATR opportunities in that stock also fail often enough to justify blocking the name for the rest of the day?

The report also answers two supporting questions:

1. Before the first stop, did price offer at least +3% or +4%?
2. After the stop, did price later recover to the original +5% target, and did a later ATR BUY opportunity itself reach +5%?

## Frozen population

- Range: 2026-08-17 through 2026-09-15, ET, inclusive. This is the last 30 calendar days as of the run date.
- Universe: distinct `(trade_date, symbol)` rows with at least one `scanner_confirmed_events.event_type='CONFIRM'`.
- Data source: stored live `schwab_1m_v2` one-minute bars, the same bar family used by the strategy.
- A signal is eligible only when its canonical ATR BUY bar begins and closes inside one scanner membership window, and the membership began before the BUY bar. This preserves the watch-start cap and excludes a symbol that faded before the flip became knowable.
- A false intrabar touch that never produces a canonical BUY close is absent by construction. It neither becomes the first trade nor consumes a later opportunity in this study.
- Canonical ATR is the shared oracle: Modified true range, Wilder 5, factor 3.5, SMA5 seed, 04:00 ET session slice.

## Frozen trade model

- Entry reference: the canonical BUY-flip bar close.
- Target: +5% from the entry reference.
- Stop: -8% from the entry reference.
- ATR SELL: if neither resting target nor stop has fired, a canonical SELL closes at the SELL bar close, actionable one minute after the bar starts.
- Session endpoint: if no earlier exit occurs and a 15:59 ET bar exists, mark at that bar's close. If the closing bar is absent, the outcome is `UNKNOWN_INCOMPLETE_SESSION`.
- A target or stop touched inside the ATR SELL bar wins because the broker order rests before the software bar-close exit.
- If one one-minute bar touches both target and stop, the outcome is `UNKNOWN_INTRABAR_ORDER`. The study never chooses an ordering that the stored bars cannot prove.
- If the stored bar series has a missing minute while a modeled trade is open, the outcome is `UNKNOWN_BAR_GAP` before the next visible price is evaluated.
- Percent-return totals are equal-weight percentage points. They are not dollars or a portfolio simulation.

The full-month model uses the BUY close because exact quote/fill retention does not provide one common execution tape for every August and September row. Live resting fills occur near the pre-flip trail and may differ because of spread and the 0.5% stop-limit band. Any exact-fill subset must be reported separately and must not be pooled into the full-month denominator.

## First-stop measurements

For each symbol-day, the first eligible BUY is the first trade. An unanswerable first trade remains first; the study never skips it to promote a later gradable trade.

A secondary descriptive table also reports the first decisive `TARGET_5` or `STOP_8` outcome. It may skip a known ATR-sell outcome, but it never skips an UNKNOWN. This matches the operator's "+5 or -8" wording without changing the pre-registered primary definition or its rule criterion after seeing results.

For a definite first `STOP_8`:

- Guaranteed pre-stop MFE uses completed bars before the stop bar.
- Possible pre-stop MFE includes the stop bar's high.
- The distinction is required because OHLC cannot prove whether that high occurred before or after the stop low.
- Post-stop MFE starts with the following bar, so a print before the stop cannot masquerade as recovery. Failure to recover is claimed only with continuous bars through 15:59 ET; otherwise it is UNKNOWN. A visible recovery remains positive evidence even if another part of the path is missing.
- Every later eligible BUY is reported individually. A later BUY necessarily follows a canonical SELL and therefore represents a genuinely new short segment.

## Frozen rule criterion

The candidate "block the name after its first -8% stop" passes only if all four conditions hold:

1. At least 30 later opportunities have gradable returns.
2. At least 20 first-stop symbol-days contain a later eligible BUY.
3. The equal-weight sum of those later returns is negative.
4. The sum remains negative after dropping every one symbol and every one session day.

Failing this criterion means the month does not justify the rule. Passing it is evidence to discuss, not permission to change live behavior; execution-fidelity and out-of-sample validation still come first.

## Required report

Every count prints its denominator. The report must include:

- confirmed and bar-measurable symbol-days;
- symbol-days with and without an eligible genuine BUY;
- first outcomes: target, stop, ATR SELL, session close, and each UNKNOWN class;
- guaranteed and possible +3%/+4% pre-stop MFE counts;
- recovery to the original +5% after the stop;
- later opportunities and outcomes grouped by the first outcome;
- the pre-registered rule verdict and drop-one robustness;
- one detail row for every first-stop symbol-day, including all times in ET and its stored-series gap count.
- a separately labelled first-decisive-stop table, including earlier known outcomes, so MYSZ-shaped sequences are visible without being pooled into the frozen primary test.

The canonical oracle reproduces the stored Schwab sequence and does not reject a BUY merely because an earlier bar is absent. After entry, however, the first missing minute makes the trade outcome `UNKNOWN_BAR_GAP`. The production run is after 16:00 ET only and uses a read-only database transaction with a bounded statement timeout.

```bash
python -m project_mai_tai.backtest.atr_first_loss_followthrough \
  --range 2026-08-17 2026-09-15 \
  --json analysis/reports/atr-first-loss-followthrough-2026-08-17-to-2026-09-15.json \
  --markdown analysis/reports/atr-first-loss-followthrough-2026-08-17-to-2026-09-15.md
```
