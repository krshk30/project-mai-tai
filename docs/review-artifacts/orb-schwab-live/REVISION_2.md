# PR 1064: response to review at 84c3f234

Builder revision only. Base remains fc68b2389a2e94f87db6183189a61f9b1ace15b3.
No deployment, flag change, production query, or real broker order was performed.

## Design calls

R1 was a real mismatch with the operator-confirmed card. The paper fill-time prefix is removed
from the live exit path. The completed Schwab fill-minute body now decides at that bar's close;
44.9% exits, 45.0% holds. A native target or stop already filled wins. A missing break bar leaves
the bracket intact and records an incident (normal 0-3 second write delay is pending). ATR uses
the same Schwab OHLC and 5/3.5/Wilder math, but only a strictly later bar may cause an ATR exit.
Only the exact current parent is reconciled by the close coordinator, not an old trip's child.

A1 was a real calculation mismatch, not a feed outage: first-value EMA on a last-26 slice is
not V2Indicators.macd. The full available 07:00+ series now uses that exact method. This needs
at least 35 bars. The last 35 must be consecutive; older sparse bars retain observed-series
math. The sign-divergence test is synthetic and IMCC-shaped, not a fabricated real IMCC tape.

A2 was a real handle-loss risk: an accepted PUT can change the broker ID even when the GET
fails, has a partial execution, or has the wrong shape. The receipt now survives those cases.
OMS durably retains the new ID and a HOLD; polling reconciles actual fills at that ID, while
reprice/cancel cannot retry across a restart. A status-only read does not clear the HOLD.
Receipt acceptance is not bracket confirmation or a fill. If no ID is returned, manual broker
reconciliation is required. A process crash before receipt persistence is also not proof of
either cancellation or flatness; the attended test/runbook remains a prerequisite.

A3 was a real scope error: the v2 gate refused any broker holding/read error, including v2's
own adds. That added broker read is removed. v2 collides only with ORB-owned buys/positions;
the existing post-submit reconciliation remains unchanged. The flag-OFF test isolates the
entry path, stubbing that pre-existing post-submit reconciliation in BOTH head/base controls.
Its PostgreSQL advisory-lock spy proves the flag-OFF branch has no new lock.

## Guard tests and controls

The focused suites pass 655 tests. These in-memory mutations all cause their tests to fail:

| Mutation | Regression proof |
| --- | --- |
| Body allowed before close | Pre-close context cannot sell |
| Body decision time changed back to fill | Decision is the completed break-bar close |
| Body <45 changed to <=45 | Exact 45% holds |
| ATR allowed on break bar itself | Only later bars may trigger ATR |
| Advisory-lock flag removed | Flag OFF v2 emits no lock |
| Watchdog flag removed | Flag OFF performs no query or intent |
| Initial-open end widened to 09:29 | 09:28:15/30/59 refuse |
| Wrong event source accepted | No preview, order, or intent row |
| Cancel ignores filled_quantity | ACCEPTED with filled_quantity=1 refuses cancel |
| Lower replacement allowed | Lower trigger never reaches PUT |
| Replacement price-match forced true | Wrong price remains UNKNOWN |
| Replacement new-ID write removed | Accepted PUT + failed GET must retain NEW-PARENT |
| Replacement HOLD guard removed | Subsequent cancel/reprice across restart stays blocked |

RED controls against the prior reviewed head 84c3f234 restore the actual old code in memory:
R1 sells before the break close; A1 allows a negative full-series histogram; A2 loses the new
ID after accepted PUT/failed GET; A3 calls the added broker read and refuses a v2 add.
The flag-OFF test also passes with process_trade_intent restored from exact main fc68b238.
These are scoped behavioral controls, not a claim new ORB modules exist on main.

Reproduce from the repository root with `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python
docs/review-artifacts/orb-schwab-live/revision_2_controls.py` (requires pytest and project dependencies).
Local evidence: /tmp/orb-1064-r2-controls.log. Repository source
files were not modified by the mutation/control runs. The existing flags default OFF.

Full tests/unit: final head 56 failed / 4615 passed; freshly rerun exact base fc68b238
56 failed / 4459 passed. All 56 FAILED node names match; no new or missing names. This is local
baseline parity, not a clean full-suite pass or live trading evidence. Logs are
/tmp/orb-1064-r2-final-unit.log and /tmp/orb-1064-r2-base-unit.log. Focused 655/655 is in
/tmp/orb-1064-r2-targeted.log. Ruff and diff-whitespace checks pass. Linux CI is separate.

## Pager and activation limits

The PR adds three ORB sources to ops/health/unexercised_watch.py with a dedicated message:
orb_schwab_replace_unknown, orb_schwab_exit_evidence, orb_schwab_eod_close. Tests cover selection,
one-time dispatch and actionable parent/account/symbol context, without claiming protection.
Other sources' delivery/auto-close behavior is unchanged. No installed copy was changed.

Before activation, separately install /home/trader/unexercised_watch/watch.py from the reviewed
file and update BOTH cron sha guards, then prove phone receipt. Also still owed: attended
Schwab place/reprice/cancel, early-close support, terminal partial-fill handling, and continued
Schwab-bar coverage for held ORB symbols. A5 is OPEN, not quietly marked completed.
Neither this revision nor a future merge authorizes live activation.
