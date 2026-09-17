# Momentum live-rule 30-session baseline

This is a read-only study of the production Momentum 30 and Momentum 60 paper rules. It changes
no detector, threshold, fill, exit, sizing, feed, or service behavior.

## Frozen before capture

- Population: the 30 most recent completed US trading sessions through the requested end date.
- Candidate selection: Massive one-second aggregates identify a safe superset of symbols with a
  possible 20% rise in 30 or 60 seconds. Only those symbols receive raw `/v3/trades` downloads.
- Replay: every captured row is normalized by production `normalize_raw_trade` and replayed through
  the production `MomentumPaperEngine`, including condition-12 neutrality, excluded prints, strict
  next-print fills, immediate fresh-move re-entry, +5% target, -15% stop, and 600-second path.
- Integrity: each strategy/session reports PATH rows against unique raw prints in the union of event
  ranges. Any mismatch aborts the report.
- Output: per session and bot, detections, fills, targets, stops, time exits, `NO_FILL`,
  `UNANSWERABLE`, PATH reconciliation, tape-key collisions, and per-symbol concentration.
- Health decision: a bot/session is `DETECTOR_SUSPECT` when detections are **greater than 10**.
  The report prints suspect sessions over all 30 sessions. This criterion is fixed before tapes are
  captured and will not be changed after results are visible.

## Operational boundary

Capture refuses to run before 16:00 ET on a trading day. Tapes are pulled on the trading box after
close, copied off-box, and replayed off-box. API credentials are never written to a tape or report.

```bash
python -m project_mai_tai.backtest.momentum_live_rule_baseline capture \
  --end-date 2026-09-17 --sessions 30 --output-dir /secure/momentum-baseline/tapes

python -m project_mai_tai.backtest.momentum_live_rule_baseline replay \
  --input-dir /secure/momentum-baseline/tapes \
  --json /secure/momentum-baseline/report.json \
  --markdown /secure/momentum-baseline/report.md
```
