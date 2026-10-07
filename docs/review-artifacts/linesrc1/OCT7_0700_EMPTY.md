# LINESRC1 October7 Amendment

[codex] Sole source writer on `codex/linesrc1-anchored-session-poll`, continuing
`a2a41012be2b9b007e0f0084f4a1bd295f79e376` / PR #1107 without rebase. AGREE on
the newly measured empty-response remedy. No production, broker, DB, token,
service, Redis, merge, review-pin, install or shared-handoff action.

## Evidence And Scope

The human reports October7 06:21 ET Schwab history anchored at 04:00 ET for
BIYA, MI, MTEN and SXTC: each `empty=true`, zero candles. This lane was not
given raw provider envelopes or price bars and made no production GET. The
fixture `tests/fixtures/linesrc1_oct7_empty_history_measurement.json` preserves
only those reported symbols/counts/flags/time, explicitly not raw-price data.
In-session response envelopes and clocks are controlled tests, not later live
measurements. No OHLC, volume, market pause or historical completeness is
invented for the four names.

The prior client rejects valid empty history as completeness failure and
opens polling at 06:55. The amendment uses the ET window 07:00 inclusive to
16:00 exclusive and requires the latest closed candle to be timestamped
07:00 or later. Thus 07:00:59 still does not fetch the 06:59 candle; the first
eligible closed-candle poll is at 07:01. The response request retains its
04:00 anchor. DST, cadence, concurrency, quotas and ordinary quotes/recent
bars are unchanged.

A valid zero-candle envelope with a boolean empty flag returns `[], None`,
never a fabricated completeness proof or source exception. An in-session
empty response sets the existing same-epoch wait, leaves coverage/revision
intact, does not dispatch a chart/strategy/persistence bar, and does not admit
entry. An old rebuild cannot clear the wait. Nonempty data marked empty,
unknown flags, wrong symbols, malformed/duplicate/foreign candles and
truncated responses remain real errors. Existing one-WARNING-per-semantic-
state suppression and repeated real-error invalidation remain intact.

## Verification

- Own unchanged a2 focused baseline: 149 passed in 6.17s.
- Final frozen focused candidate: 199 passed in 6.56s, zero failures/errors. This
  includes 102 LINESRC1, 12 ledger, 52 restoration integration, 18 R6 spanning,
  3 R6 assessment and 12 reviewer-factory controls. The increase is 47 new
  LINESRC1 cases plus three existing assessment cases added to the invocation.
- Named measured/control families:
  `test_oct7_measured_empty_names_do_not_poll_before_first_0700_close`,
  `test_oct7_measured_empty_shape_has_no_bars_or_proof_in_controlled_session`,
  `test_oct7_empty_session_waits_without_epoch_failure_or_warning_flood`,
  `test_empty_is_not_a_bypass_for_foreign_or_malformed_source`,
  `test_client_fences_context_before_first_0700_closed_candle`,
  `test_valid_empty_response_retains_prior_recorded_coverage_but_cannot_release_wait`.
- The first preliminary run had one NameError from misplaced old-epoch test
  assertions. Those assertions were restored to their original test; nothing
  was removed or weakened. Retained `/tmp/linesrc1-oct7-pre-final-focused.*`
  is not the final receipt.
- The existing twenty semantic mutations plus seven new 07:00/empty-response
  mutations run in isolated module/class processes, never editing checkout
  source. Raw per-phase controls and harness-error classification are retained
  under `/tmp/linesrc1-oct7-final-mutations/`; aggregate receipt is
  `/tmp/linesrc1-oct7-final-mutations.json`: 27 baseline controls pass, 27/27
  semantic assertion kills, zero harness or non-assertion errors.
  The first mutation sweep had 26 valid kills and one selector/harness error
  (two matches). That preliminary JSON/log is preserved separately; the
  selector was narrowed and all 27 controls rerun, without application edits.
- Unchanged 123-case acceptance runner: base/head JSON byte-identical, each
  SHA256 `b3104ee6525171e5ae427f4a8c806b2925e8586166244b68d2aa0851122a7f81`.
  First: 90 MATCH / 33 HELD; plus10: 98 MATCH / 25 n/a. No claim that held
  cases were admitted. Existing R6 real traded-hole and ten-clean-live-bar
  requirements remain protected by tests and semantic mutations.
- Targeted Ruff and whitespace checks pass. Reviewer runner/factory, ledger,
  settings and ops are byte-unchanged from a2. No flag/age/sizing/retry waiver.

Commands use shared venv Python, `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:.`,
inherited PATH unchanged and `-p no:cacheprovider`. Focused XML/log and the
new source/test hashes are recorded in `OCT7_RECEIPT.json`.

## Remaining Coverage

Raw envelopes/prices for the four names, actual in-session provider recovery,
historical fills/execution and deployed behavior are UNMEASURED. No new-head
local full-unit GREEN or complete hosted CI result is inherited from a2's
older receipts. Fresh hosted CI, parent-coordinated rebase if needed,
independent review and exact-head pin remain separate gates. Parent alone
owns morning checks, Redis maintenance and installation decisions.
