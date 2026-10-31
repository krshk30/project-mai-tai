# Session Handoff - October 31, 2026 (for Monday November 2)

Written 2026-10-31 ~11:00 ET by claude-1. Supersedes the 10-27 page (PR #1169). Production facts read on the box 10-31 10:49 ET.

## Production NOW

- Box checkout: `6ddb3e6b0dc8295ba35419cee817d86ba0f97fba` (installed Sat 10-31 10:48 ET, operator-ordered).
- Services (all active, NRestarts 0): oms 3759926, schwab-1m-v2 3759942, strategy 3759958, control 3759969, orb-schwab 3759978. market-data / reconciler / capture untouched.
- Env (/proc verified): `MAI_TAI_STRATEGY_SCHWAB_1M_V2_WBEXIT2_ENABLED=true`, `MAI_TAI_OMS_WEBULL_EXIT3_ENABLED=true`. Backups: `env-backup-pre-install8159-20261030.env`, `env-backup-pre-install6ddb-20261031.env`.
- Preopen pinned to 6ddb (`/home/trader/after-hours/2026-10-31/pkg-6ddb3e6b0`, expect-flag EXIT3=true); `daily.verify_runtime()` OK.
- Broker census complete on both accounts; flat; zero open managed rows; zero open WBEXIT2/WBEXIT3 owners.
- Schwab refresh token: re-authed 10-31 10:46 ET, expires **Sat 11-07 09:46 ET** (re-auth by Fri 11-06).

## What is in 6ddb (chain since 10-30 06:23 install of 30c63e0b)

| Commit | Change |
|---|---|
| 0deff5f7 | ATR sim (FADE-gap cancel, causal print guard, H2 inputs, PM/RTH split) |
| 78bad800 | health: no page for boot hold with empty watchlist |
| 24d80deb | `[V2-LINE-RESTORE]` adds `first_false_predicate` + `branch` (logging) |
| 86a22355 | WBEXIT2 durable-child resolve (stale close obligation after a native OCO fill) + exit-fill exception class logging |
| c7ed59b4 | atrsim LIVE-rules sim honours the live 3-bar established-short gate |
| 61ee3d28 | WBPA2: country-routed Webull PRICE_AGGRESSIVE resubmit (8% band, serial, no-working-order proof) |
| 9ee03a5a | WBPA3: broker-rejected never-accepted open = closure; recovery uses dispatched accounts |
| 8159cd107 | `[WBEXIT2-GEOMETRY-REFUSED]` refusal-operand log |
| e58e2497 | WBEXIT2 geometry accepts Webull working vocabulary (adapter `_ACCEPTED_STATUSES`, aliases must agree) |
| 6ddb3e6b0 | passive `[OMS-V2-EOD-OCO-STEP]` log (no behaviour change) |

## Root cause 10-30 — status defect proven conditionally (Webull exits refused: TNMG, ONEG, CHOW)

WBEXIT2 (switched on in the 10-28 evening install) required the raw Webull `order_status` to equal the literal `WORKING`; Webull documents `SUBMITTED` for an open order and never `WORKING`. Evidence: CHOW's real bracket read after the close (already CANCELLED by DAY expiry) replayed through the installed `original_geometry` with ONLY the status (and event=accepted) varied — SUBMITTED/PENDING/ACCEPTED/QUEUED refused, WORKING passed; the remaining real operands passed. This proves the status defect conditionally; the original live WORKING-state payloads for TNMG/ONEG/CHOW were not retained, so other predicates on those exact replies are not excluded. WB2/WB3 fixtures used the invented `WORKING`. Last good old-path Webull exit: 10-28 13:31 ELPW. Fixed by e58e2497. Interim 10-30 18:42 install ran with both switches OFF; switched back ON with the fix 10-31.

## Open (watch, no build)

- 16:00 EOD-OCO-TRANSITION `unanswerable` (CHOW 10-30): cause UNKNOWN (standalone confirm read said released; refusal inside OMS `_release_v2_eod_oco`). The passive step log in 6ddb narrows which branch refused on the next real case; it may not by itself name the cause. Bring the cause to the operator before any fix.
- First live Webull v2 exit on Monday: expect `protection=wbexit2_owner` with a sold/closed outcome. Any `[WBEXIT2-GEOMETRY-REFUSED]` line: keep the raw operands and classify it (it can also be an unchanged guard refusing correctly).

## Decided / closed

- 3-bar established-short rule stays (24-day measure: 5 of ~247 sim trades affected, net cost ~$29–42; too small).
- 10-30 trading closed: TNMG +15.18, ONEG +15.48 (both native targets), JDZG +4.94, WFF −9.98; CHOW 64 closed by the operator by hand.
- Codex: all workers HOLD, no open lane.
