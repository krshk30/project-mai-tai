# OWNMIX1 and RPGSTUCK1: independent Step 0

Assessment only. No production fix, service action, settings edit, or ledger repair.
Base: `e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89`; loaded application source is
`bbb4360453eb38570a3eb988122d73f97a886a61`. Dates/times below are October 5 ET unless UTC is explicit.

## Decision first

- OWNMIX1: **AGREE, REAL FAILURE**. OMS closed v2's 180-share MI row using ORB's already-recorded
  2-share exit. This violates the own-entry identity requirement. Native broker protection
  nevertheless sold the real v2 position later: **180 at $3.05, 09:39:37 ET**, child `1008165371002`.
- RPGSTUCK1: **AGREE on the failures; DISAGREE with requested/missing-ack as a sufficient cause**.
  APUS has two durable refused tickets, and both acknowledgements exist. Its same-segment terminal
  tickets still return `entry_owned=True`. VEEA has a refused primary plus a held-unknown Webull
  ticket. Both primary replacement refusals are pre-wire OMS comparisons, not broker refusals.
- **STOP before building**, per the supplied instruction that a cause disagreement must be sent
  with both versions. The corrections below need acknowledgement before either proposed fix
  is treated as agreed. No fix PR exists from this assessment. No readiness PASS is claimed.
- Additional same-class finding: virtual RESTORE omits strategy identity. The MI log restored
  180 at 09:34:34.763; at 09:39:50.714 it cleared **two** virtual MI rows of 180 each. The ledger
  had one v2 entry of 180 and one already-closed ORB entry of 2. Do not repair either ledger by hand.

## Independent instruments and boundaries

Own pulls, not the reviewer's scratchpad, ran at nice 19 on `mai-tai-vps`:

| Evidence | Read time UTC | Scope / bound | Local raw file |
|---|---|---|---|
| E1 | 14:08:26.342484 | SQL read-only transaction, 8 s/query; ten queries, limits 20/60/100; 31 retained files per service; no Redis reads | `/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005/ownmix-rpgstuck-own-read.json` |
| E2 | 14:09:32.479784 | Two exact-parent Schwab GETs, HTTP 200, 3377 and 3936 response bytes; 1 MiB cap/request; existing token only, no refresh | `/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005/ownmix-own-schwab-exits.json` |
| E3 | 14:14:04.642866 | Four exact durable authorizations, filled-buy census, today's MI RESTORE/CLEAR lines; read-only SQL | `/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005/ownmix-rpgstuck-followup.json` |

E1 sha256 `a6559b94e6565ecf797daa46756d0536d070d085ce12986af0a2c3cb80f8e951`.
E2 sha256 `fe2e9f93a735970888433a9ba455a2b887c9a7e20a87241c4ef70cbdfb1ed8d1`.
E3 sha256 `fe2f1c30554994515ddbdf3b346e33df39edf5720c1a07a7ddaf8722c1dd885c`.
Pull scripts are preserved beside those files. Account identifiers are redacted in E2;
no token bytes were emitted. E1's security scrub also redacted the non-secret authorization
object; E3 retrieves only those four trading authorizations to remove that measurement gap.

Fresh 10:06 ET identity read: clean checkout e1ce3b39; OMS 23705, v2 26811, strategy 24025,
orb-schwab 27173, each NRestarts=0. These reads do not authorize a deployment.

## Part A: A1-A8

| Claim | Verdict | Own evidence / correction | New or old |
|---|---|---|---|
| A1 ORB MI 2 filled, then its bracket sold 2 | AGREE | E1 fill times 09:30:22 and 09:30:29; E2 parent 1008165370304 has child 1008165370307 FILLED 2 @2.80 | First filled ORB Schwab trade in this account in the all-time ORB census |
| A2 v2 MI 180 filled and managed row opened | AGREE | E1 row `1c4f6d03-8dc7-4472-855c-c9b8706da4d3`, original qty180, entry09:34:33.120513; E2 parent1008165370999 filled33+50+97 at3.31 | First observed cross-strategy MI overlap; not a sizing error |
| A3 first exit miss correctly names v2 | AGREE | E1 09:34:34.133 entry coid `schwab_1m_v2-MI-open-2220fd5b2d58`, parent1008165370999, qty180 | Existing polling path |
| A4 repeated attribution of ORB child | AGREE | E1 09:34:48.711 onwards, child1008165370307 already_recorded about every15s | Wrong-entry selection, old code |
| A5 poll closes v2 from ORB2 | AGREE | E1 09:35:06.144 qty2@2.8; row updated09:35:06.265815; resolved-flat09:35:06.314; CLEAR09:39:50.714 | Wrong selected parent bypasses row ownership, despite durable-fill check |
| A6 both broker books flat; no v2 Schwab SELL180 in DB | AGREE | E1 fresh accounts zero at10:08:24; five MI fills include no Schwab v2 exit. E2 proves real v2 sale09:39:37 | Recording omission, not evidence the broker failed to exit |
| A7 latest UPDATED filled buy is the cause | AGREE | `oms/service.py:7251` selects account+symbol+filled BUY, descending updated_at, no strategy/episode predicate; :8333 and :8630 both use it. ORB row refreshed after v2 entry | Introduced in `4f8f1d36e439a34cbcfd06448d7b8e6fa5e60804` (#566, July27); not introduced by October3 installs |
| A8 one mismatch of42; halt not the closing cause | AGREE with coverage limit | E1 independently counts42 polls across31 retained OMS files:41 match prior logged open qty,1 differs (MI180 vs2). This is not proof of41 correctly-owned exits. No code dependency on missing bars explains the close | Old lookup; new practical shared-account exposure |

Historic go-live evidence is not inferred from a merge: `docs/handoff-log.md` July27 pt4 records
#565/#566 deployed and exit-record flag ON that evening. September30 Mode B records ORB Schwab
LIVE19:31 ET. The October3 joint install retained that route. At E3 census, 2718 filled/partial
BUY rows since August3 include exactly one account/symbol with two strategy IDs: Schwab MI,
2 BUY rows. All-time orb_schwab rows =1 cancelled (October2),2 filled (today's buy/sell).
**Earlier demonstrated cross-strategy closes:0 in these retained populations;1 today.**
Earlier same-strategy wrong-episode closes remain UNMEASURED; equal quantities cannot exclude them.
The complete 42 poll/open comparisons are reproducible from E1; retained coverage starts September8.

## Actual Schwab exit (report only)

| Position | Entry parent | Filled exit child | Time ET | Qty | Price | Execution activity |
|---|---|---|---|---|---|---|
| ORB | 1008165370304 | 1008165370307 | 09:30:29 | 2 | 2.80 | In E2 ORB subtree |
| v2 | 1008165370999 | **1008165371002** | **09:39:37** | **180** | **3.05** | **132509514360** |

V2 OCO container1008165371000; target1008165371001 cancelled at09:39:37. Its cancelled zero-price
activity is not a sell fill. Real v2 entry executions:33 at09:34:22,50+97 at09:34:24, all3.31.
Execution price3.05 is the broker's fill, not the pre-rounding stop reference3.0455.
No backfill or incident disposition has been performed.

## Every entry lookup (13)

Line numbers at e1ce3b39. UNSAFE means a foreign strategy/older episode can reach the lookup,
not that every path has a measured incident. Row UUID fences alone do not identify the parent.

| Line / caller | Protection today | Cross-strategy reach / assessment |
|---|---|---|
| 3523 `_release_exit_reservation_before_close` | Webull pair release before a software sell | UNSAFE fallback base lookup; can cancel another entry's pair |
| 4802 `_check_webull_uncovered_shares` | Coverage/protection state for managed row | UNSAFE chooses another entry's protection state |
| 4943 `_mark_webull_protect_state` | Durable resting/released/failed state | UNSAFE can stamp another BUY's payload |
| 5368 `_prepare_confirmation_webull_leg` | Confirmation cancel pair, row-UUID fence | UNSAFE parent identity independent of the row fence |
| 5561 `_confirmation_reprotect_spec` | Reattach protection with managed quantity/price | UNSAFE base can come from another episode |
| 6124 `_release_native_oco_for_cw_flip` | Release bracket before ATR flip close | UNSAFE row fence does not bind selected broker parent |
| 7345 `_persist_webull_protect_base` | Persist attach handle on exact fill entry | SAFE when nonempty supplied entry coid; optional empty default is UNSAFE fallback, must be removed/refused in a fix |
| 7805 `_probe_webull_late_close_fill` | Detect already-filled pair before retry sell | UNSAFE quantity>=managed qty is not ownership proof |
| 8161 `_v2_close_reconcile_flat` | Record child when confirmed broker-flat | UNSAFE attribution, although a genuine aggregate zero proves no remaining broker shares |
| 8333 `_poll_native_oco_exits` | Poll/record exits independently of close rejects | UNSAFE, today's observed path |
| 8575 `_close_resolved_oco_managed_row._read_base` | Fetch child if no detail supplied | UNSAFE generic parent selection |
| 8630 `_close_resolved_oco_managed_row._close` | Durable fill required before closing row | UNSAFE durable fill belongs to selected parent, not managed entry |
| 11687 `_release_v2_eod_oco` | 16:00/EH protection handover | UNSAFE pair base despite expected-row UUID |

Schwab's adapter already walks an exact broker parent's subtree. It is given the WRONG parent
by OMS; weakening that adapter is neither necessary nor safe. Webull's persisted exact protection
handle must be preserved, not replaced by an account/symbol latest-buy fallback.

## Every managed-row resolution caller (10)

All ten reach an unbound entry selection in the common resolution helper today.

| Line / caller | Guard today | Assessment |
|---|---|---|
| 5276 `_webull_cancel_then_sell` | Pair detail + snapshot row UUID | UNSAFE foreign pair/detail possible |
| 6455 `_evaluate_v2_managed_exit` confirmation | Expected row UUID | UNSAFE selected entry still not row-bound |
| 6815 `_evaluate_v2_managed_exit` CW flip | Snapshot row UUID | UNSAFE release/record parent not row-bound |
| 8381 `_poll_native_oco_exits` | Fetched detail; no expected row UUID passed | UNSAFE, observed MI |
| 8731 `_close_broker_flat_phantom_managed_row` | Expected row UUID; requires child fill | UNSAFE child may be foreign |
| 9019 `_emit_v2_exit_on_loop` | Detail + expected row UUID after await | UNSAFE wrong parent can survive UUID race fence |
| 9543 `_refresh_native_oco_armed_state` | Account/symbol filled set, pending row UUID if present | UNSAFE symbol-level resolved/armed evidence is not episode evidence |
| 11162 `_v2_eod_cancel_and_reexit_one` | Broker reports child FILLED | UNSAFE common helper can select another entry |
| 11336 `_v2_eod_place_pm_exit` | Symbol resolved-by-fill set | UNSAFE, also suppresses PM placement after calling helper without checking its result |
| 11493 `_v2_eod_oco_transition` | Pair detail + snapshot UUID; handles recording false | UNSAFE detail parent remains unbound |

## Class sweep

| Area | Verdict | Reason / remaining limit |
|---|---|---|
| AccountPosition / fresh broker flat reads | SAFE for account truth, not bot ownership | Aggregate zero on a successful read means neither bot has shares; positive aggregate cannot identify either bot. Failed reads remain UNKNOWN |
| `get_virtual_position` | SAFE cross-strategy scope | Strategy+account+symbol predicate |
| VIRTUAL-CLEAR | SAFE for a proven aggregate zero, not a child attribution | All virtual rows may be zeroed only after backing/fresh-fill guard. Today's duplicate180 clear exposes an earlier restore corruption; zero book at09:39:50 was real after v2 native exit |
| VIRTUAL-RESTORE (`oms/store.py:1059`) | UNSAFE, same class | Managed row strategy_code is ignored by VirtualPosition query (account+symbol only). E3 restore180 and later two180 clear, E1 strategy rows, support the live consequence. Introduced #714 `70ca930d11375e71b40dd52a4078cc852620069f`, August17 |
| Open managed row lookup | SAFE only as v2 ladder lane, not entry proof | Current writer `_apply_managed_position_after_fill` rejects non-v2 strategies; unique open account+symbol row. It stores no entry order/coid. An entry binding must be durable |
| Reconciler positions/fills | SAFE as aggregate census; per-strategy episode UNMEASURED | It sums strategy virtual/fill balances before comparing broker aggregate, retains strategy labels, does not close a position. It correctly exposes missing180 exit but cannot resolve this ownership defect |
| Webull fanout collision guard | SAFE conservative account-wide veto | Any existing arm/managed/broker holding blocks opening, rather than claiming another strategy's holding |
| Late-close fill guard | UNSAFE | Entry lookup7805 lacks row parent binding; quantity check alone insufficient |
| Manual-stop symbol veto / hard-stop registry | SAFE cross-strategy scope | Global manual veto intentionally blocks all opening/increasing intents for symbol. Protective registry key is strategy+account+symbol, not account+symbol alone |
| ORB own live position/exit read | SAFE against v2 selection; older-ORB-episode UNMEASURED | `orb_schwab_exits.open_entries` joins Strategy=orb_schwab and same-strategy virtual row, selects an exact entry. Service's direct broker holding read is a conservative entry veto, not ownership attribution |
| Native OCO stand-down / resolved symbol sets | UNSAFE as sole episode proof | Aggregate armed/resolved symbols can refer to another bot's bracket; must be bound to owned entry before suppressing its ladder or resolving its row |

This is a scoped class audit, not certification of every query in the entire repository.
EH has no Schwab native bracket but still reaches Webull pair lookup/release/late-close/confirmation
paths. They need the same binding on both accounts and all sessions, not an RTH-only patch.

## Part B: B1-B5

| Claim | Verdict | Own evidence / correction | New or old |
|---|---|---|---|
| B1 APUS clears old legs but neither replacement reaches broker | AGREE | E1 durable jobs: primary refused and Webull refused.901.758ms primary readback;1422.766ms submit; Webull2709.842ms readback and7827.118ms last attempt | New RPG1/NFQ1 composition failure, first live RTH session |
| B2 VEEA failure, unproven old order | AGREE issue; DISAGREE leg attribution | Primary cancels with explicit zero in969.119ms, then OMS rejects at2682.435ms. `exact_old_order_unproven` is durable Webull ticket `faa55c1f-262b-52e2-adcc-280ab1e5e2ff`, not primary | New RPG1 runtime; existing distance refusal is intentional |
| B3 no rest despite allowed quotes, admission flood | AGREE shape; count difference disclosed | Own fixed09:36:00-09:54:00 ET window:34 VEEA allowed checks,2118 admissions,0 replacement broker rows. Reviewer's36/2188 is not reproduced; boundary difference remains unproven | New handoff-owned retry flood; admission logger itself existed earlier |
| B4 requested handoff missing acknowledgements is cause | DISAGREE as explanation for APUS | Four persisted per-leg jobs,0 persisted requested jobs. Offline actual refused/refused APUS jobs return entry_owned=True; VEEA refused/held_unknown alsoTrue. Requested synthetic aggregate is acknowledged by both account tickets, but terminal same-segment branch still blocks | New `_rpg_entry_owned` terminal-ownership branch, RPG1 |
| B5 first2 RTH reprices failed, missed entries only | AGREE 2/2 failure; downstream fills/cost UNMEASURED | Exactly APUS09:32 and VEEA09:36 in own frozen window, four per-leg tickets, no replacement broker orders. No direct duplicate-buy/oversell shown by these sequences | First RTH since October3 joint go-live; no earlier observed RTH tickets in durable census |

### Both cause versions, and why the proposed narrow fix is insufficient

Reviewer hypothesis: synthetic phase=requested lacks all leg acknowledgements; release that ownership.
Own version: acknowledgements exist for APUS, yet `schwab_1m_v2.py:5307` deliberately owns refused/
expired tickets for the same segment. VEEA's held_unknown also owns globally. Removing only a requested
latch cannot fix either complete sequence. Four actual persisted tickets reproduced offline, no
invented market inputs or live changes.

There are two earlier failures before that lock:

1. **Schwab canonical-price mismatch.** Actual authorized APUS stop/limit5.2720/5.2983 is rewritten
   by `_apply_v2_oco_bracket_entry` to5.27/5.30; VEEA5.5093/5.5368 becomes5.51/5.54. The later
   `_rpg_open_refusal` compares strings to the unrounded authorization and returns
   `rpg_current_price_size_or_identity_changed`. E1 intent refusal codes plus E3 actual authorizations
   and the offline source-method replay confirm both. Quantities113/108 remain identical.
   A terminal-release-only fix restores a next-bar path but leaves immediate handoffs broken.
2. **Webull local hold distinction.** PRICE_AGGRESSIVE_precheck logs `[OMS-NFQ1] decision=held`, but
   `service.py:12889` stores this in `_webull_mirror_deferred_by_slot`, not `_nfq_price_holds`.
   `_rpg_begin_cancel` checks only the latter for proven local no-wire. VEEA had no Webull BUY row,
   so it becomes held_unknown. Actual durable job has local_no_wire=false. A proven no-wire
   distance hold must be retired/accounted for without pretending a broker cancellation occurred.

APUS's Webull distance9.7117% refusal remains correct; neither age nor band should be widened.
Normal retry on a later bar when placeable is the requested ending. However a truly unreadable
old broker order cannot safely be relabelled no-wire merely to release ownership. The agreed fix
must preserve strict readback and duplicate-buy protection; distinguish proven-local no-wire from
genuine unknown dispatch, with a proven reconciliation path, not an unmeasured timeout.

Runtime and ownership code were added in `c80391bfca41813580ed2274d7bc1afc5b4f9a3d` (RPG1 piece3,
October3), deployed joint install bbb43604 COMPLETE23:22:53 ET October3. Current v2 started02:53:29Z
October4. The prior first-rest ownership logger predates RPG1; the handoff loop now calls it on
every refreshed authorization, including terminal tickets. Once-per-bar logging must not replace
required fresh gate evaluation with stale permission.

## Verification and next build boundary

`replay_assessment.py` uses the actual E1 tickets, E3 authorizations and persisted intent metadata.
It demonstrates APUS/VEEA ownership with zero requested durable tickets, reproduces both primary
refusal codes (stop+limit differ), and checks42 historical polls against their prior logged opens.
Run with PYTHONPATH pointing to this worktree's src; import path was printed and verified.

Baseline focused tests (unchanged source): **68 passed in5.48s**, covering `test_oco_exit_ownership`,
`test_rpg1_runtime`, `test_rpg1_runtime_nfq`, `test_rpg1_nfq1_composition`. These existing tests do NOT
prove the requested fixes; live-recorded replay reproduces missing coverage. No full-suite/head
mutation or CI success for a fix is claimed because no fix has been built.

On acknowledgement of the corrected causes: separate OWNMIX1 and RPGSTUCK1 PRs, actual payload
fixtures, own-entry/strategy/episode binding in all listed paths, O-T1..8 and R-T1..7 as specified.
Historical equal-quantity cases require true parent verification, not invented ownership.
PMREST1 is pinned but unmerged; PMPRINT1/PMFLIP1 builds remain gated by the prior explicit operator
answers, not silently authorized by this request. Composition claims must name what code was tested.
Install proposal only after exact-head independent pins: one evening plan, required OMS+strategy
and v2 restarts, configured entry windows unchanged, flatness proofs and scanner validation.
No install GO, ledger repair GO, new timeout, or trading-rule change is inferred from this assessment.
