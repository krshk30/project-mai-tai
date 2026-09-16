# v2 ATR Massive Seed — 30-Session Replay

Pre-registered window: `2026-08-05` through `2026-09-16` ET. The run is deliberately deferred
until after 16:00 ET on 2026-09-16; this file will be replaced by the measured report before the PR
is submitted for independent review.

Gates frozen before results:

- Massive/Schwab overlap parity on close/high/low within 0.1% must be at least 99.5%.
- Lost current tradeable BUY flips must be no more than 1.0% of current BUY flips, with every loss named.
- Every count must carry its denominator, including per-day parity and flip populations.
- Gained flips use entry at flip close and stop-first `+5% / -8% / next SELL close / 16:00` scoring.
- Gained outcomes report medians first and drop-one by symbol.

Command, run only after the close with the production environment loaded:

```bash
PYTHONPATH=src .venv/bin/python \
  docs/review-artifacts/v2-atr-massive-seed/replay_30.py \
  --start 2026-08-05 --end 2026-09-16 \
  --output docs/review-artifacts/v2-atr-massive-seed/replay-30.md
```
