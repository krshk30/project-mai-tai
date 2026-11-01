# Session Handoff - October 31, 2026 (for Monday November 2)

Written 2026-10-31 ~20:20 ET by claude-1 (final for the weekend). Supersedes the 10-27 page (PR #1169, closed). Production facts read on the box 10-31 20:15 ET.

## Production NOW

- Box checkout: `cad483fc1b27853ec4f196e468e18438336d2e2c`.
  - = 6ddb3e6b0 (10:48 ET install, WBEXIT2/3 status fix + EOD step log)
  - + 178a9aa5 (13:13 ET, BROKERDECIDES1 owner-phone pin fix)
  - + simulation-only commits (ATRSIM4, see below).
- Services (all active, NRestarts 0):

  | Service | PID | Started (UTC) |
  |---|---|---|
  | oms | 3759926 | 14:48:48 |
  | schwab-1m-v2 | 3772249 | 17:13:34 (v2-only restart for the phone fix) |
  | strategy | 3759958 | 14:48:54 |
  | control | 3759969 | 14:48:57 |
  | orb-schwab | 3759978 | 14:49:00 |

  market-data / reconciler / capture untouched.
- Env (/proc verified): `MAI_TAI_STRATEGY_SCHWAB_1M_V2_WBEXIT2_ENABLED=true`, `MAI_TAI_OMS_WEBULL_EXIT3_ENABLED=true`, `MAI_TAI_STRATEGY_SCHWAB_1M_V2_BROKERDECIDES1_ENABLED=true`.
- Preopen pinned to cad483fc (repin backups `*.claude1-pre*` in /home/trader/preopen-daily/repin-backups); `daily.verify_runtime()` OK.
- Broker census complete on both accounts; flat; zero open managed rows.
- Schwab refresh token: re-authed 10-31 10:46 ET, expires **Sat 11-07 09:46 ET** (re-auth by Fri 11-06).

## Main (GitHub)

- **main is GREEN** at `4cd00d6394dd` (#1174, merged 10-31 17:16 ET): first passing `validate` since ≥10-17.
  - Contents: installed code 178a9aa5 + 124 + 109 stale tests fixed + Ruff 830 → 0 (lint only).
  - Box ≠ main only by those behaviour-neutral Ruff source edits and the ATRSIM4 sim commits (PR #1176). The box takes main at the next after-close install.
- Open PRs:
  - #1175: squash-durability + 4 CI flake fixes (tests only).
  - #1176: ATRSIM4, simulation only.
  - #1172: docs-only fast path; validate passed; waiting on codex's pin.
  - this #1171.
- **CI flakes:** each 95-min run can randomly fail ~1–2 timing-sensitive unit tests. Seen: bbci_exit, wbnullid1, eodhandover1, pomexitlog1_v4 (fixed in #1175), pomexitlog1_v4_rate (codex lane open). Read which tests failed before calling a red run a regression.

## Today's real defect (fixed and installed)

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

- 06:20 preopen report (first on cad483fc).
- First live Webull v2 exit: expect `protection=wbexit2_owner` with a sold outcome. Any `[WBEXIT2-GEOMETRY-REFUSED]` → keep raw operands, classify.
- 16:00 `[OMS-V2-EOD-OCO-STEP]` lines if a v2 position is open at the close.
- WBPA2/WBPA3 first exercise on a country-routed Webull PRICE_AGGRESSIVE reject.
- ATR page: today's stock inputs appear on new rows (sim timer every 5 min).

## Process (operator 10-31)

- Three tiers:
  - docs/tools: same hour;
  - off-order-path: + real-data replay;
  - order path: + replay + tests + rate test.
- Docs-only PRs need no pin (live after #1172 merges).
- Codex talks only to claude-1 through `claude1-reviews/LATEST.md`, never to the operator.
- claude-1's file instructions carry operator authority for Tier 1/2 actions (pins, labels, fast-forward fixes).

## Root cause 10-30 — status defect proven conditionally (Webull exits refused: TNMG, ONEG, CHOW)

WBEXIT2 (switched on in the 10-28 evening install) required the raw Webull `order_status` to equal the literal `WORKING`; Webull documents `SUBMITTED` for an open order and never `WORKING`. Evidence: CHOW's real bracket read after the close (already CANCELLED by DAY expiry) replayed through the installed `original_geometry` with ONLY the status (and event=accepted) varied — SUBMITTED/PENDING/ACCEPTED/QUEUED refused, WORKING passed; the remaining real operands passed. This proves the status defect conditionally; the original live WORKING-state payloads for TNMG/ONEG/CHOW were not retained, so other predicates on those exact replies are not excluded. WB2/WB3 fixtures used the invented `WORKING`. Last good old-path Webull exit: 10-28 13:31 ELPW. Fixed by e58e2497. Interim 10-30 18:42 install ran with both switches OFF; switched back ON with the fix 10-31.

## Open (watch, no build)

- 16:00 EOD-OCO-TRANSITION `unanswerable` (CHOW 10-30): cause UNKNOWN (standalone confirm read said released; refusal inside OMS `_release_v2_eod_oco`). The passive step log in 6ddb narrows which branch refused on the next real case; it may not by itself name the cause. Bring the cause to the operator before any fix.
- First live Webull v2 exit on Monday: expect `protection=wbexit2_owner` with a sold/closed outcome. Any `[WBEXIT2-GEOMETRY-REFUSED]` line: keep the raw operands and classify it (it can also be an unchanged guard refusing correctly).

## Decided / closed

- 3-bar established-short rule stays (24-day measure: 5 of ~247 sim trades affected, net cost ~$29–42; too small).
- 10-30 trading closed: TNMG +15.18, ONEG +15.48 (both native targets), JDZG +4.94, WFF −9.98; CHOW 64 closed by the operator by hand.
- Codex open lanes at hand-off: #1172 pin publication; 5th CI flake (pomexitlog1_v4_rate). Results come back in claude1-reviews/LATEST.md.
