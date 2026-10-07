# LINESRC1 Own October7 Provider Read

> SUPERSEDED IMPLEMENTATION SCOPE: the measurement below remains actual evidence,
> but polling-source tests/rebase metrics are historical only. The new event card
> assessment is in `OCT7_EVENT_CARD_STEP0.md`; replacement acceptance is pending.

[codex] Runtime source frozen at `35380b9b76745896b90afd43eca7c0f09011bd12`,
continuing PR #1107 at the initial read checkpoint without rebase or deploy.
The subsequent own-read change is only
tests/fixtures/evidence. Independently read client token/auth code: existing
REST client `_read_access_token` reads the store and `_authorized_get` uses
GET without refresh. The own memory-only helper reads the configured token
store, performs pricehistory GET only, and forbids redirect forwarding.
No credentials/authorization headers are printed or retained.

## Bounded Measurement

The first unprivileged setup at 06:38:51 ET could not read the root-only
service environment file. Official FAIL / PermissionError / zero requests
is retained in `OCT7_READONLY_SETUP_FAIL.json`; it is not reclassified or
silently removed. A separate authorized read-only privileged invocation ran
four serialized requests, one per name, zero HTTP retries, fifteen-second
timeout each and one-second spacing. No remote file, token, configuration,
order, service, DB, Redis or running strategy write. No token refresh.

Exact fixed request anchor: `2026-10-07T04:00:00-04:00` /
`1791360000000`. Latest closed minute: 06:38, `1791369480000`; inclusive
endDate: `1791369539999` (06:38:59.999 ET). Requests use frequency=1 minute,
periodType=day and needExtendedHoursData=true, matching the inspected source.

| Name | Request ET | HTTP | Empty | Candles |
| --- | --- | --- | --- | --- |
| BIYA | 06:39:07.647899 | 200 | true | 0 |
| MI | 06:39:08.882449 | 200 | true | 0 |
| MTEN | 06:39:10.087004 | 200 | true | 0 |
| SXTC | 06:39:11.295856 | 200 | true | 0 |

All four independently reproduce the earlier human-reported empty shape.
Official observation status is MEASURED, not deployed-source/morning-gate
PASS. No later 07:00 recovery or market-silence assertion is inferred.
Parent-owned reporter unclassified exceptions/official FAIL are not waived.

Own local raw metadata:
`/tmp/linesrc1-oct7-own-readonly-schwab-raw-metadata.json`;
SHA256 `b5c1a11553c6b229350ea1e97f9b81d46837bb65fc045f7503aecc49b7464661`.
Committed copy:
`tests/fixtures/linesrc1_oct7_empty_history_own_receipt.json`.
It retains all parsed market-data fields (symbol/empty/candles), request
bounds/time/status, original body byte lengths/SHA256 and no auth values.
It does not claim a raw wire-byte capture file. No additional response fields
were returned and no price bars were supplied or fabricated.

Own helper `/tmp/linesrc1-oct7-schwab-readonly-probe.py`, SHA256
`4d61f2ec997cdb9fa2c3f1bde407bda3f0c294f3cfa173ac3601966b2a9ccf47`,
ran over SSH stdin using the installed venv Python with bytecode writes
disabled. It was never installed/copied onto the box. Setup FAIL SHA256:
`e5d31d302ab10086a7a7a6f145a78a6129ddb5115203dae5765f06690120cc22`.

## Recorded Tests And Remaining Coverage

The four-name in-session parser/loop controls now replay each retained raw
envelope, with explicitly controlled 07:01 clocks. Counterfactual empty=false
remains labeled as such. Added
`test_own_oct7_live_receipt_is_bounded_readonly_and_matches_empty_shape`
checks each name's actual before07 bounded receipt. No raw-price inputs.
`test_exact_wallclock_first_0700_closed_minute_availability` directly checks
07:00:00 -> no request and 07:01:00 -> the closed 07:00 candle. Existing
07:00:59 and DST/16:00 boundaries remain. This derives from the pre-existing
latest-closed-minute calculation, not an arbitrary new wall-clock timer.
The interrupted final receipt was not published. Historical local 205-test
and 27-mutation runs remain at `/tmp/linesrc1-oct7-review-focused.{xml,log}`
and `/tmp/linesrc1-oct7-review-mutations.{json,log}`. Original 199/27
source-checkpoint receipts remain preserved. None certifies the new event card.

The unchanged 123-case acceptance output is byte-identical before/after the
runtime change. R6 true-hole/clean-live requirements and live error handling
remain protected. Full current-head unit pair, hosted CI and independent
review/pin remain separate coverage/release gates. Actual in-session recovery,
historical execution/fills and deployment remain UNMEASURED. No source
activation or parent morning-gate/Redis maintenance action by this lane.

## Authorized Rebase

Parent authorized exact origin/main
`5b8b4f642bbc3c312be436d0e92adbc22d9e9f95` before the final push. Own fetch
verified that SHA. The three-commit rebase completed without conflicts;
range-diff reports all three patches equivalent (`=`). No merge commit or
additional application/source delta. Rebased source checkpoint:
`5c2cd48ad8cb02ff60db50322be9f42d98d79c09`; original source change `35380b9b`
maps to `ab609549`. New own-read and exact-time test changes do not alter
runtime source. Historical focused tests/mutations and the 123-case acceptance
were rerun against the rebased polling source, before the superseding event card.
