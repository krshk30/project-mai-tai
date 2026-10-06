# RETRYOFF1 corrected zero budget: PASS

As of 2026-10-06 14:49 ET, independently replayed on main
`c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`, with the import path verified
inside the isolated main worktree. Production was not changed.

Keep `MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_ENABLED=true` and set
`MAI_TAI_STRATEGY_SCHWAB_1M_V2_RETRY_ONE_MAX_RETRIES=0` in the reviewed
install. This implements the no-second-trade rule without a trading-code change.
The earlier flag-OFF FAIL remains valid for that different configuration.

## Recorded decision replays

| Recorded case | Budget | Owner after close | Primary drafts | Mirror drafts |
|---|---:|---|---:|---:|
| OLOX 12:20 close -> 12:21 draft, SELL cycle 1791302700000 | 0 | consumed | 0 | 0 |
| IPDN 12:16 close -> 12:17 draft, SELL cycle 1791301200000 | 0 | consumed | 0 | 0 |
| OLOX same evidence, positive control | 1 | idle | 1 | 1 |
| IPDN same evidence, positive control | 1 | idle | 1 | 1 |

**Correction to the requested IPDN 11:5x label:** it was not a second trade
in the previous SELL cycle. Own log read shows a fresh SELL at 11:41:02.394 ET
(bar 11:40, `1791301200000`), resetting the budget before the 11:44 first rest.
The prior 127-share row closed at 11:06; the next 131-share row did not fill
until 12:14. Its confirmation close at 12:16 is the actual same-cycle retry
counterexample. The 11:56 waiting reprice must not be suppressed as a closed trade.

Control using that recorded 11:40 bar and SELL event: a consumed prior cycle is
retired, count resets to zero, and the first rest emits one primary and one mirror.
The unfilled waiting owner remains resting with count zero at 11:56. No synthetic
price prints or broker results are substituted for these cases.

## Tests And Boundaries

`test_zero_retry_recorded_replay.py` contains six checks:
- `test_recorded_close_zero_retry_blocks_both_second_drafts` (OLOX and IPDN).
- `test_recorded_close_budget_one_positive_control_emits_both` (OLOX and IPDN).
- `test_recorded_ipdn_fresh_sell_resets_consumed_budget_and_first_drafts_both`.
- `test_recorded_ipdn_unfilled_wait_is_not_a_closed_trade`.

Six new checks plus main's 27 retry checks: **33 passed**. Changing the replay's
configured zero budget to one makes both no-second-draft checks fail by assertion
(2 failed, remaining 4 passed), including after raw inputs were bundled locally.
No implementation mutation or source edit was made.

This is an **offline decision replay**, not a full-session or live-cache replay.
The provisional owner, successful persistence, liquidity, quote and window gates
are reconstructed controlled inputs; row/client/segment identities, OHLCV, close
and SELL times and placement trails come from the recorded inputs. The real
position-book, retry ownership, SELL tracking and resting-draft methods run.
The waiting control checks retained ownership, not a broker replace wire call.
No full-suite claim is made for this docs-only evidence update.

## Receipts

Raw inputs and results are preserved in `zero-budget-receipts/`; original `/tmp`
receipts remain unchanged. IPDN SQL was bounded to ten managed rows / forty bars,
inside READ ONLY transactions with a five-second statement timeout. Log extraction
used low-priority reads of the current v2 log. No DB/Redis writes or broker calls.

Replay source SHA256:
`d25936f85a1ba5c6414a9f23dab663616e015d5cf8ba76f7c336dcce0affc102`.
Combined test log: `f1b5045ce8b761ce455a07695a73f230c809b3b1b8890fc7a5acf04f876222aa`.
Results JSON: `b574d462278c677d607d90e558ef373835d84eddd1c082fca2ce95472f9e40e4`.
IPDN rows: `74550c0736ec88175a13629fc29973f06cbd44a011020547fcac3d7f1cdec273`.
IPDN bars: `7bae6a133c0176a4d0f446402a24ae5b967210b876fdf3b9ba45764b99a54725`.

To reproduce, run the bundled replay file with pytest and `PYTHONPATH=src:.`
from an isolated checkout of the stated main SHA; include
`tests/unit/test_v2_retry_one.py` for the 33-check combined run. Set
`REPLAY_MAX_RETRIES=1` for the assertion-red setting control.
