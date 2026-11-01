# Session Handoff - November 1, 2026 (for Monday November 2)

Written 2026-11-01 ~13:00 ET by claude-1 (final for the weekend). Supersedes the 10-31 version of this page. Production facts read on the box 11-01.

## Operator rulings 11-01 (read first)

- **FREEZE from Mon 11-02:** fix only what actually breaks in the live market. New ideas and findings go on the board as one line each.
- **Chat before "bug" and before ANY fix:** every fix needs the operator's approval for that fix, unless he grants a window (e.g. an overnight session). Scans keep finding and proving causes freely. Codex starts no fix build without a claude-1 block that quotes his approval.
- **H2 dropped.** ATR page panel 5 is now the **Pullback check** (see below).
- **Daily close-out line:** "live took X of Y LIVE-rules-SIM trades; missed N", each miss with its proven cause.

## Production NOW

- Box checkout: `6b29bc0547722d6dc58f120cd483a658d0c6aadf` (main at #1181), installed 11-01 15:43 UTC (10:43 ET) on the operator's OK.
  - Adds the passive OMS loop-lag log `[OMS-LOOP-LAG]` (logs only when lag > 100 ms).
  - Plus behaviour-neutral lint vs cad483fc.
- Services (all active, NRestarts 0):

  | Service | PID | Started (UTC) |
  |---|---|---|
  | oms | 3873635 | 15:43:10 |
  | schwab-1m-v2 | 3873649 | 15:43:13 |
  | strategy | 3873669 | 15:43:16 |
  | control | 3873680 | 15:43:19 |
  | orb-schwab | 3873690 | 15:43:22 |

  market-data / reconciler / capture / tv-alerts untouched. If #1172 is installed after this page, only control restarts (see Main).
- Env: WBEXIT2_ENABLED=true, WEBULL_EXIT3_ENABLED=true, BROKERDECIDES1_ENABLED=true. BROKERDECIDES1-PUBLISH complete=True accounts=2; 0 errors since start.
- Evidence `/home/trader/after-hours/2026-11-01/pkg-6b29bc05`. Preopen re-pinned to 6b29bc05 (backups `*.claude1-pre6b29`); `daily.verify_runtime()` OK.
- Schwab refresh token expires **Sat 11-07 09:46 ET** (re-auth by Fri 11-06).

## Main (GitHub)

- Merged 11-01:
  - #1180: timing gates in a fresh process, report-only.
  - #1181: OMS loop-lag log (= box).
  - #1184 (b69877fa): five reviewed test PRs combined + the DST reporting-window fix at 6 off-order sites.
    - 3 order-path DST candidates PARKED: keepwatch_producer.py:48, session_line_restore.py:195, oms/service.py TTL 17569. Revisit as Tier 3 before 2027-03-14.
- #1172: docs-only fast path + its codex P2 fix + **Pullback check panel**. Codex-pinned at bf1de4dd9; merges when validate passes. Install = control restart only.
- Box vs main after #1172: main adds #1184 (tests + off-order DST windows: control_plane / trade_coach / v2 display / ORB tape) and the panel. Take both at the next after-close install, with the operator's OK.
- The report-only fresh-process timing step still fails on the shared runner (keepwatch ~55–72 ms > 50). It does not block merges; it is not live proof.

## Weekend findings (11-01)

- **Friday 10-30 live vs LIVE-rules SIM:** live took 3 of 7 and missed 4. Sim P&L of the misses ≈ +$5 net.
  - CHOW 12:01 / 13:15 / 14:35 = one cause, WBPA3. Webull refused CHOW waiting buys PRICE_AGGRESSIVE 11:29–11:45. The refused-never-accepted open was never closed, so CHOW was stuck in flight for the rest of the day.
    - Fix `entry_rejected_closure.py`, installed 10-30 night, in 6b29bc05.
  - ONEG 14:39: live was still holding the 13:18 ONEG. Its sell-flip exit was refused (WBEXIT status words). Fixed and installed 10-31.
  - Monday is the first exercise of both fixes.
- **Pullback backtest** (Downloads/PULLBACK_BACKTEST_claude1_1101.md), LIVE+2min, 241 trades:
  - No 2% pullback inside purple = 43 trades, 28% win, −$306.56. The rule "skip if no 2% pullback" gives +$306.56.
  - Leave-one-day-out +$197..+$332, every case positive.
  - The effect is 09:30 onward; pre-market is weak.
  - In-sample only: score it on new days from 11-02. Nothing live.
- **Pullback check panel** (ATR page panel 5) shows this per LIVE+2min buy every day. Replay on 22 days = 43 skipped, net +$306.56 exact.

## 10-31 real defect (fixed and installed)

- BROKERDECIDES1 owner-phone channel was refused at every v2 boot since the 10-30 18:42 install.
  - Cause: our 10-30 health edit 78bad800 changed `ops/health/armed_segments_check.py`, whose sha256 the channel pins.
  - 0 alerts were attempted, so nothing was missed.
  - Box replay: old pin REFUSED, new pin channel_ok. Fix 178a9aa5.
- Rule: any edit to that file must re-pin `CHANNEL_SOURCE_SHA`. The test catches it now that CI is green.

## ATR simulation / coach (ATRSIM4, simulation only, no order path)

- Every LIVE rules / LIVE+2min row records "stock" inputs as of the buy decision (operator round 4, "hot/cold STOCK at the buy"):
  - order_times;
  - previous_trade;
  - purple minutes and pullbacks at 2/3/5% on 1-minute closes, plus max swings;
  - 5/10-min time & sales buy/sell/between with UNKNOWN buckets and coverage;
  - below_day_high_pct + coverage.
- Provenance stamped on every day row. Session-end exits with a >15 min no-print gap = EXIT UNKNOWN.
- Codex caught two look-ahead defects (purple bars after the decision; eventual EXIT UNKNOWN in previous_trade). Both fixed before the final run.
- 22-day baseline re-run 10-31 (archive `/home/trader/after-hours/2026-10-31/atrsim4-archive/`). Changes: 3-bar gate rows + RENT/AHT EXIT UNKNOWN.

  | Table | New total | Trades | Pre-market | Regular hours |
  |---|---|---|---|---|
  | LIVE+2min | +$97.24 | 241 | +$204.80 | −$107.56 |
  | LIVE rules | −$227.38 | | | |
  | FIRSTBAR | −$491.49 | | | |
  | FIRSTBAR+2min | −$530.65 | | | |

- Coach files: Downloads/COACH_BRIEF_round4_stock_features_1031.md, COACH_BASELINE_ATRSIM4_1031.md, COACH_PROVENANCE_reply_1031.md. Coach next: winners vs losers per input, no thresholds yet.

## Monday watch

- 06:20 preopen report (first on 6b29bc05).
- `[OMS-LOOP-LAG]` lines: count and worst lag in the first hour (0 lines = no lag > 100 ms).
- Close-out: live vs LIVE-rules SIM count with each miss's cause; Pullback check panel numbers for the day.
- First live Webull v2 exit: expect `protection=wbexit2_owner` with a sold outcome. Any `[WBEXIT2-GEOMETRY-REFUSED]` → keep raw operands, classify.
- 16:00 `[OMS-V2-EOD-OCO-STEP]` lines if a v2 position is open at the close.
- WBPA2/WBPA3 first exercise on a country-routed Webull PRICE_AGGRESSIVE reject.
- ATR page: today's stock inputs appear on new rows (sim timer every 5 min).

## Process (operator 10-31)

- Three tiers:
  - docs/tools: same hour;
  - off-order-path: + real-data replay;
  - order path: + replay + tests + rate test.
- Docs-only PRs need no pin and skip the heavy validate steps (live once #1172 merges).
- Codex talks only to claude-1 through `claude1-reviews/LATEST.md`, never to the operator.
- claude-1's file instructions carry operator authority for Tier 1/2 mechanics (pins, labels, fast-forward review fixes). From 11-02, any FIX of a live finding still needs the operator's approval first (rulings 11-01).

## Root cause 10-30 — status defect proven conditionally (Webull exits refused: TNMG, ONEG, CHOW)

WBEXIT2 (switched on in the 10-28 evening install) required the raw Webull `order_status` to equal the literal `WORKING`; Webull documents `SUBMITTED` for an open order and never `WORKING`. Evidence: CHOW's real bracket read after the close (already CANCELLED by DAY expiry) replayed through the installed `original_geometry` with ONLY the status (and event=accepted) varied — SUBMITTED/PENDING/ACCEPTED/QUEUED refused, WORKING passed; the remaining real operands passed. This proves the status defect conditionally; the original live WORKING-state payloads for TNMG/ONEG/CHOW were not retained, so other predicates on those exact replies are not excluded. WB2/WB3 fixtures used the invented `WORKING`. Last good old-path Webull exit: 10-28 13:31 ELPW. Fixed by e58e2497. Interim 10-30 18:42 install ran with both switches OFF; switched back ON with the fix 10-31.

## Open (watch, no build)

- 16:00 EOD-OCO-TRANSITION `unanswerable` (CHOW 10-30): cause UNKNOWN (standalone confirm read said released; refusal inside OMS `_release_v2_eod_oco`). The passive step log in 6ddb narrows which branch refused on the next real case; it may not by itself name the cause. Bring the cause to the operator before any fix.
- First live Webull v2 exit on Monday: expect `protection=wbexit2_owner` with a sold/closed outcome. Any `[WBEXIT2-GEOMETRY-REFUSED]` line: keep the raw operands and classify it (it can also be an unchanged guard refusing correctly).

## Decided / closed

- 3-bar established-short rule stays (24-day measure: 5 of ~247 sim trades affected, net cost ~$29–42; too small).
- 10-30 trading closed: TNMG +15.18, ONEG +15.48 (both native targets), JDZG +4.94, WFF −9.98; CHOW 64 closed by the operator by hand.
- Codex open lanes at hand-off: none (all reviewed work merged or pinned). Results come back in claude1-reviews/LATEST.md.
