# Momentum live-rule 30-session baseline

This is a read-only study of the production Momentum 30 and Momentum 60 paper rules. It changes
no detector, threshold, fill, exit, sizing, feed, or service behavior.

## Frozen before capture

- Population: 30 completed US trading sessions ending **2026-09-16**. The 2026-09-17 outcome was
  already seen before this criterion was frozen (14/16 detections and 29/30 AEMD concentration),
  so it is excluded from calibration and reported only as a separate screen-control session.
- Candidate selection: every eligible prior-close symbol is checked with Massive minute aggregates
  from 04:00-09:30 ET. Any symbol whose premarket maximum high is at least 20% above its minimum low
  advances to the one-second exact screen. Only exact candidates receive raw `/v3/trades` downloads.
  The minute range may over-select because it ignores ordering, but cannot remove a true 30/60-second
  raw-print move.
- Replay: every captured row is normalized by production `normalize_raw_trade` and replayed through
  the production `MomentumPaperEngine`, including condition-12 neutrality, excluded prints, strict
  next-print fills, immediate fresh-move re-entry, +5% target, -15% stop, and 600-second path.
- Integrity: each strategy/session reports PATH rows against unique raw prints in the union of event
  ranges. Any mismatch aborts the report.
- Output: per session and bot, detections, fills, targets, stops, time exits, `NO_FILL`,
  `UNANSWERABLE`, PATH reconciliation, tape-key collisions, and per-symbol concentration.
- Health decision: a bot/session is `DETECTOR_SUSPECT` when detections are **greater than 10**.
  The report prints suspect sessions over all 30 sessions. If either bot is suspect on more than
  **3 of 30** sessions, the old `>10` band is rejected and the report recommends that bot's
  nearest-rank **P95** detection count (never lower than 10) as its replacement. The study does not
  alter runtime behavior; the operator decides whether to adopt the recommendation. These rules are
  fixed before any of the 30 calibration tapes are captured.
- Split control: both adjusted and unadjusted prior closes are fetched. A symbol-day whose `$1`
  eligibility differs is excluded and named, never silently graded. Every split with an ex-date in
  the population window is listed in the report.
- Screen control: before calibration capture, 2026-09-17 runs an unscreened one-second scan over
  every eligible symbol. The minute-screen set must contain every full-scan candidate. The rejected
  grouped-daily/RTH screen is evaluated beside it so its dropped symbols are measured.

## Operational boundary

Capture refuses to run before 16:00 ET on a trading day. Tapes are pulled on the trading box after
close, copied off-box, and replayed off-box. API credentials are never written to a tape or report.

## Screen control result (run after the rules above were committed)

The unscreened 2026-09-17 control checked `11,776` gradable symbols with both minute and one-second
aggregates: `23,555` API calls in `343.312` seconds. The full one-second scan found `7` candidates;
the new premarket minute screen retained all `7/7` within a 32-symbol superset, so the set difference
was empty. The rejected RTH grouped-daily screen retained all `7/7` on this specific day as a
106-symbol set, so its unsafe mechanism had no measured dropped instance in this control. Three
adjusted/unadjusted `$1` disagreements (`JAGX`, `MGN`, `NCT`) were excluded and named. The exact
machine-readable result is `screen-control-2026-09-17.json`.

```bash
python -m project_mai_tai.backtest.momentum_live_rule_baseline screen-control \
  --date 2026-09-17 --json /secure/momentum-baseline/screen-control-2026-09-17.json

python -m project_mai_tai.backtest.momentum_live_rule_baseline capture \
  --end-date 2026-09-16 --sessions 30 --output-dir /secure/momentum-baseline/tapes

python -m project_mai_tai.backtest.momentum_live_rule_baseline replay \
  --input-dir /secure/momentum-baseline/tapes \
  --json /secure/momentum-baseline/report.json \
  --markdown /secure/momentum-baseline/report.md
```
