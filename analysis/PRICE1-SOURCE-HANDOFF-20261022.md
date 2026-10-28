# PRICE1 Source / Test Author Handoff

ACTIVE CENSUS CORRECTION Claude15:51:27: withdraw10-12 (warning path did not
run/0ADD). Replacement10-21 +10-22 full delivered logs and log-only normalizer
are specified in PRICE1-LOG-CENSUS-1021-1022-READINESS-20261022.md. Earlier10-12
requests below are preserved history, NOT a current build gate. Missing1s bars
remain UNKNOWN/no fetch. No production delta for this correction.

SOURCE ONLY. Base aa16266823e7ef0ebb58a44e8efb64af417fc2bc; branch
codex/price1-buy-floor-20261022; worktree
/private/tmp/mai-tai-price1-buy-floor-20261022.
First c6da5116f7a5e760e4ed999bd02edc8a05136384 at 14:28:29 ET;
second a4d9e72866863edd9eff945fb09476de9cf26cbb at 14:43:54 ET.
L13 heartbeat preemption completed separately at f76a3f23d22490a0f19eb34504e73c46d0ac1409
14:48:41 ET; no L13 files are in this PRICE1 branch.

No tests/import/AST/compile/replay/provider/DB/SSH/build runtime executed.
Only source review, diff checks, Git commit/bundle metadata and hashing are authorized here.
All controls/drivers below UNRUN; independent review, original cases, cost and ALL-ON owed.

## API and Source Assessment

Shared module project_mai_tai.buy_price_floor:
buy_price_allowed(price, *, bot, symbol, side="buy") -> bool. Pure bounded price
validation + requested PRICE1-REFUSED log. Finite >=1 admits, missing/nonfinite BUY
refuses; SELL bypasses before parsing. No prior-close operand, fetch or connection.
buy_order_allowed(order) -> bool uses the already-held payload/OrderRequest.
Original ATR entry/stop/reference, momentum signal or ORB intended breakout is
required; a padded limit cannot rescue .9975. Present executable LIMIT/STOP_LIMIT
limit is checked in addition, not substituted for the original anchor. Cancels
(except ORB reprice plans) and every SELL bypass this entry-only rule.

Gibbs/main L2 API: call buy_price_allowed on BOTH actual current trade price and
cached executable ask before consuming WAIT/READY/dispatch. A remembered trigger
>=1 and ask<=cap are NOT sufficient when current print/ask is <1. The new combo
control is a helper contract only, NOT proof of L2 callback integration. Gibbs owns
that integration; main must merge the small overlapping ATR/OMS hunks explicitly.
Cicero MOMNEG1 remains separate. This branch does not read prior-close at LIVE
admission or modify its runtime/handoff metadata contract.

## Exact Functions / Callers / State

- momentum_paper.warning_consumer.observe_aggregate: prior-close dollar gate is
  REPLACED by current bar.close floor; same IGNORED reason and cap 16. engine
  detect_from_bucket replaces prior-close dollar gate with signal_price, retains
  missing-prior-close refusal and all percentage/reference/age/lifecycle math.
  Legacy raw-paper _can_detect is untouched; MOMNEG paper scope is not adopted.
- LiveEntryExecutor.admit refuses signal/cached ask before episode/durable claims;
  _request checks signal, raw current ask before rounding, and priced limit.
  _refresh_prepared and the existing exact-request native validate callback reuse
  _request, so current quote refreshes cannot skip this floor. Added early quote
  access is to the existing in-memory ports.quote cache, NOT a provider read.
  Existing late prewire no-dispatch settlement, sealed permits and no-resend rules
  remain unchanged; do not infer permission to revive terminal/unknown episodes.
- SchwabV2Strategy: _queue_resting_place, _reprice_resting and RPG authorization
  use _resting_price_allowed on the unrounded offset trigger before owner/plan/
  cancel/quantity writes. RPG sub-dollar returns wait, not expired. Existing
  reconciliation of real fill/terminal feedback still precedes current admission.
- ATR _eh_resting_cross_check/_fanout_rth_resting_cross check original/raw and
  placed trigger plus current ask before settle-grace or fanout claims. Reactive
  _cw_v2_quote checks trigger and _sized_open checks present ask before its
  provisional, slot, fanout and owner mutations. Original entry/quantity/route
  math is retained. No extra broker position or current quote acquisition.
- ATR hold creation in on_quote checks touch/current print/ask before legacy
  atr_fired_in_short_seg. _resolve_hold retains pending hold on PRICE1 current
  refusal; _build_hold_draft checks entry before provisional guard. Indicator
  _update_atr_state preserves touch/math but does not spend legacy segment on
  a sub-dollar touch/current price. _maybe_atr_emit, _cw_entry and legacy
  MACD/VWAP open draft decision check entry/current close before emission.
  _evaluate_completed_bar checks current close after indicator/SELL observation
  but before consuming pending MACD/VWAP entry crosses; normal age/reversal gates
  still apply when price recovers.
  Normal expiry/flip invalidation still applies; PRICE1 grants no stale setup.
- _build_webull_fanout_draft checks entry before identity construction/sizing.
  Shared _sized_open additionally checks a present sizing price; absent sizing
  quote preserves the existing default-quantity path ONLY behind the original
  known entry/trigger guards (not a fabricated current-ask proof).
- OrbSchwabOpeningOrder.on_closed_bar checks original level before placed and
  last_requested_level. New price1_waiting remembers ONLY a valid third-bar
  PRICE1 refusal; later complete fresh eligible bars may place while the existing
  service deadline/MACD/coverage gates still allow. Missing/late bars cannot gain
  this permission. Service binds symbol for logs. Open/reprice intent builders
  check original breakout; select_entry/assert_same_selection check it before
  rounding/cap and at the existing final-thread fence. No fence removed.
- OMS _refresh_working_cache_after_intent checks before its nested mirror
  finalization and any DB/cache work; refusing inside process_trade_intent would
  have consumed retained mirror state via its outer finalizer. _orb_pastlevel_request
  forwards the optional symbol keyword; other public calling forms remain valid.
- NFQ2 _nfq2_pre_submit retains its existing held state for a fresh sub-dollar
  ask; _evaluate_nfq2_holds checks fresh price/original anchors before CAS/enqueue.
  NFQ1 _nfq_pre_submit/_prepare_nfq_queues likewise keep waits before queue tokens.
  Existing periodic expiry, segment, native/unknown, phase and ownership guards
  still run; no terminal budget is restored and no unknown wire is called unsent.
- SchwabBrokerAdapter.submit_order and replace_bracket_order, WebullBrokerAdapter
  submit_order: original-anchor floor before native I/O/config lookup; explicit
  client refusal with no invented broker order/fill. SELL/cancel bypass. Broker
  Webull sub-dollar share rule, exits/protection, routing and scanner unchanged.

Persistent schema/flags: NONE added. Existing episode/hold/order tables retain
their contracts. New state only ORB price1_waiting bool on the per-session plan;
otherwise refused entry/slot/owner fields remain unchanged. Normal NFQ held writes
use existing DashboardSnapshot/intent/outcome contracts, no economic DML added.

## Controls / Main-Only Commands

Intended new controls: test_price1_floor.py 13; test_price1_dispatch.py 42;
test_price1_retained_waits.py 6 = 61. Collection/counts UNVERIFIED. These use
controlled inputs and fake transport/transactions, NOT native historical proof.
Required focused preservation modules include test_momlive1_execution.py,
test_momentum_warning.py, test_momentum_warning_followup.py,
test_schwab_1m_v2_resting_entry.py, test_schwab_1m_v2_eh_resting_entry.py,
test_schwab_1m_v2_cw_v2.py, test_schwab_1m_v2_hold_confirm.py,
test_schwab_1m_v2_atr_flip.py, test_orb_schwab_open.py, test_orb_pastlevel.py,
test_nfq1_mirror_fresh_price.py, test_nfq2_eh_fresh_price.py, test_nfq2_tick_path.py.
Main must add current L2/MOMNEG/ALL-ON selections on the exact composed head.

Proposed new-module command, NOT executed (main must claim serial slot/preflight):

```sh
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider tests/unit/test_price1_floor.py tests/unit/test_price1_dispatch.py tests/unit/test_price1_retained_waits.py
```

Source cost: fixed bounded comparisons, no provider/DB reads introduced. New
tests include existing 150-bar warmup fixtures, not whole-day tape. Actual time,
memory, P2/native cost and ALL-ON latency remain UNMEASURED, not a PASS claim.

## Finite 10-12 Input Request / Versioned Study Contract

tools/price1_candidate_counts.py and price1_candidate_compare.py are AUTHOR ONLY.
Main runs the identical absolute driver separately with baseline/current pinned
PYTHONPATHs, then compares outputs with identical input SHA. No source monkeypatch,
baseline mutation, native fills or guessed arrivals. --declared-head is a label,
NOT automatic Git verification; main binds each receipt to its actual worktree.
The successor v2 adds --expected-input-sha256 and --expected-source-root, records
actual engine/consumer source paths+hashes and rehashes the replayed bytes. This
does not attest Git identity of imported code; main freezes and pins both roots.
V3 additionally requires clock_basis provenance and distinguishes a declared
historical/counterfactual study from retained local receive evidence. The driver
does not assign new times or silently turn SIP clocks into receipt clocks.

Need existing retained 2026-10-12 warning consumer input delivery prefix, from
original session preparation through the LNKS/MNDR window (all competing names,
not just those two), prior closes/condition version, original gateway epochs,
sequence/drop counters, received/published/consumer observation ns, read-complete
checkpoints/invalidations/heartbeats, and actual contemporaneous legacy-active
sets. Include actual eligible delivered trade records/raw coverage needed for
warning ownership/lifecycle. Do not replace local receive history with SIP time.
If raw delivery for counterfactually newly owned names was not retained, mark
counterfactual coverage UNKNOWN; historical trades alone cannot supply arrivals.
No new provider/DB connection requested and no full-market 48GB export required.

JSONL header: contract=PRICE1-CANDIDATES-v3, session_date=2026-10-12,
initial_state=empty_before_session_prepare (or declared_cold_start for an explicitly
declared historical study only), prior_closes={symbol:decimal_string},
condition_version, coverage_started_ms, owner_cap=16, max_window_symbols,
study_start_ns/study_end_ns (explicit whole-minute half-open bounds in 10-12 ET),
validation_window={session_date:null-or-ISO-date,gateway_end:HH:MM,
detection_end:HH:MM,guard_end:HH:MM} from actual retained config,
complete_for_both_owner_sets=boolean, provenance (producer/path/export receipt).
clock_basis is REQUIRED: retained_consumer_delivery with receipt_provenance and
observation_provenance; OR declared_counterfactual with contract_id,
assignment_rule and delivery_scope. All required fields are nonempty strings.
Following records in original nondecreasing observed_ns order each include
legacy_active list and kind: aggregate (original AggregateObservation.from_payload
envelope); trade (exact TradePrint constructor fields incl decimal-string price,
conditions + sip_timestamp_ns + raw_coverage); complete_read (message_id);
invalidate_transport (reason); heartbeat. Coverage fields are epoch pair,
start_ms/through_ms/dropped_total/continuous/lineage, or null. Header completeness
is explicitly DECLARED, not independently verified by the driver.

Output preserves minute sample maxima/last owner lists/sample counts, global
maximum vs 16, LNKS/MNDR detection counts, outcomes, input SHA and declared head.
V2 also exposes desired-owner and active-path maxima plus per-minute refusals
from warning_cap_full/warning_owner_cap_full. Integer arithmetic keeps the final
ns of a minute in that minute. Every declared minute is included; missing samples
are null/UNKNOWN and no samples yield maximum=null, not a fabricated zero.
Comparison reports old/new per-minute values and UNKNOWN missing-minute evidence;
no automatic acceptance. Missing whole minutes are not fabricated zeroes.
Actual historical counts and old/new results are UNKNOWN until inputs and main's
execution. Original named fills/arrival/native witnesses are not manufactured.

New test_price1_candidate_census.py has 54 intended source controls UNRUN, separate
from the original 61 entry controls. No old controls were removed/relaxed. See
PRICE1-CENSUS-V2-READINESS-20261022.md for commands and
PRICE1-CENSUS-V3-CLOCK-CONTRACT-20261022.md for the latest clock/input limitations.
Additional test_price1_native_exits.py authors12 real-adapter/fake-transport SELL
controls, PM/RTH x both brokers x all3 strategies. Total127 intended UNRUN.
PRICE1-SELL-P2-SELECTION-20261022.md records exact existing performance selections
and their coverage limits; old head measurements are not borrowed.
