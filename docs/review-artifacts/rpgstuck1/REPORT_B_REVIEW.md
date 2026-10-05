# RPGSTUCK1 Complete B1-B6 Follow-Up

Base: `7e10baf0319da796b84934fe38994f6db4fcfc0b`, the merged pinned PM tree.
Clean `git rebase origin/main` produced `d68651686a72200913c6e7e498c574d22907ea18`.
That head was published before this follow-up using the exact old-remote lease
`7f0729ef67e97566d7c254273802665c760adf08`. One subsequent follow-up commit.
No main, pinned PM, other worktree, shared-handoff, production or ledger edits.

## Complete Review Scope

| Requirement | Proof / Change |
| --- | --- |
| B1 | Recorded RETO unknown primary stays held with reads30; independently proven/finished Webull leg emits and submits exactly one Webull BUY. No Schwab draft, quantity or generation change. Strategy source already passes. |
| B2 | Accepted AND rejected generation-bound BrokerOrder rows veto local clearance despite retained prechecks: three recorded symbols plus committed-notification variants. Existing fence retained. |
| B3 | Only persisted `skipped_before_submit/webull_mirror_precheck_deferred` proves original distance no-wire. Broker rejection, client abort and NFQ hold labels are insufficient. Later ownership refusals require the separately proven exact anchored retry chain. |
| B4 | Entry and gap holds give WAIT while old order is unproven and EXPIRED once proven clear. Unknown ownership and cancel/readback recovery retained. |
| B5 | One startup scan plus committed exact-generation notifications, durable notification hash, no permanent unknown scan/xadd/evidence query. Scan and notification/CAS work run in worker threads; live transactions retain existing retry cadence. |
| B6 | Actual strict RETO REJECTED-zero body retained. One claimed proof GET; no duplicate after crash/restart, no clear on wrong identity, missing zero, positive fill/remaining or unknown response. Priced positives use existing accounting; unpriced positives remain UNMEASURED/no-rebuy. |

Only `oms/atr_reprice_runtime.py` and `oms/service.py` change production source in
this follow-up. B1-B4 require proof, not additional strategy policy changes.

## B5 Work And Failure Bounds

The new 60-second prototype was removed before final verification. There is no
periodic unknown scan or newly invented unknown timeout. The OMS proof task blocks
on signal/stop when only held tickets remain. Over180 empty turns, one startup
scan only; over12 seconds real idle time, zero additional queries or wakeups.
Exact committed-generation notification wakes only its matching held ticket;
wrong generation leaves the durable snapshot unchanged; duplicate content is
suppressed by its stored notification hash. Notification is NOT clearance:
the serial consumer rechecks matching intents and all generation wire rows.

Notification runs after DB commit, before order-event publication. A crash between
commit and wakeup is recovered by startup regardless of stored hash, not a timer.
Out-of-process DB changes without this notification path require restart or a
new matching committed notification. Failed notification retains ownership and
logs; it does not silently authorize a replacement.

Worker queries use the existing OMS session factory and its Postgres statement,
lock, connect and pool limits (`db/session.py:65`, `settings.py:950-953`), not new
timeouts. Startup performs one snapshot query. Publication performs one scoped
snapshot query, at most two identity lookups if generation is absent from report
metadata, and CAS only for changed hashes. Cost scales with matching tickets;
no arbitrary row truncation omits ownership. Serial proof retains the existing
OMS lane. Error-only scan retries use the existing configured
`oms_broker_sync_interval_seconds` (default5, `settings.py:890`, already used by
`oms/service.py::_broker_sync_interval_seconds`), not periodic unknown probing.
Error spacing never changes ownership or the broker read allowance. Existing v2
feedback observation is not claimed to have zero database reads; the zero-work
claim applies specifically to the OMS unknown proof/wakeup task.

## Population And Dispositions

The original eight-ticket fixture is unchanged. The separate later fixture retains
**14 tickets, 11 orders, all15 intents and one actual linked Fill**, captured at
**13:17:02.125832 ET** from `tonight-startup-later-with-fills-own-read.json`.
Source SHA256: `120fc475d5620d2bbf8d55a7888a06abfb226350d669db20f315d0ca682145d2`.
These are as-of captures, not the whole live day or execution-time clearance.
The strict Schwab bodies remain the earlier12:13:55ET capture, not fresh broker
observations for this later census. Future clocks/quotes and placements are
controlled tests; no production DB or network is contacted.

| Additional Ticket | Recorded / Replayed Disposition |
| --- | --- |
| MI Schwab `c539a57f-6111-59ae-8b79-79b7fee1700a` | refused, old zero clearance; no RPG veto, current gates still apply |
| MI Webull `4be7cb2d-406b-56db-b597-3783f6e956ff` | held_unknown; actual precheck plus anchored later guard proves original no-wire. Clear stays owned until existing transaction finishes. NFQ label alone proves nothing |
| SCKT Schwab `a9eac442-d340-59ab-8c26-9c04d3453f1d` | refused, old clear; actual filled opportunity prevents rebuy independently |
| SCKT Webull `6fb89c93-a101-5200-b569-320acf5f4f85` | refused/replacement_terminal_accounted; old clear; opportunity consumed |
| SCKT Webull `889889cd-2de7-59ca-997f-fc546b728ec9` | now refused/replacement_terminal_accounted in newer census, NOT still placed; opportunity consumed |
| SCKT Webull `d86d5d38-200b-52b6-8147-6eecfe13698b` | filled/replacement_fill_accounted; exact Fill `d9e5f402-fdff-48c3-8970-9256e24761c6`, order `d5227349-a40c-4cbb-b381-7cc3a7c4919c`,280 shares at1.06. No replay, duplicate Fill or rebuy |

Original eight dispositions remain in `REPORT_STARTUP.md`. All14 restore through
real bot and OMS startup. At simulated20:05ET, four proven-local Webull tickets
expire by existing `window_closed`; RETO clears only by strict rejection proof;
SCKT stays filled/slot-consumed; zero opens/cancels. Known-fill replay changes no
Fill data. Stale SCKT placed-feedback is corrected by its actual committed Fill
without rebuy. MI broker/client-abort/NFQ-label counterfactuals remain held at
closed window, never purged. The original eight-ticket OFF valid-window race
still proves three existing replacements complete once without legacy duplicates.
OFF disables new admission, not recovery. First/reclaim legacy behavior, gap
holds and three canonical Schwab wire sequences remain in the focused suite.

## Integration And Verification

The inherited PM test is adjusted only here after PM merge:
`test_current_rpg_off_startup_restores_proof_dependent_ticket_ownership`. All four
fixtures remain. Three explicitly enumerated proven-clear tickets release and
yield primary/mirror drafts; unproven VEEA still blocks both. No skips or weakened
unknown guard. Historical characterization failures are not relabeled green.

Independent existing ownership regressions included are `test_oco_exit_ownership`,
`test_oms_native_oco_stand_down`, and `test_orphan_order_ownership_and_oversell`.
Unmerged OWN source `55563e6f` is NOT composed or claimed covered here; parent owns
that composition. PM runtime source is genuinely merged and tested here.

Final paired counts, all failed IDs,97 recorded/integration IDs,1,318 focused IDs,
and final source/test/fixture hashes are retained in `verification-b-review.json`.

| Final Suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Exact main `7e10baf0`, own fresh full units | 5500 | 56 | 0 |
| Frozen follow-up source, full units | 5632 | 56 | 0 |
| Focused merged PM / RPG / independent existing ownership | 1318 | 0 | 0 |
| Recorded startup / review / later census / PM integration | 97 | 0 | 0 |

Exact full failed-ID difference: **added0 / removed0**, identical56 IDs retained
in the JSON. Main full224.73 seconds; follow-up full227.69 seconds. Recorded
97 passed in16.68 seconds. Focused1318 passed in38.86 seconds.

| Final Production Source | SHA256 |
| --- | --- |
| `oms/atr_reprice_handoff.py` | `08bf0e12bfe331e6bd4b7f527f38aceeecfaa40348586426e0af775da686886c` |
| `oms/atr_reprice_runtime.py` | `36d5c14d33cf12eb6bf5da7d88d3d755edf50f1a8c91c78c34e7b4afba493d3a` |
| `oms/service.py` | `e354386aef2a829ab0934c609285da4b9501614afc75d39f833d6ffb9fbd02fc` |
| `strategy_core/schwab_1m_v2.py` | `ba754302c7ea42da6319804b27cfb18f25b4ff60e728b19b630fefb83adbf712` |
| `broker_adapters/atr_buy_readback.py` | `e4595a6424cc7e92c4be32257a6fabc039c4e04da630b02305fde1e41947cb78` |

Raw final logs/XML: `/tmp/rpgstuck-b-frozen-*`,
`/tmp/rpgstuck-7e10-main-full.*`. The first full run was terminated and superseded
after strengthening the wrong-generation durable assertion; source was unchanged.

Mutation scope: existing strict-reader8 + coordinator8, all prior startup32,
and16 new B/later-fill falsifiers. All64 final controls are assertion-red; no
anchor/import error counts. Final `b-*-mutations.txt` are retained. The first
wrong-generation falsifier exposed a test timing gap, fixed by a direct durable
state comparison rather than assuming asynchronous work finished in one turn.

Additionally, the parent independently ran the exact requested literal R13
`None if soft_rest or primary_blocked` -> `None if soft_rest`: B1 assertion-red,
one failed case. Exact R16 `_rpg_persisted_local_open(old_request, candidates)` ->
`_rpg_persisted_local_open(old_request, [])`: all six B2 accepted/rejected cases
assertion-red. No source edits/import/anchor errors. Raw parent outputs were
retained as `b-parent-exact-R13-mutant.txt` and `b-parent-exact-R16-mutant.txt`;
original paths, parent provenance, source and output hashes are recorded in the
JSON. These two independent literal controls are separate from our64 controls.

Use `PYTHONPATH=src` and the shared project's `.venv/bin/python`. Run full units
in exact main `/tmp/rpgstuck-main-7e10-pair` and this checkout, the focused command
below, `run_b_mutations.py`, then `collect_b_results.py`. No full suite is called
green while its56 baseline failures remain. Restart scope for separately approved
deployment remains OMS + strategy companion + v2. No merge/deployment performed.

```sh
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python \
  docs/review-artifacts/rpgstuck1/run_focused.py \
  tests/unit/test_v2_entry_composition_slot.py tests/unit/test_v2_flip_owned_first_entry.py \
  tests/unit/test_v2_resting_cancel_rth_placed.py tests/unit/test_v2_retry_one.py \
  tests/unit/test_v2_webull_resting_mirror.py tests/unit/test_rpgstuck1_schwab_sequences.py \
  tests/unit/test_rpgstuck1_startup.py tests/unit/test_rpgstuck1_review_completion.py \
  tests/unit/test_rpgstuck1_later_startup.py tests/unit/test_pmprint1_pmflip1.py \
  tests/unit/test_pmprint1_tonight_flags.py tests/unit/test_oco_exit_ownership.py \
  tests/unit/test_oms_native_oco_stand_down.py tests/unit/test_orphan_order_ownership_and_oversell.py \
  --junitxml=/tmp/rpgstuck-b-frozen-focused.xml
```
