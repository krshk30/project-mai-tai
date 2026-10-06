# MIRRORHOLD1 controlled build verification

[codex] Isolated branch `codex/mirrorhold1`, base
`c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`. This source build supersedes only
the build-paused status in the historical plan receipt `629703f8`. This lane has
not modified the separate Step0 assessment or WEBULL429 worktree. No production,
install candidate, merge, shared HANDOFF, or `atr_reprice_handoff.py` edits.
The four-line runtime price-budget integration is explicitly scoped below.

## Counter and ownership contract

- Flag `oms_v2_webull_mirror_retained_hold_enabled` defaults OFF; the exhaustive
  health catalog expects OFF. Neither source release nor this report activates it.
- One initial actual Webull submission plus at most three actual resubmissions:
  four evidenced wire clients per exact account/strategy/symbol/segment/slot.
  Reprices and RPG authorization nonces never reset that durable budget.
- Distance outside 8%, stale quotes, local risk/collision, callback refusal before
  the adapter, queue reservations and invalidated enqueue tokens are free.
  Unknown dispatch reserves ownership, not new permission or a guessed counter.
  The adapter submit timestamp evidences the SDK submission boundary, not remote
  receipt or execution. A transport failure after that boundary stays uncertain
  even when its submission has been charged; missing evidence never means zero.
- Existing `DashboardSnapshot` storage holds the revision-CAS owner, canonical
  tick-rounded strategy stop/limit generation, exact quantity, enqueue token,
  dispatch client and unresolved fence. No schema migration. The price generation
  is not a claim about subsequent marketable shaping or an observed venue fill.
- Original and retry submissions persist the pending order and dispatch fence
  before broker await. Unknown results remain pending/submitted for inflight
  readers. OFF prevents new retained owners but continues existing durable ones.
- Restart invalidates queued tokens, never dispatch reservations. Lost enqueue
  ACK invalidates the exact token unless dispatch has already reserved ownership.
  NFQ transfer retires the predecessor actor/token and preserves uncertain wire
  ownership; exact pending identity is linked locally, not claimed as broker echo.
- Only own Webull acceptance/fill or a proved terminal segment/session/window
  ends the hold. Sibling acceptance, rejection and fill cannot settle this leg.
  Real reprices can promote a previously accepted price only with old-clear and
  the unchanged wire budget. Late reports cannot resurrect a retired owner.
- Final dispatch rechecks segment, old tickets, fresh in-band price, quantity,
  generation, client uniqueness and cap. Normal sizing, risk/collision, broker
  shaping, entry windows and 8% constraints remain authoritative.
- Ordinary unproven `aborted` orders remain inflight. If T43's exact read-only
  `local_rpg_abort_proof` exists, this reader delegates to it; it never releases
  the separately owned old ticket or changes the shared journal. Exact persisted
  terminal recovery is confined to this reservation and requires linked intent,
  account/strategy/client/event/quantity/generation/segment/slot evidence, no Fill,
  and an accounted historical wire budget. Missing evidence stays fenced.
- The sole RPG runtime change is a price-budget hook: retained owners use actual
  wires; legacy/default-OFF owners retain the existing cap. Nonces are not rewound.

## Recorded Versus Controlled

Own frozen Step0 census at **2026-10-06 12:55:51.850465 ET**: 76 forgotten
events; 75 price/cap events = 62 RPG + 13 caps; 71 before 11:30. The **75-event**
symbol breakdown is AIXI 54, IPDN 9, XHG 8, OLOX 4 across seven symbol-segments;
the accepted retirement is the additional event in 76. Four named acceptance segments
are a subset: 66 events across three symbols, not the whole census.

Fixture `tests/fixtures/mirrorhold1_recorded_step0.json` SHA256:
`721346c440c986b081c5115148e1f0391bace9615f57b56888a7137ddb23e494`.
It retains selected independent raw timeline/capture rows and their record hashes.
Raw transcript:
`/Users/velkris/.codex/sessions/2026/10/06/rollout-2026-10-06T11-12-58-01a111c6-6c38-7b83-a351-feca34a7999c.jsonl`.

| Raw record | Cutoff / purpose | SHA256 of record stdout |
| --- | --- | --- |
| 845 | 12:55:51.850465 ET census | 347178d8f43970031ea567043ff8d97e09cabe7fe20983304c185251217807ab |
| 859 | retained census | be5a8344e7d45225359bae66ee333df8c084d73b59691f61dac78104bd8631b1 |
| 873 | 16:57:04.924022Z bounded OMS/v2 timeline | de9acdffc4f4cbe33b6007693a0a08767b804554c350e2257ebb1f0fa5578f4a |
| 878 | 16:57:08.900256Z scoped read-only DB | 83327ec1656dd21027c3c01985494142ebf493b98003ac922f30fd716f6d2112 |
| 908 | bounded AIXI audit | 16cffe03c12094906091e254ead11af0b4bef1c2c715cbabc57ee46288e76be0 |
| 954 | 17:00:11.089136Z scoped capture proxies | bb1f26c88a7bf25ab5b55b39d6bb8f1c78a4fd1ac9193fbd6088950e4aeb1562 |

The raw DB pulls used read-only transactions and bounded timeouts. No new
production/broker pulls were made during this build. Primary persisted two-decimal
stops are replay proxies, NOT original final Webull wire prices. Captured asks and
timestamps are projected into a controlled cache. SDK replies, delivery ordering,
crashes, successful submissions, partial/full fills and completed-line readiness
are controlled inputs. No test establishes a historical Webull fill.

| Case | Controlled result / boundary |
| --- | --- |
| IPDN 10:27 segment | Nine sampled prices through first sampled in-band 10:48 stop 4.73 / ask 4.38; zero wires while outside, one controlled SDK submission in band. Earlier hypothetical Webull fill UNMEASURED. |
| IPDN 11:44 segment | Exact 11:55 stop 4.5839 / ask 4.2 (8.375%) survives repeated nonces; next sampled 11:56 stop 4.54 / ask 4.19 submits in the controlled harness. |
| XHG 10:32 | Primary rejection/acceptance/partial/full outcomes cannot erase Webull hold. |
| AIXI 09:57 | Actual terminal cancel remains UNMEASURED: own bounded rows show 09:54/09:56 reprice cancellations and 09:57 PLACE. Named 09:57 is never treated as a historical segment end. Separate controlled actual cancel/flip/window/end cases fence queued generations and restart. |
| PM | PA1 retained owner remains RTH-only. Separate `eh_resting` software-cross/marketable LIMIT guards and windows are unchanged; measured PM PA1 recovery UNMEASURED. |

## Verification

Final source/test freeze: 2026-10-06, after explicit OFF catalog/count pins and
all-eight-on retained-owner control. Earlier exploratory/interrupted full runs
are superseded, not pristine or final evidence.

- New MIRRORHOLD1 controls: **68 passed**, including real OMS + real adapter with
  a fake SDK endpoint, wire counting, reprices/nonces, both crash boundaries,
  enqueue ambiguity, CAS, NFQ transfer, per-leg fills/retirement, foreign/stale
  cancels/reports, terminal audit negatives and post-claim price/quantity/quote.
- Focused companion pair on final source: **628 passed**, including PM/all-on,
  RPG runtime/NFQ/piece-3/handoff/review guards, flag catalog and test-oracle lint.
  Explicit new all-eight-on RPG case uses controlled completed-line readiness;
  it is not a line-history attestation.
- Additional PM nightly catalog/behavior controls after the last count pin:
  **16 passed**. No activation expectation or runtime behavior changed.
- **11/11 isolated mutations killed**: queue charges counter, reprice resets
  counter, RPG forget deletes owner, final cap/client guard removal, cancel
  permission retention, affirmative no-wire removal, compound enqueue token
  fence removal, wire-evidence removal, CAS removal and OFF recreating NFQ actor.
  Mutation needle/import failures do not count as kills. No tracked source edits
  are performed by the runner.
- Ruff for new source/tests/runner and `git diff --check`: PASS.
- Final full-suite baseline comparison: **no new failure/error IDs**. Untouched
  source `c21` in separate assessment worktree: 6514 passed, 56 failed, 12 errors,
  2 skipped, 1 xfailed (292.15s). Final frozen source/test tree: **6582 passed,
  56 failed, 12 errors, 2 skipped, 1 xfailed** (311.58s). All 68 failure/error IDs
  match exactly, with none added or removed. No green full-suite claim: baseline
  failures include macOS shell harnesses and throughput timing; integration errors
  require an unavailable PostgreSQL server. The prior head's sole additional
  failure was the catalog 151-to-152 count pin, corrected and independently
  verified by the 16 PM nightly controls before the final post-pin full run.

Reproduction from this worktree (common prefix):
`env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python`

- New: `-m pytest -q -p no:cacheprovider tests/unit/test_mirrorhold1_retained_hold.py --tb=short --junitxml=/tmp/mirrorhold1-final-new.xml`
- Mutations: `docs/review-artifacts/mirrorhold1/mutate_controls.py > /tmp/mirrorhold1-final-mutations.json`
- Full (also pristine base in its separate WT): `-m pytest -q -p no:cacheprovider --tb=line --junitxml=/tmp/mirrorhold1-final-full.xml > /tmp/mirrorhold1-final-full.log 2>&1`, run at low scheduling priority.
- Focused full command is retained in `commands.txt`; reports below are local
  generated artifacts, not production observations.

| Generated evidence | SHA256 |
| --- | --- |
| /tmp/mirrorhold1-final-new.xml | 713bc99e7e668bb1b7b3e4acafce0976aa73693f89c3e90aa666495a5c6fd626 |
| /tmp/mirrorhold1-final-new.log | baf2a55c67cb5c77634d41785b97aeb28356d6d8e3922c02e4ae9c1e7e6150a2 |
| /tmp/mirrorhold1-final-focused.xml | 2cd36ce57a776f159904f094b973134f140ab6c02a532de280bc4754c628cc5b |
| /tmp/mirrorhold1-final-focused.log | c74b986c4a896a166b87da7865aa6b0de03d792275439667338d37c1a8eb0b7e |
| /tmp/mirrorhold1-final-mutations.json | 13ec4c42001f195ec86c75f4379d489b70600ed24e447a0a78c238d1888d8a36 |
| /tmp/mirrorhold1-pm-catalog.xml | b1c703169e9fbe7067eff866cd0f231eacb57f8b1c780512a97072ca6d7de147 |
| /tmp/mirrorhold1-pm-catalog.log | 9c2a6afd874684996ac4dcc5dca1f88268274cc5d962b22fb72cbfecaba070f2 |
| /tmp/mirrorhold1-pristine-full.xml | 9649c5f21fda4cafbab06eaf4067c3c90e09b04ada5d71a072b961d7fc06d792 |
| /tmp/mirrorhold1-pristine-full.log | db25b2c69fd5b6a7ecade32108287bf5cb918ee1aa9902a2c0b224d16a986042 |
| /tmp/mirrorhold1-postpin-full.xml | 63b8008cb268cbcf0a00d4e96c3045312e287b657e376698b5b3e900c8a309b8 |
| /tmp/mirrorhold1-postpin-full.log | 75837925f364eac74e49bac1279fcfc98c7837d2ed5cf0ccdd476e3ca6acaa8a |
| /tmp/mirrorhold1-postpin-comparison.json | d3bd6a7cb5ae9f916a608e11d982c0c2f8910b88923367ac9e2d17ff0b5334b7 |

Frozen content hashes (SHA256, before source publication):

| Path | SHA256 |
| --- | --- |
| src/project_mai_tai/oms/mirror_retained_hold.py | 68ffebbb5dad5ca90dea5a7be91d3d6a378b33ac8f1e5152449789a9582f6700 |
| src/project_mai_tai/oms/service.py | b7ba85ddd0a87a29141ef4606d2dc59fc0ff99e3e3049d915d9b6ad14449ef2c |
| src/project_mai_tai/oms/atr_reprice_runtime.py | 842a6e3fda5e9d5038a2cb6eef22151cd3e4480276547d57c363bfc2185729a6 |
| src/project_mai_tai/broker_adapters/webull.py | f36676c819fd79ad2b83c240cf451333224fefe8fea310280fec093297831770 |
| src/project_mai_tai/settings.py | 3737f9960664d64a268880ccb2bca02e2ecb84ea3078b8b5cab0b2b0f7e26f27 |
| ops/health/expected_flags.json | ca1069de1dd5f005e8eefd37c817b8bf7ac6fceddc4e8fd6c3b7d3c7dabda033 |
| tests/unit/test_mirrorhold1_retained_hold.py | b817c057471c1be04501d788776bed3b4c7f37db01265da9b0e2853d4c334a7c |
| tests/unit/test_expected_flags_check.py | 9cfc33d9c4e28a61a8162256898482fb21054c1aa9974f439ab825aaa8b8175a |
| tests/unit/test_all_on_pm.py | 87ae22f8ad129be04afe89bf78e88ef3f920209efa98f1c8df75b2a37eb72eac |
| tests/unit/test_pmprint1_tonight_flags.py | 8283bc40336c7e2db2fcac6443af17530f8450a270c714beabe8c60777db0e5a |
| docs/review-artifacts/mirrorhold1/mutate_controls.py | e2e34e75b1956709736960e8e73bc216e16155df2984dc75b6569fa6ac5c1b8a |

## Integration Gates Still Unmeasured

Parent T43/MIRRORHOLD1 frozen-source composition (including T43's new shared
abort/rejection proofs and 70-placement acceptance population) remains
UNMEASURED until parent C-rows run on the composed source. This build runs the
existing `c21` piece-3/14 and all-on suites, not the parent's uncommitted T43 suite.
Live/PostgreSQL concurrent-writer verification, actual historical AIXI 09:57
terminal cancel, counterfactual IPDN Webull fills and measured PM PA1 recovery
remain UNMEASURED. Unknown audit/client identity or historical budget is a fence,
not grounds to infer no-wire or flat. No pin/merge/install/activation approval
is inferred from controlled tests.
