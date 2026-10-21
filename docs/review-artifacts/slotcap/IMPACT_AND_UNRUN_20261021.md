# SLOTCAP-SEED-RESTORE-20261021 source handoff

Status: SOURCE AUTHORING COMPLETE, ALL EXECUTION UNRUN. No static result is PASS.
Base: 556775899b1014db284afbd022cfe26fd4dec14a.
Branch: codex/slotcap-same-sell-20261021.
Worktree: /private/tmp/mai-tai-slotcap-same-sell-20261021.
Main exclusively owns shared queue/status/LATEST, integration and runtime execution.
No queue/status/LATEST edit, tests/imports/compile/AST/parsers/app/DB/provider/SSH/
SQL execution/orders/push/install/restart performed by this worker.

## Assessment and impact call graph

- _evaluate_completed_bar gap-not-resumed branch -> _observe_atr_sell -> existing
  account-neutral SELL queue/decision_id unchanged; new _remember_slotclear_gap_sell
  retains (original SELL bar, reconstruction watch, boot) only for genuinely live,
  fresh, completed, post-watch/post-boot SELL with matching short segment and cap.
- The original gap early return bypasses both _cw_v2_track cap release and its
  _retry_one_start_segment_on_sell. Remembering proof now invokes that EXISTING
  durable initializer while the ORIGINAL SELL is live. Its monotonic segment,
  watch, readable-budget and persist-before-memory guards are unchanged; duplicate
  observation/segment cannot reset spent budget. Resume never initializes budget.
- _maybe_resume_gap_hold -> _retry_slotclear_deferred_sell after contiguous-bar gate
  opens. If line/owner readiness is still held, later live _cw_v2_track retries the
  same proof. No quote retry, manufactured signal, SELL observation or order dispatch.
- _slotclear_fresh_sell and deferred retry -> shared _release_slotclear_sell_cap.
  Existing line, entries-held, gap, removed-wait, readable/fresh owner, idle phase,
  opportunity/segment/flip/provisional/first-rest, fill/account/position identity,
  held/union quantity, primary/mirror resting, emit/Webull claim, filled outcome,
  and BOTH-consumed-slot gates remain conjunctive. No symbol-only/no-fill waiver.
- Deferred retry additionally requires current fresh live completed bar, unchanged
  watch/boot, original short segment, same session, pre-close, and no pending or
  unmanaged restart evidence. Original SELL timestamp may age; only its proven live
  observation is retained. Current bar freshness is never borrowed from that proof.
- _clear_cw_slot_claims clears proof and provenance once; last_fresh_sell records
  ORIGINAL SELL bar, never resume bar. This does not replay the rest of the SELL
  state machine, retire owners or reset claims/budgets on resume.
- BUY math (including state-only updates), BUY tracker, ATR/session reset and slot
  release invalidate proof. Changed boot/watch/segment/session invalidates on retry.
  New SymbolState has no proof; last_live_sell restoration cannot recreate it.
- Entry still runs through existing _cw_v2_resting_track -> strict first-entry,
  RETRYLEFT, removed-wait, current line/quote/window/liquidity/ownership and durable
  fanout/OMS gates. Clearing cap alone neither dispatches a historical entry nor
  changes any entry price, quantity, PnL, fill attribution, persistence schema or flag.

## Source and authored controls

Production: 1 file, +61/-4 lines versus 556.
Tests: 2 new files, +283/-0 lines; 12 functions, 56 manually counted parameter
cases (54 core + 2 adapter refusal cases), NOT collected/executed counts.
Tool: 1 new bounded adapter, +94 lines. Docs: assignment plus this impact note.

Core controls cover same-SELL once-only release; unchanged event identity/no dispatch;
21 owner/inflight blockers; 10 current-state gates; 6 invalidations; 8 bad initial
proofs; line-readiness retry; actual gap-held completed-bar branch; restart default;
4 spent/newer/unreadable/persist-failure budget cases; recorded SXTC bars through
current first-rest manager with controlled owner/quote/window operands and dual
broker once-only slot identity. These are not historical OLOX outcome evidence.

SHA256 of authored implementation inputs:

| Path | SHA256 |
| --- | --- |
| src/project_mai_tai/strategy_core/schwab_1m_v2.py | 7db2b4f8980a6988a22d3bc4ecac9977712c002e1e0d0fd3a5ad43a6082c1d00 |
| tests/unit/test_slotcap_deferred_same_sell.py | 81954f8d810d0ad18aefaf55ac03e02a66afb50fbc19301ac2a3635748a79a5e |
| tests/unit/test_slotcap_olox_adapter.py | c039440cf12806d09123713fe177e7e053b2e76db4762ea111e66d94648d68e1 |
| tools/slotcap_olox_same_sell_replay.py | 6f60ded4f24b2fd87be87a4aef75ea4c29cb3682fc3de90134d25fe870dc327f |

## Original OLOX adapter and limits

tools/slotcap_olox_same_sell_replay.py accepts ONLY named SANITIZED input SHA256
28811fac407e32236a5b10c13b2e3df17f0fdabfc1b84a770e3ea7f1539bd273.
No raw env log opened or copied; sanitized bytes are not bundled or duplicated.
Bound original identity: atr-sell:OLOX:1792602240000, observed 17:05:02.905Z;
resume current bar 1792602540000 at 17:10:02.693Z. Current suppression and original
SELL markers are checked against exact sanitized bytes before loading snapshots.

Main must separately review/provide load_bound_snapshot and set_clock with actual
historical state in the same strategy instance. Each snapshot requires independent
hash bindings for watch/boot, flags, bars, owner/inflight, line/gap and budgets.
No default empty owner or fabricated candle is accepted by this adapter. Loader
must raise on UNKNOWN and preserve process-local proof. Hash syntax alone does not
validate provenance/semantics: review loader and actual operand sources before use.
Adapter reports bounded cap state, not PASS/full-candle/native/OMS acceptance.
Historical original-case inputs/complete affected-class denominator remain UNKNOWN.

## Gates and next action

Main: exact head/source and independent impact review first; then sole serial
execution of new tests plus existing slotclear/fresh-sell/RETRYLEFT/removed-wait/
gap/line/fanout controls; zero NEW failures versus 556, actual OLOX replay with
independently bound missing state, mid-session restart and 04:00 roll, composed
ALL-ON/full, reviewer GO, guard repin/prestart and install all remain UNRUN here.
No comparison to 556 or original-case success can be inferred from this source packet.
Package freeze Oct22 02:00ET/install03:00ET authority belongs to main/operator.

Reported: blocker callout sent to user for missing complete OLOX historical operands;
source-complete callout accompanies exact committed head/bundle handoff. Main should
amend existing stable task ID with these results; this worker must not overwrite queue.
