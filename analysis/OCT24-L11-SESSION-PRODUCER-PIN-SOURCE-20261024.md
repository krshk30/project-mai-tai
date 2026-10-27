# L11 session/minute producer pin: source checkpoint, NOT qualification

Owner Galileo. C1 2026-10-24 17:09 authorizes this bounded source increment.
Branch `codex/oct24-l11-session-producer-pin`; worktree
`/private/tmp/oct24-l11-session-producer-pin-20261024`.
Parent `c90f17d0c5d0a7df21384ea7a6f03aac323b68d6` includes the separately reviewed
compatibility fixes. The Git commit containing this document is the source head;
its actual commit time is authoritative. No earlier parent is called the fix.

## Scope and call graph

Nine APP paths, two NEW test modules, this handoff. No strategy-core entry-rule,
OMS, SQL, settings/catalog, L12/L14, permission, native-book or provider-limit
change. Main owns integration and ALL execution. No author tests, imports, AST,
compile, APP, PG, provider, SSH, replay or timing were run.

1. Native Schwab streamer/REST parsers and Massive seed/session parser attach an
   explicit producer identity. A generic `rest` transport is NOT Schwab proof.
2. `_enqueue_keepwatch_bar` accepts a closed candidate into the session pin and
   invalidates the existing ledger CAS in memory before its owner awaits. No
   receiver provider/DB/log I/O is added. Existing inbox500,180000ms, cadence,
   epoch, freshness and shutdown ownership guards remain.
3. Owner/dispatcher and `SessionLineRestoration.observe` use one pinned OHLCV
   candle per session/minute. Both latest immutable producer candidates and
   per-producer revision counts remain separate. First producer wins; its later
   revisions still update. Other-producer revisions cannot rewrite the line or
   liquidity value. First simultaneous full-response acceptance retains the old
   Schwab tie priority; an earlier ingress pin wins over that response.
4. `read_keepwatch_history` retains both parsed response sets and raw hashes.
   After existing response-clock/epoch/state checks, `project_history` checks
   IDs, parsed fingerprints and metadata BEFORE mutating pins. It projects the
   response for prepare/history polling without rewriting the raw merge.
5. Restoration/rebuild still uses the existing HLC line-dependency comparator,
   reset/epoch/shape cancellation, strict liquidity floor and real owner guards.
   Same-producer close/H/L changes still revoke both waiting-order legs. No
   comparator globally ignores price revisions; no threshold is relaxed.
6. HALT/repair checks recognize only a canonical proof backed by the original
   KEEPWATCH source, and repairs must match an actual current response candidate.
   Empty-minute/trade conflict and exact spanning-pair rules remain. Pre07 seed
   handling and Massive repair when Schwab is absent remain explicit.

Memory is bounded per symbol/session to960 minute pins,1920 latest producer
candidates/revision counters and960 latest stored-row witnesses. Session rollover
clears the service pin map; fade/re-add retains it. This is NOT a new global
symbol-count bound, nor a claim of lossless infinite retention.

## Main WIP finding: raw versus derived proof

Addressed in this source, UNRUN: `_accept_line_source` verifies original complete
native-source proof, original closed IDs and original value hash before observing.
`ledger.raw_producer_coverage` retains that original immutable object. Canonical
coverage has the DISTINCT source `keepwatch_canonical_projection`, plus
`raw_source`, `raw_bars_sha256`, `raw_closed_ids`. Dual-response coverage additionally
retains Massive response-byte hash, Massive parsed-value hash, Schwab parsed-value
hash and their IDs. Its `bars_sha256` is explicitly the DERIVED projection hash.
Projected mixed values are never named an original provider response.

`prepare` refuses missing/malformed raw provenance and values not matching actual
session pins. No raw provider attestation/hash/IDs are rewritten to fit a target.

Stored DB rows have no producer attestation. Before a real producer prefix,
`stored_bar` refuses; `_reconcile_line_store` has not yet added `_line_db_checked`.
After an actual accepted prefix, the same real reconcile method can recover and
mark checked. The new controlled test checks both states; it is NOT actual boot
or DB proof. A DB-only boot without a readable producer prefix remains UNKNOWN.

## Shared ChartBar impact

`producer` is an optional `InitVar`; original seven positional arguments,
dataclass fields/equality/repr/asdict remain. Legacy untagged instances keep the
old instance dictionary. Explicitly tagged instances add `producer` to their
instance dictionary/pickle; this is a disclosed metadata serialization impact,
not a claim of byte-identical pickle. `replace` preservation is a new UNRUN
contract. Streamer, ordinary native REST, session REST and Massive seed/session
parsers attach known identities without modifying their prices, clocks or
existing volume conversions. No installed ChartBar vars/pickle consumer was
identified by static search; this does not replace Main's shared/MOM gate.

## Test inventory: all UNRUN

`tests/unit/test_keepwatch_session_producer_pin.py`:41 intended expanded controls
(manual source inventory, no collection). First producer both orders; same-source
H/L/C revisions; malformed/foreign/future/unknown candidates;960x2 bounds; both
raw responses/hole; corrupt fingerprints; stored identity UNKNOWN; immutable owned
copy; pending identity conservation; shared ChartBar contracts; real native
streamer/REST metadata; real ingress/owner covered cross-source H/L/C; actual
both-leg cancellation for same-source close/H-L; stale proof refusal; epoch/reset
revoke; immediate CAS invalidation; unknown generic REST refusal; original proof
hash check; distinct derived/raw proof; four provenance refusal variants; real
stored-row reconcile refusal/recovery; strict liquidity floor; re-add/rollover;
flagOFF legacy cancellation; pre07/joint-empty HALT/trade conflict; repaired
interval actual-candidate match.

The cancellation fixture uses the actual constructor, armed-safety ON, real
no-factory ownership UNKNOWN and real unreadable falseflip-store initializer.
Entries remain held. Its standing waiting order is an explicit cancellation-only
preimage, NOT proof of a newly admitted placement. Only the cancel observer
delegates to the actual method. No field injection pretends native readability.

`tests/unit/test_keepwatch_session_producer_chart_oracle.py`:4 separate actual-input
math gates. NOT actual case/owner/entry/native acceptance. Uses the unchanged
independent oracle, both retained producer inputs at labelled reconstructed clocks,
real pins/ledger/build_session_line. No missing candles invented, integer-volume
normalization, minute-set trimming, rounded target-derived expectation or oracle
parameter tuning. Each writes a0600 artifact before comparison assertions.

Main-only proposed selectors AFTER whole source/fence review:

```text
tests/unit/test_keepwatch_session_producer_pin.py
tests/unit/test_keepwatch_session_producer_chart_oracle.py
```

Use fresh private output/XML/audit/basetemp; existing pinned memory/SQLite fence
for controlled41, a separately reviewed exact read allowlist for actual-input4.
No PG/provider is authorized by either selector. No previous controls need repeat.

## Four independent chart-oracle bindings

Independent source (pre-fix, not APP-derived):
`/private/tmp/oct24-l11-daic-chart-oracle-20261024/src/project_mai_tai/backtest/atr_oracle.py`
SHA `22ead27b69dc89cafc75e6dc1c97238a9cd8f82d48b4e4c2665bdb4889fa60d7`.
Function `compute_atr_trail`, period5/factor3.5/seed=sma5. A delegating formatter
observer exposes its unrounded recurrence, without changing it. Comparison proposal
is the existing `test_atr_indicator_parity_vs_oracle` raw trail delta<1e-9,
state/age/flip exact (review required), NOT a new rounded tolerance.

Raw CSV root:
`/Users/velkris/.codex/task-queues/analysis/L11-MASSIVE-1M-AGGS-20261023/`.
These are real refetch data requested with `adjusted=False`, not original
HTTP receipt/availability proof. All actual sparse minutes are retained.

| Case | Independent CSV / SHA256 | Actual first..last ET | Evaluated oracle receipt |
|---|---|---|---|
| IZM10-13 | `massive_1m_2026-10-13_IZM.csv` / `ea37f613da96c556996ec8e4aac296db4e4961ba7bec426419d12afe642ba365` |04:17..19:59|MISSING: proposed new math artifact, UNRUN|
| CLWT10-12 | `massive_1m_2026-10-12_CLWT.csv` / `153025afcfd7d97f7e3d515b83b33b561e9b660a79cfaf90fff995dc923fb8e1` |07:00..19:59|MISSING: proposed new math artifact, UNRUN|
| DAIC10-12 | `massive_1m_2026-10-12_DAIC.csv` / `e5f3631c52d0dfce091780b118d676767d0c97bd42d439efb8c8ac5f8da66bd7` |04:10..19:59|prior independent receipt below; new APP comparison UNRUN|
| LNKS10-12 | `massive_1m_2026-10-12_LNKS.csv` / `d804c3268a5a563851ca6d4d6fce9c5037fb631e251bfd4eacfccc25c449fd82` |04:00..19:59|MISSING: proposed new math artifact, UNRUN|

Existing independent DAIC/CLRO artifact:
`/private/tmp/OCT24-L11-DAIC-CLRO-ORACLE-FIRST-20261024.json`, SHA
`d50eb48cc000d1231f0dec9eeb75722efac6200cea444b21e49343abfc8cc364`.
It does NOT qualify this new APP. IZM/CLWT/LNKS evaluated artifacts do not yet
exist; no native or chart gate PASS is invented from raw file availability.

Common retained response/input packet:
`/private/tmp/OCT23-L11-C124-OLD17-INPUT.json`, SHA
`680dcd4a073f47549de3ec5eb79f2790cd30ed4341d20bff6fb85c8c52adb334`.
Exact case IDs are frozen in the new math test; IZM's other case is not substituted.
Stored provider=NULL rows are only labelled reconstructed Schwab FILE protocol,
never original native Schwab provenance. Actual recorded availability is unchanged;
only existing approved missing-clock close/+1ms reconstruction is used.

## Remaining binding and qualification gates

Frozen94/9f harness MUST refuse the new APP source until a reviewed new binding.
Its `keepwatch_weekend_unified.py` currently makes seven-field untagged bars.
New generation must attach producer metadata at the actual known FILE sender,
not infer producer from `rest`, and preserve exact raw OHLCV/clocks/source IDs,
ZERO/HALF inputs/phases/cadence/poll/query caps/cap35/security. For the literal OLD
seven-field constructor, metadata may be attached after construction by the SAME
adapter bytes; OLD must not become NEWfeatureOFF. No old receipt is retagged.

Main must explicitly review this9-path delta plus compatibility/L12/L14 composition,
updated actual-reader dual response/projection handling, raw-versus-derived
observer fields and explicit source hashes. No wildcard future APP acceptance.

Original IZM ZERO/HALF raw FAIL/UNKNOWN receipts and all earlier CLWT failures
remain. After source controls and independent chart gates, Main's NEW IZM pair
must establish actual cadence/a-e/no5s lifetimes; chart math alone cannot. Then
C1 review/release remaining30. Original Case21, boot/122, ALLON/full, shared MOM,
advancing hotpath and impact/install gates remain OPEN. No20:00 READY promised.

## Source hashes

| Path below `src/project_mai_tai/` | SHA256 |
|---|---|
|market_data/keepwatch_producer.py|6fb5bda0f1e0258cc05b85a50b40a003c74a378afbd0fa292d7f74e592af06f5|
|market_data/keepwatch_history.py|ac409fcde484dcade865576e1c8ba431222f7dd30d402d346a1328aa095ad724|
|market_data/massive_atr_seed.py|3860a52c781656fa6f47b24b10e74c9c9cf7f31c41dcfae8d4ab7ac7bcb82466|
|market_data/schwab_v2_rest_client.py|57c02d052e4a1afa121425b7c511afabfb0293d8ea572a22f6a1463fba89038c|
|market_data/schwab_v2_streamer.py|a158a86f99f89ddf3dd3dde4eba70ecae81b63393d9895d75cdd4b4639ebaa9d|
|services/keepwatch_bar_owner.py|4385d9b8a353140af4b0a61619fb1db4cf6265ceee3171893820b698e1fd3fc5|
|services/keepwatch_halt_proof.py|50d1ed12d0218e6558ce873a8e91b69151a0f64ea9343b6c2e3552660c95f04e|
|services/schwab_1m_v2_bot.py|6e7cb1d5dfeba487974c7710fce3ff58bf9715d41ced92636571e73504a0d8b0|
|strategy_core/session_line_restore.py|3b953c9248d9b2dd46800bcece1c0191ab0556367265bffefe0bf590ec1ed62e|

Test SHAs: producer contracts
`4469a3c2271b8ea3ac5dff1a28275a67743f1d8159eceb0c12ec12a961bd11cf`;
four math gates
`932c6fc828739c7a784059479b082364e4ea4123547eed2ca5ab6b640ef26095`.
Static diff inspection/whitespace checking is not a runtime PASS. No shared
queue/status/LATEST, frozen APP/harness, auxcopy tool or production writes.
