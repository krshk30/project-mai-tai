# L7 Unsent-Preparation Successor

Task: L7-ORB-WEBULL-TICKCAP-20261022, accepted independent P2 follow-up.
Parent frozen UNRUN: 8330f9aeed6d9037aad0cd85002d66c3b6cea9d5.
Worktree: /private/tmp/mai-tai-l7-orb-webull-unsent-20261022.
Branch: codex/l7-orb-webull-unsent-20261022.

## Assessment And Contract

Arendt/main finding is supported: _prepare_bot_control_entry commits a pending
row (submitted_at=None) under enabled native bot control, then the final Webull
fence may refuse. Parent8330 marked only the intent rejected, leaving the row
pending. This is a bookkeeping defect, not permission to terminalize prior or
uncertain dispatch, weaken the economic writer, or rerun the incident live.

Read preparation and its three callers, final dispatch/report caller, native
ControlledBrokerAdapter/authorize/authorize_entry, BotControlStore._lock and
prove_unsent_entry, RPG abort, journal unsent contract, model/store identity and
native-control-enabled fixtures. Existing STOP proof requires actual STOP and
cannot be reused/forged for this RUN-time local policy refusal. RPG abort is
also not a drop-in cleanup authority for an arbitrary prepared/uncertain row.

Preparation now returns the UUID ONLY for a row newly created by that invocation;
existing rows return None. Previously the outer method discarded all return
values. All other callers still ignore the result; no dispatch policy changes.
The Webull/open caller captures this ephemeral current-call authority.

On final fence refusal ONLY, settle_prepared_webull_refusal locks the same
control state before the order as native authorize_entry, reloads the order and
locks/reloads its intent, then checks exact strategy/account/provider/live scope,
parent/open/side/quantity, order UUID/coid/parent binding, symbol/side/quantity,
order type/TIF, pending status, no broker ID, no submitted timestamp, exact
prepared payload with nonempty preparation stamp/generation, absent permit KEY
in both row and request (even false/corrupt markers refuse), and no Fill or
BrokerOrderEvent. It does not infer unsent from an absent broker ACK alone.

Only then does it set this row aborted plus a distinct local no-wire receipt.
The existing caller marks its intent rejected/client_abort and commits both in
the SAME transaction, while holding the control/order locks. It never invokes
the report/economic writer or creates execution events. Without a prepared row,
the ordinary no-order intent refusal remains. Existing/changed/permit/unknown
evidence raises orb_pastlevel_prepared_dispatch_UNKNOWN BEFORE terminal writes;
the existing durable order AND parent remain unchanged for reconciliation.
No STOP, native terminal, fill-zero or live/current-flat attestation is invented.

## New Controls, All UNRUN

One NEW module, five test functions / 28 hand-enumerated inputs, not collected:

- Fresh enabled native control: actual preparation/permit/dispatch/report flow.
- Stale, changed-shape, expired window, missing and above-cap quote: actual OMS
  with native bot control enabled; exact pending row observed after preparation,
  then row aborted and intent rejected together; original geometry retained,
  no delegate submit/preview, no fills/VP/events or forged STOP proof.
- Eleven preservation inputs: actual native permit, UNKNOWN ACK with submitted
  parent, broker ID, submitted clock, accepted/unknown status, false permit KEY,
  request permit, broker event, reused unpermitted row, wrong created-row UUID.
  Whole order/parent snapshots including timestamps and payload stay identical.
- Ten exact-preimage inputs: changed parent link/account/symbol/quantity/side/
  order type/TIF/payload/preparation stamp/generation, all refuse cleanup.
- Existing fill: refuses cleanup and retains exact economic row/identity/amount.

Existing parent51 tests and all prior assertions are untouched. No test outcome
or database lock behavior is claimed from source inspection. The synthetic
SQLite controls do not establish actual concurrent PostgreSQL serialization;
the state->order lock-order argument remains source evidence pending review.

Proposed main-only future module selection, NOT EXECUTED, no overlap authorized:

```sh
python -m pytest -q tests/unit/test_l7_orb_webull_unsent_cleanup.py tests/unit/test_l7_orb_webull_tickcap.py tests/unit/test_l8_orb_control_envelope.py tests/unit/test_orbpast_source_regressions.py tests/unit/test_orb_pastlevel.py tests/unit/test_orbpast_fresh_ask_applicability.py tests/unit/test_orbroute1_oms_chain.py
```

Parent handoff's full preservation selection still applies. Main controls any
separate existing test_killsw1_postgres.py execution/environment; no database
setup/adoption/connection or PG run is authorized/performed by this worker.

## Scope And Pins

Successor impact: preparation return value -> Webull final fence failure ->
bounded exact-row helper -> existing intent-refusal setter -> one commit.
Only refusal path adds database reads/locks; fresh wire selection and provider
implementation are unchanged. Economic fills/VP/PnL are read-only/no DML here.
Schwab, Webull exits/pairs, ATR, actor/runtime/UI/ops, POM, other owners and shared
queue/status/LATEST files are unchanged. Parent8330 worktree remains frozen.
Its documented Webull OMS-versus-transport fence limitation is NOT resolved or
claimed resolved by this bookkeeping successor.

SHA256:
- src/project_mai_tai/oms/service.py:
  cab807ea72f6e0a9d78974dadc5826ec7378e77699717f9590078b0152014725
- src/project_mai_tai/oms/orb_pastlevel.py:
  a6c56df7af8b1db6f9cd452a6561fe338300a7869f1db15db8bfcd36170e4307
- tests/unit/test_l7_orb_webull_unsent_cleanup.py:
  6c43f6be9c6d6b8ad88c5ac4ffff6c664ab066bad9009a4c4930e8ca1f9378f6

All worker runtime/tests/imports/AST/compile/DB/provider/SSH/native orders/live
replay/push/install/restart remain UNRUN. Static whitespace inspection is not a
test PASS. Committed head, numstat and bundle pin follow outside this file to
avoid self-referential hashes. Completion is source handoff only, not lane GO.
