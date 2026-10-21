# ORBPASTLEVEL1 Input Contract / Author-Only

Status: UNRUN. Main assessment b5426b4c accepted by Claude13:37:20. This
contract confers no collection, execution, order, install or tonight authority.
Main requests the existing authorized exporter; worker opens no connections.

## Minimal Existing Export

Export the original 11 plans since 2026-10-01, including every initial decision,
reprice, refusal, cancel, partial, late execution and missing capture. Reviewer
names are an expected reconciliation list, not independently verified results:
MI, JAGX, FLYE, GIXI, NXL1001, XHG1006, BIYA1007, AIXI1008, OLOX1021,
SXTC and AMOD1002. Supply full actual session dates/IDs, not inferred dates for
the four filled names. Do not filter to successful preview or broker orders.

Only bounded per-plan captures are needed, not a full-market tape. Exporter
must declare each actual capture start/end and its coverage, requested decision
window and actual exit/cancel horizon. Author has not chosen an arbitrary
post-entry window. Missing windows, quotes or histories remain explicit UNKNOWN.

Packet directory has manifest.json plus these UTF-8 CSVs. Empty unavailable
files still have headers, a coverage reason and a real file hash.

- decisions.csv: decision_id,plan_id,session_date,symbol,action,decision_at,
  level,quote_id,observed_order_type,observed_limit_price,capture_start,
  capture_end,capture_complete. One row per actual attempted decision, including
  above-cap/no-parent/refused/unknown. Timestamps include timezone. No invented
  quote_id where the exact consumed quote is not retained.
- quotes.csv: quote_id,plan_id,symbol,producer_at,available_at,ask,bid,
  source_event_id,source_sequence,source_timestamp,received_provenance. producer_at
  is the field OMS actually stores as received_at (currently event time); it is
  NOT local consumption arrival. available_at is documented actual OMS
  consumption availability; absent is blank/UNKNOWN, never backfilled from SIP.
- broker_events.csv: plan_id,decision_id,intent_id,client_order_id,
  broker_order_id,parent_order_id,replacement_old_id,replacement_new_id,
  native_status,event_type,event_source,filled_quantity,remaining_quantity,
  fill_id,fill_price,event_at,observed_at,raw_record_reference. Preserve every
  partial/terminal/unknown and exact OTOCO parent/child association. Confirmed
  native fills are only actual broker execution reports, never tape touches.
- trades.csv: trade_id,plan_id,symbol,event_at,received_at,price,size,provider,
  conditions,eligible,eligibility_rule,correction_of,cancel_of,sequence,
  raw_record_reference. eligible is true/false/UNKNOWN under the declared
  provider rules. Revisions/cancels must be resolved under an independently
  declared contract or remain UNKNOWN. No invented eligibility, identity or
  arrival times. Historical flat-file SIP supports a separately declared
  event-time study, not a claim of local as-known delivery or broker execution.

Also export existing bounded state records (JSON/JSONL is fine) needed for
controlled full OMS replay, separately from the price-only driver:

- Full original intent envelopes, IDs, source service, produced_at, control
  generation/time, deferred MACD bar proof and country selection provenance.
- All strategy/account/symbol same-session intents and orders, including rejected
  previews without BrokerOrder, durable refusal origin/code/raw preview, pending
  and MAY_HAVE_SENT permissions. No-parent alone is not an exposure census.
- Native order IDs, exact terminal zero-fill or partial allocation reports,
  cancelled/replaced chains, possible dispatch uncertainty, owner reservations,
  account and virtual positions, managed positions, unresolved protective pairs,
  one-name/day attempt records and exact client identity. Absent evidence cannot
  be treated as no orders, no fills, or an unused budget.
- Actual effective settings and clock/session, control, risk, country/owner,
  MACD, ATR and body decisions with input row IDs/source timestamps. Include
  available submit/preview/control/token/transport boundary timings and quote
  changes during those awaits. Missing clocks stay UNKNOWN.
- Capture coverage for every plan/decision, omitted dates, log retention and
  existing exporter query bounds. Explicitly say whether the original11 is a
  complete census, and enumerate every missing item instead of dropping it.

Exact missing-input question to exporter/main: Can the existing export supply
the full 11-plan identity/decision census, exact consumed quote plus availability
at each preview/wire boundary, original preview/refusal/possible-wire histories,
native IDs/partial allocations, and bounded capture windows through actual exit
or last cancellation? For each unavailable field, return its provenance and
UNKNOWN reason. Do not acquire a new provider connection for this worker.

## Manifest And Frozen Interpretation

manifest.json schema is ORBPASTLEVEL1-input-v1. Required keys:

- files: decisions,quotes,broker_events,trades each mapping path and sha256.
- plans: list of full plan_id/session_date/symbol and decision_ids; missing
  decisions are still declared even if no CSV row can be supplied.
- complete_original11_census: boolean, with source/query/coverage receipts.
- declared_time_bases: strict-arrival and/or declared-event-time-study.
- quote_max_age_ms: actual settings value (currently source setting defaults
  2000ms); never tune to obtain a preferred result.
- candidate_source_sha256: every changed src file plus replay driver. Main pins
  the reviewed candidate hashes before running; installed/base Git object
  inventory below is the source comparator, not a substitute for these hashes.
- capture_contract, eligibility_contract, missing_inputs, exporter_identity and
  exported_at: explicit provenance, coverage and unresolved limitations.

Frozen v1 policy: rounded-trigger anchored +5%/-8% exits; nominal level*1.005
rounded UP, floor trigger plus one broker tick; tick is .01 above $1 and .0001
at/below $1, with floor at exactly $1 stepping to $1.01. Ask rounds UP.
Raw ask below trigger selects STOP_LIMIT; ask at/above trigger with rounded ask
at/below effective cap selects LIMIT. Above cap/missing/stale/future quote means
no new send. Age bound inclusive; a newer fresh quote can reaffirm exactly the
previewed wire type/prices, not authorize an unpreviewed changed price/type.
No hidden price retry or automatic 401 retry on fenced sends.

Local-zero continuation requires explicit new-code receipt, rejected/client_abort,
same root plan/route/account/day, strictly higher new decision, no redelivery,
and no prior order. Any native order consumes the unchanged attempt budget,
even if cancelled zero. Historical receipt absence, uncertain dispatch, unknown
fill/clock or filled plan blocks. No invented historical local-zero receipts.
There is no automatic quote-tick retry: an above-cap plan remains available for
the next explicit higher strategy reprice, within unchanged entry windows.

Installed object 1e1a12b3641e3d55c680bda2c70c61c66b7d466f and exact base
556775899b1014db284afbd022cfe26fd4dec14a have identical relevant blobs:

- oms/service.py f8b9123e7cd18815a02161ce76a37685aedb4424.
- broker_adapters/schwab.py 0131bbe5993e34f8c352fd5ebb7779bba4232dab.
- orb_schwab_order_route.py e7640e992e8bc26265b251fb722abe69bb10f341.
- strategy_core/orb_schwab_bracket.py 9b9629da0de5f535f4c124acc7fdedbf7ed9bc71.

## Driver Boundary / Main-Only Commands

tests/unit/test_orb_pastlevel_replay.py calls the candidate's real select_entry,
hash-checks input/source files, retains all declared missing decisions, compares
actual old wire type/limit to candidate selections and returns actual broker
events unmodified. Conditional target/protective tape touches assume an entry;
they are not evidence an entry/exit filled. Same-time opposing touches remain
ambiguous. No estimated native fill, PnL, win count or production proof.

This driver is intentionally NOT a full OMS/lifecycle replay. Quote selection
alone cannot prove gates, absence of another parent, late fills, replacements,
preview acceptance or wire scheduling. Focused authored tests exercise the real
OMS path under explicit fixtures. Original11 full state replay remains blocked
on actual snapshots and a separately reviewed offline fixture adapter. This is
an explicit remaining gate, not a silent claim that the price driver covers it.

After main reviews exact head, commands, readiness, embargo and durable claim,
the following are PROPOSALS ONLY (worker has run none):

```sh
cd /private/tmp/mai-tai-orbpastlevel1-20261021
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q tests/unit/test_orb_pastlevel.py tests/unit/test_orb_schwab_bracket_plan.py tests/unit/test_orb_schwab_order_route.py tests/unit/test_orb_schwab_replace_adapter.py tests/unit/test_orbroute1_country_contract.py
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python tests/unit/test_orb_pastlevel_replay.py /ABSOLUTE/LANDED/ORB11-PACKET --time-basis strict-arrival
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python tests/unit/test_orb_pastlevel_replay.py /ABSOLUTE/LANDED/ORB11-PACKET --time-basis declared-event-time-study
```

No command may install dependencies or contact a provider. The existing Python
symlink was inventoried, NOT invoked; environment readiness remains main-owned. The
packet path is deliberately unresolved until real inputs land. Main-only
source/Git comparison can inspect the four exact blobs above without app import.

Cost expectation, UNMEASURED: focused local fixture suite, no provider/DB service;
driver memory O(packet bytes) and work O(decisions*bounded trades + file bytes),
not a latency benchmark. File and row counts must be reviewed before execution.
No full-market 48GB requirement and no worker replay/processing loop executed.
Native bracket acceptance, rate/tick/stall, final all-on composition, original11
full replay and reviewer gates remain mandatory before any install assessment.
