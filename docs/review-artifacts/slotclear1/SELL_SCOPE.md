# Fresh SELL source follow-up scope

Historical pre-rebase freeze below. Current-main follow-up and ticket boundaries
supersede its baseline/supplemental counts: see CURRENT_MAIN_FOLLOWUP.md and
RECORDED_INPUT_BOUNDARIES.md. Hypothetical empty-ticket replay drafts never prove
actual both-leg placement. Present restored SXTC tickets veto both legs even
handoff OFF; no forced ownership release is permitted.

Sole source writer: `codex/slotclear1-fresh-flip-side`, original head
`d68d25e0bf8cca33d6c22da39fa550b5977b5da3`. No rebase, integration or production write.
PR #1113 reported base `1a70da19176d2cad486e8518e1c9030f1cabb968` at claim;
remote main was `994f08aee2c35b3809b3c3384e0d8628807dcfb7`. The paired unit
baseline is the exact original SLOT head, not a moving main or a joint NFQ tree.

## Source change

Independent `strategy_schwab_1m_v2_slotclear_fresh_sell_enabled`, default OFF.
Reconstruction records a separate watch boundary even when the old cap-only
marker cannot attest idle ownership. This boundary alone never grants entry.
At a completed, observed-live SELL newer than watch/restart, the strict path
requires readable fresh empty account-neutral ownership, no held/union quantity,
owner identity, working order, emit/fill claim, removed/boot/gap/line hold.
It then releases reconstruction claims; normal first-rest timing still requires
three short bars. No reactive BUY permission is created by SELL. BUY stays on
its existing independent narrow switch and 0.5% cap. Historical/duplicate SELL
delivery cannot retire the newly placed owner. Actual entry clears provenance.
RETRY-OFF still permits fresh SELL release; RETRY-ON reset remains persisted.

## Evidence and boundaries

Own bounded read-only pull: SXTC logs 13:45 through 14:44 ET, DKI 14:21 through
14:32 ET, October 7. Raw file `SELL_EVIDENCE_2026-10-07.json`: 933 SXTC log
records / 60 DB candles; 368 DKI log records / 12 DB candles. PostgreSQL read-only
transaction, 3-second statement timeout, 121-row fail-closed ceiling per symbol.
Log file `/var/log/project-mai-tai/schwab-1m-v2.log`; DB `strategy_bar_history`.

Observed SXTC: restored owner at 13:45:27, UNKNOWN during seed, old BUY arm capped
13:45:40, proven-empty owner retirement 13:45:46, second warmup cap 13:45:47.
The pre-SELL probes show idle owner, zero held/union quantity, no working rest,
both consumed bits and readable fresh ownership. Live SELL at 14:26:02.535;
16 consumed-slot suppressions from 14:29:01.119 to 14:44:02.259. The old cap-only
marker is NOT logged: its exact loss point is not claimed as directly observed.

The tests use real OHLCV rows joined to actual ATR probe timestamps/trails/ages,
not invented prices/bars or an ATR recomputation. Historical owner transport,
fresh empty book, window, liquidity and stop-ask eligibility are explicit
controls. No historical quote/decision-cache contents or fills are invented.
The partial prior-claim setup is a provenance counterfactual, not a directly
logged internal marker value. A separate test exercises recorded owner recovery
and second-cap sequence through the real retirement/cap functions.

SXTC full-window replay: 19 recorded completed probes. OFF produces 16 consumed
bar suppressions and zero drafts. ON first arms at age 3, zero suppressions,
3 open + 2 cancel drafts per account: one initial rest plus two normal reprices,
all one economic opportunity. These are drafts, NOT executed buys or fills.
DKI: no fresh flip in the window; cap retained, zero open drafts. Only 11 probes
join to the 12 bounded DB candles because the last candle closes after the log
window. No missing probe is synthesized.

Tests cover held primary/mirror, union quantity, working primary/mirror,
emit/fill claims, contradictory identity/rows, stale/unreadable book/restore,
boot/gap/incomplete-line/removal, historical delivery, stale/pre-watch SELL,
duplicate delivery, KEEP-REST cancellation, RETRY-OFF and close-boundary restart.
Mutation harnesses retain 7 BUY controls plus 11 SELL controls, including
restoring the actual original d68 dispatcher in memory. All receipts, including
pre-build failures, are retained separately. No checkout mutation is used.
Supplemental `test_both_paths.py` explicitly runs both SLOT switches ON against
the recorded LPCN BUY and SXTC SELL fixtures; 2 pass. These are supplemental
tests, not part of the full `tests/unit` denominator.

NFQ2 original source and own NFQ+HOTFIX proof worktree remain untouched/read only.
The latter froze runtime `494bbeefdc0cd392747f528010c4f73043d2477b`, final receipts
head `e4578d44c8e48a1bbbe0f8b0185ae05908bafb1c`. Its three active-hold benchmarks
are accepted scoped evidence, NOT live OMS certification or NFQ readiness.
Joint SLOT+NFQ+HOTFIX exact-tree integration remains a parent-review gate.
#1115 / RPGSTALE remain parked and unmodified.
