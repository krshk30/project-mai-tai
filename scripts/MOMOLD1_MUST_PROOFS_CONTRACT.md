# MOMOLD1 MUST1-3 author checkpoint

Ruling: Claude 09:50 heading, read locally 09:40:54, accepts the narrow 52-name
reconstruction + RUMBW event-only + SYNTH coupling contract. Original full-universe
ordering is no longer a hard gate. This supersedes the earlier conditional
full-universe blocker in MOMOLD1_REVIEW_CONTRACT.md, not the remaining must-tests.
Claude 10:28:19 (read 10:29:25) cleared the UI blocker and accepted main's rendered
LIVE board/canonical section ordering. Main also reports the bounded 52-name
reconstruction MATCH (20 records / 5 DETECTED each; report fbf7b0db). These receipts
do not establish original full-feed parity or this newly authored MUST1 result.

Lane C changed no source or existing retirement tests for this follow-up. Every
new test/harness here is UNRUN: no imports, tests, replay, render, provider, network,
SQL, orders or service action. Queue/LATEST remain main-owned. MUST4 is Claude's
external after-restart guard/A ON/CPU/GUARDPIN1 proof; this lane cannot mark it PASS.

## MUST1: exact retained offer calls

Source inspection: the inclusive text block from def _offer_live_signals through
the following _start_country_router signature is identical in 99fc and b403:
SHA256 b2c6d4ffa99cdbf69bb06a61c92e0444a4527c88cc36eb034bb342b56804b7e1.
This is a static text receipt, not an executed AST or trading acceptance result.

momold1_live_offer_proof.py compiles the extracted service method AST from git's
99fc bytes and the current source file, without importing either service module.
It records exact method and AST hashes, literal cache-staleness constants and
byte-equal JSON offer calls: symbol, strategy, trigger, ns, logical ID, retained
payload hash and forwarded volume. Inputs are all retained warning DETECTED rows
from the hashed October 20 CSV, plus both delivered AMOD October 21 08:15 rows.
tests/acceptance/test_momold1_retained_acceptance.py preserves every
actual assertion, including comparison to the current bound service method.
Portable unit controls use explicitly SYNTH records and controlled source copies;
they do not require an untracked export or the pinned historical git object.

Observation time is the retained sealed observed_decision_ns; no current-clock
fallback exists. Cache absent/fresh/exact-boundary/stale/future cases are explicitly
controlled. October 20 volume 123456 remains a SYNTH cache control, not an observed
market volume. AMOD uses actual log value 15361 with the same five controlled
cache contexts; the original cache observation timestamp is UNKNOWN.

**Delivered packet:** analysis/MOMOLD1-EXPORT-AMOD-1021/events_amod_1021.csv has
nine rows, including two sealed 30/60 warning DETECTED records at observed ns
1792584906753471330. Their qualifying signal price is 1.57; the earlier warning
ADD aggregate-high trigger 1.42 is a different field and is not substituted.
SHA256SUMS (80146ce21aab333ed9088efe34348278cea20407895675b822d207ab25d4111f)
pins exactly three files: events 1ca01bf7c6987cc634f04e37553df3b97da8e8f87453edbad32d55ea5c320490,
quotes 2238484af91944f74f6cf532ddae0925ac8857722667f55ab37bd6f98612bd8c,
trades 664f28607dc8a456e10545ac58c60a027f3383c2e79795e0757a61a85809756f.
amod_live_log_lines.txt is OUTSIDE that three-file manifest; its separately pinned
SHA is e34955bd5f28f28a04b402e98da8400d98505ded5fc17a4fc1d39f32cf49d46b.
The harness verifies every digest and the nine-row census before comparison.

AMOD uses the actual LiveHandoff class (source class name, not a stub) and real
EpisodeLedger. Both complete source files handoff.py/admission.py must be byte
equal in 99fc and current source; hashes/equality enter the report and pass gate.
The route callback supplies recorded schwab / SEC_US / READY / as_of context;
it does not recompute SEC policy or contact a provider. is_us boolean and CIK
were not retained in this log, so they are not invented. A controlled empty,
restored-ready ledger is used; historical owner-book state is UNKNOWN.

The recording wrapper delegates to real offer()/Signal.validate()/claim(). It
does not manufacture a refusal return. Actual 15361 fresh/exact-boundary cases
must return day_volume_below_50000 for both records, matching the two retained
refusal lines; absent/stale/future cache cases must return day_volume_unknown.
Every PRE/POST offer/result byte string must match, with zero queued emissions
and zero claimed episodes. No handoff worker, Redis IO or native order runs.
Native acceptance, original cache clock/freshness and exact handoff callback
clock remain UNKNOWN. This is a pure offer/refusal result, not transport/native
parity. A SYNTH 50000 positive control and volume-forwarding mutation control
ensure the real refusal logic is tested rather than a mocked return.

## MUST2: independent LIVE subscription call graph

All paths below are relative to src/project_mai_tai. These files are unchanged by
MOMOLD retirement; lines refer to the inspected b403 source except main-owned UI.

| Edge | Source | Evidence |
| --- | --- | --- |
| active working/held/unknown -> owner proof | momentum_live/admission.py:12; owner_proof.py:34-49 | ACTIVE_STATES determines owned episodes; proof uses their symbols, not paper candidates. |
| independent OMS owner replacement | owner_proof.py:57-66, 113-130; runtime.py:451-453 | replace_quote_owner publishes consumer momentum-live through the shared subscription stream. |
| gateway regular-owner union | market_data/gateway.py:515-528 | Only momentum-warning is excluded from regular owners; momentum-live remains regular. |
| regular owner -> physical T and Q | market_data/massive_provider.py:477-500 | sync_subscriptions uses regular union and warning union for T; Q uses regular union. |
| warning replacement cannot remove LIVE T | market_data/massive_provider.py:432-444 | new_trades = regular subscriptions UNION desired warning trades. |
| LIVE quote dispatch independent of paper engine | oms/service.py:12791-12793; momentum_live/actor.py:256-258 | OMS quote path dispatches to the LIVE actor; its owned-position exit reads bid/quote clocks. |
| OMS trade safety path | oms/service.py:12847-12855 | Dedicated trade handler updates trade cache and safety evaluation independently. |

test_momold1_live_proofs.py uses a restored SYNTH ledger's working and held states,
real OwnerProofPublisher + replace_quote_owner, a dormant paper engine, real
gateway subscription handling and MassiveTradeStream with a recording socket.
Clearing both paper owners must keep T.AMOD and Q.AMOD through momentum-live;
terminalizing that LIVE episode is the positive release control. No warning
absence or subscription request is represented as provider ACK/coverage.

## MUST3: durable readers and copied-table protocol

Core call graph (registration removal does not delete durable rows):

| Reader/writer | Source | Treatment |
| --- | --- | --- |
| seed -> ensure_strategy | runtime_seed.py:32-53 -> oms/store.py:216-247 | Upserts by unique code. Legacy rows/UUIDs remain; warnings get separate paper rows. |
| OMS constructor / intent lookup | oms/service.py:875,2080-2091,15055 | Config map lookup is optional; unknown historical code retains fallback metadata. No generated legacy signals are introduced. |
| strategy state / DB restore | services/strategy_engine_app.py:4157,10355-10472 | Registered-code filter; missing runtime is explicitly skipped. Warning rows do not create a generic trading runtime. |
| control DB / configured views | services/control_plane.py:_load_database_state / _build_bot_views (b403 2783 / 1892) | Reads all Strategy IDs for history; active view membership is configured. Main owns final LIVE-board selection. |
| reconciliation lookups | reconciliation/service.py:341,701,747 | Reads all Strategy IDs; position checks scope live active accounts. New paper rows alone are not positions/orders. |
| LIVE persistence/ledger | momentum_live/persistence.py:30-53; runtime.py:363; ledger.py:32-33 | Explicit momentum_warn_live identity remains separate. |
| control lifecycle | oms/bot_control_runtime.py:77-78 | Live execution mode AND controlled strategy allowlist; paper warning rows do not become controlled LIVE bots. |

Broader reader inventory from both Strategy selection/join and primary-key get
searches (each path is a durable-row reader, not necessarily a boot entry point):

- ai_trade_coach/repository.py; owned_inventory.py; paper_exit_store.py.
- falseflip1_runtime.py; v2_falseflip_release.py; v2_removed_wait.py.
- orb_country_route.py; orb_schwab_exits.py; services/schwab_1m_v2_bot.py.
- services/bot_control_routes.py; services/control_plane.py; services/strategy_engine_app.py.
- momentum_live/runtime.py; momentum_live/persistence.py; momentum_live/ledger.py.
- oms/service.py; oms/store.py; oms/route_owner.py; oms/orb_country.py.
- oms/orb_schwab_eod.py; oms/orb_protection_work.py; oms/managed_sell_prewire.py; oms/managed_sell_retirement.py.
- oms/atr_reprice_runtime.py; oms/atr_reprice_handoff.py; oms/eh_fresh_price.py.
- oms/webull_owned_positions.py; oms/schwab_owned_positions.py; oms/webull_attach_riskoff.py.
- oms/webull_identity_copy_audit.py; oms/mirror_retained_hold.py.
- oms/bot_control.py; oms/bot_control_runtime.py; oms/bot_control_journal.py; oms/bot_control_legacy.py; oms/bot_control_atr.py; oms/bot_control_orb.py.
- reconciliation/service.py; reconciliation/working_lifetime.py.
- Historical raw-SQL readers: backtest/data.py:239,254; backtest/retry_one.py:585; backtest/replay.py:1151. These are not startup tasks.

runtime_registry.py reads settings rather than durable Strategy rows; runtime_seed
is the corresponding writer. db/models.py and db/__init__.py declare/re-export
the model. Strategy mentions in settings.py, strategy_core/schwab_1m_v2.py and
momentum_live/__init__.py are not additional durable-row queries. No raw FROM/JOIN
strategies reader in src outside the historical backtest paths above was found.

**Actual-copy export request:** a read-consistent, referentially complete SQLite
copy of tonight's application tables after the final approved source composition,
with original UUIDs/JSON/nullable fields and explicit counts even for empty tables.
Do not remap old IDs fefc7ab9/a7c5e3ec or synthesize missing rows. The loader requires
every table declared by Base.metadata, so an absent table cannot silently become
an empty fixture. Preserve strategies, broker_accounts, trade_intents,
broker_orders/events, fills, virtual/account/managed positions, control state/
commands/leases, dashboard snapshots, strategy history, Momentum events, and all
remaining application tables plus FK closure. A reviewer-owned PostgreSQL-to-SQLite
conversion receipt must disclose type transformations; SQLite is not PG SQL/lock proof.

Manifest JSON requires: kind=ACTUAL_TONIGHT_COPY, coverage=COMPLETE_ALL_APPLICATION_TABLES,
sqlite_sha256, captured_at_utc (Oct21 after 16:00 ET), next_boot_utc (following ET
date), table_rows (exact ORM table-name -> count), effective_settings_redacted
(explicit momentum flags and every effective non-secret setting relevant to boot).
No credential or live endpoint values are accepted. Producer/export receipt and
hashes are external inputs, not minted by the harness.

momold1_copy_boot_proof.py copies the supplied file again into a temporary folder,
checks schema/counts/FKs, then exercises OMS seed, strategy restore, control DB
load/view building and a reconciliation cycle against that second copy. It fails
on row-conversion errors and caught reader ERROR logs. Legacy row preimages must
remain exact; warning rows must exist; seeding cannot create a paper:momentum
broker account. Existing paper broker rows, if any, are retained rather than deleted.
Existing reconciliation findings are reported, not erased to make the run green.

SYNTH fixture tests use UUIDs 000...001/002 explicitly labeled synthetic. They
cannot satisfy tonight-copy acceptance. The actual-copy test in the separate
acceptance module fails explicitly if the copy/manifest is missing. Unit tests
also verify missing-input refusal using controlled temporary paths, without
depending on an actual export. This protocol covers selected local SQLite boot
readers only: Redis restore, actual LIVE actor boot, PostgreSQL dialect behavior,
external provider health and production boot remain separate acceptance evidence.

## Proposed main-only commands after full slot release (all UNRUN)

Workdir /private/tmp/mai-tai-momold1-20261021; paths below are requested inputs,
not assertions that those files exist. Choose fresh receipt filenames.

```sh
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -vv --tb=long tests/unit/test_momold1_live_proofs.py tests/unit/test_momold1_copy_boot_proofs.py tests/unit/test_momold1_live_dashboard.py tests/unit/test_momold1_retirement.py tests/unit/test_momentum_paper_service.py
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -vv --tb=long tests/acceptance/test_momold1_retained_acceptance.py
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python scripts/momold1_live_offer_proof.py --amod /Users/velkris/.codex/task-queues/analysis/MOMOLD1-EXPORT-AMOD-1021/events_amod_1021.csv --output /Users/velkris/.codex/task-queues/analysis/MOMOLD1-MUST1-1020-AMOD-OFFERS-REFUSAL-20261021.json
env -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python scripts/momold1_copy_boot_proof.py --copy /Users/velkris/.codex/task-queues/analysis/MOMOLD1-TONIGHT-COPY/retained.sqlite --manifest /Users/velkris/.codex/task-queues/analysis/MOMOLD1-TONIGHT-COPY/manifest.json --output /Users/velkris/.codex/task-queues/analysis/MOMOLD1-MUST3-COPY-BOOT-20261021.json
```

The standalone MUST1 command produces the actual October 20 + AMOD comparison
and real low-volume-refusal report WITHOUT requiring tonight's table copy. It
does not run or satisfy MUST3. Output must use a fresh file. Nonzero is returned
for any method/component/call/refusal mismatch; missing or corrupt inputs fail.

Run the portable unit command in main's serial slot. Run the whole
mandatory acceptance module after tonight-copy delivery (AMOD is now delivered);
no filters, skips or unit-focused pass can stand in for its three actual gates.
This relocation changes resource placement, not mandatory MUST1/MUST3 scope.
Repository testpaths=["tests"] still discovers acceptance in an unqualified full
run; it intentionally fails closed without inputs. Do not claim tests/unit is
the entire repository's acceptance or add a new exclusion to an approved full run.

Set MAI_TAI_MOMOLD1_1020_EXPORT (optional full CSV path override),
MAI_TAI_MOMOLD1_AMOD_EXPORT (optional delivered AMOD CSV override), MAI_TAI_MOMOLD1_TONIGHT_COPY and
MAI_TAI_MOMOLD1_TONIGHT_MANIFEST to the delivered files for the actual-input tests.
Missing required inputs intentionally fail their must-gates; no skips or SYNTH
substitution clear them. Main reviews differences/results and final composition.
Tonight install still requires the dashboard correction, MUST1-3 and Claude's
MUST4; this authorship checkpoint is not an install-ready claim.
