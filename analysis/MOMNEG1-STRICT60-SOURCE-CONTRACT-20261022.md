# MOMNEG1 strict60 source-only successor

## P2 scope correction: synthetic rate mode, no historical prerequisite

Main/operator explicitly corrected the original c5b9 source proposal: P2 OLD vs
NEW latency does NOT require historical records. That proposed prerequisite below
is superseded for P2, not a policy gate. Actual P1 remains UNKNOWN without actual
historical prior-close evidence. The strict measured loop/gates are unchanged.

Explicit `MOMNEG1_RATE_MODE=SYNTHETIC_RATE_ONLY` loads the committed dataset
tests/fixtures/momneg1_synthetic_rate_only.json, SHA256
98c0aa033cae510d6b2692844e649ed5591510a5b209fdf4cded9e40224592d4.
The driver checks that hash. It expands 24 disjoint controlled lanes: PM/RTH,
US/Schwab and non-US/unknown-country Webull, down/up/equal/unknown prior close.
All prices >=1 to isolate MOMNEG1 from PRICE1; names are synthetic RATEAA..RATEAX,
not FUFU/EYPT/CRMT/GIXI. Both warning strategies have12 lanes. No historical
claim/provenance/source-head is fabricated. JSON output explicitly labels
SYNTHETIC_RATE_ONLY / NOT_P1 / NOT_GIXI / NOT_NATIVE_PROOF.

No real packet/environment/hash is consulted in synthetic mode. The SAME source
driver AND exact dataset bytes must be present in oldaa162668 and new checkout.
Both retain actual outcomes, queue receipts/routing, normal GC/logging/capture,
240/s60s, >=200/s and strict handler+stall<50ms. No legacy assertion was weakened.
The identical e001 microdriver remains separate. Newly authored two fast shape/
hash-negative controls do not substitute for strict60 and are also UNRUN.

Main's paired serial command (fresh OLD and NEW outputs separately):

```sh
MOMNEG1_RATE_MODE=SYNTHETIC_RATE_ONLY \
MOMNEG1_NATIVE_RESULT=/ABS/FRESH-OLD-OR-NEW.json \
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python \
  -m pytest -vv -s -o addopts= \
  tests/unit/test_momneg1_signal_native_rate.py \
  --junitxml=/ABS/FRESH-OLD-OR-NEW.xml
```

The prior strict retained-packet mode remains available only by explicit
`MOMNEG1_RATE_MODE=ACTUAL_PACKET_RATE`; its required packet environment and
fail-closed validations below remain unchanged. It is also a rate control, not
itself a tape replay/P1 acceptance. Do not silently fall back between modes.

## Original retained-input variant (not a P2 prerequisite)

Owner Cicero. Parent e001cd3cc1c2fa2cb2ad8ee52abecae40984e338.
No production changes. No worker execution/import/test/AST/provider/DB/SSH.

Main requested the original strict60 control, not only a 1000-call microbenchmark.
Read legacy tests/unit/test_momneg1.py at1274455bc969: its Signal.prior_close
and reason labels do not match690/e001. It was NOT copied, imported or executed.

New driver tests/unit/test_momneg1_signal_native_rate.py invokes the actual
MomentumPaperService._offer_live_signals -> LiveHandoff.offer -> actual
select_route -> EpisodeLedger.claim -> bounded asyncio queue. Controlled
country/session input is pinned by the packet. No Redis emission worker/OMS
actor/provider/network executes; any unexpected Redis call hard-fails.
This is an offline actual signal callback control, not live broker latency.

Five independent case lanes retain one stable source identity each; repeated
valid offers exercise existing duplicate/one-episode behavior. New refusals
retain no claimed episode or queued signal. At every offer, actual queued
symbol/price/route is checked; routing counts, episode/source/queue receipts and
actual handoff reason counts are exported. Outcomes are NOT hardcoded to new
reason labels, so the IDENTICAL file runs on oldaa162668 and newe001+successor.
P1 policy assertions remain in test_momneg1_live_admission.py. No old function is
reimplemented, dynamically rewritten or monkeypatched for the paired comparison.

14,400 scheduled offers at240/s, real60seconds, >=200actual offers/s,
max actual callback handler<50ms AND independent1ms-loop-watchdog stall<50ms.
All schedule delays/handler/stall samples retained without trimming. No GC,
logging, capture, perf/monotonic clock or handler alteration. Driver construction,
packet reads/hashes and receipt export are outside measured callback; scheduling,
record preparation and receipt checks remain inside total elapsed/stall window.
Original historical signal/prior prices remain unchanged; retained decision
clocks advance by actual elapsed monotonic time for this repeated load control.
That disclosed clock mapping is NOT an actual tape replay or native attestation.

## Exact missing input / main-owned packet contract

No reviewed MOMNEG packet path/SHA is presently supplied. Driver FAILS (not skips)
without one. Do not feed L11/L12 ATR exports as momentum signal evidence.
Main/Claude must pin an existing retained export with all five true case records,
prior-closes memory/reference evidence, real eligible cached day volumes and
country/session decisions. Historical clocks must be honest; if actual signal
minute/price differs from the reviewer fill summary, stop for source disposition,
not edit the evidence or quietly relabel fill-as-signal. Actual GIXI prior close
is still UNKNOWN here; no5.00 placeholder. Existing P1 synthetic fixture is NOT
this packet. GIXI up/equal prior proof is mandatory for this intended kept case.

UTF8 JSON<=64KiB, main-reviewed rawSHA256, required shape:

```json
{
  "kind": "momneg1_retained_signal_prior_closes_v1",
  "source_head": "FULL_ACTUAL_PRODUCER_40HEX",
  "provenance": "actual export source, clock/asof, retained input hashes",
  "prior_closes": {"FUFU": "ACTUAL", "EYPT": "ACTUAL", "CRMT": "ACTUAL", "GIXI": "ACTUAL"},
  "cases": [{
    "case_id": "FUFU-1016-1544",
    "signal_evidence_ref": "actual retained record reference",
    "prior_close_evidence_ref": "actual retained prior-close reference",
    "day_volume": "ACTUAL_INTEGER_NOT_THIS_PLACEHOLDER",
    "is_us": "ACTUAL_BOOL_OR_NULL_NOT_THIS_PLACEHOLDER",
    "market_open": true,
    "broker": "ACTUAL_schwab_OR_webull",
    "record": {
      "event_key": "ACTUAL", "logical_id": "ACTUAL",
      "strategy_code": "momentum_30s_warn", "event_type": "DETECTED",
      "session_date": "2026-10-16", "symbol": "FUFU",
      "observed_at": "ACTUAL_AWARE_TIMESTAMP",
      "payload": "COMPLETE_ORIGINAL_PAYLOAD_OBJECT"
    }
  }]
}
```

Exactly five cases: FUFU-1016-1544, EYPT-1019-0747, EYPT-1019-0749,
CRMT-1020-1502, GIXI-1022-0819. Payload must include original detect/reference
clocks/prices, observed_decision_ns and prior_close agreeing with pinned map.
All placeholders above are a schema illustration, NEVER an executable packet.

## Main serial commands and raw outputs

Preserve identical driver SHA in OLD separate main-owned checkout ataa162668
and NEW successor checkout. Use the SAME reviewed packet and SHA. Main chooses
fresh absolute stdout/XML/JSON paths, clears unrelated app DSNs/test environment
as usual, records exact heads/claims/start/finish and inventories before launch.

```sh
MOMNEG1_NATIVE_PACKET=/ABS/ACTUAL.json \
MOMNEG1_NATIVE_PACKET_SHA256=ACTUAL_64HEX \
MOMNEG1_NATIVE_RESULT=/ABS/FRESH-OLD-OR-NEW.json \
PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python \
  -m pytest -vv -s -o addopts= \
  tests/unit/test_momneg1_signal_native_rate.py \
  --junitxml=/ABS/FRESH-OLD-OR-NEW.xml
```

One controlled old and one new claim; no worker launch/no until-green loop.
JSON uses exclusive creation, writes raw samples and actual outcomes even on
failed gates. XML/stdout summary includes timing and lane routing/queue receipts.
e001's identical3-case microdriver remains separate for old/new offer-only numbers.
Strict60+P1+actual cases+P3ALLON remain UNRUN, no source review implies PASS.

L13 independent review completed:5a25234b deployment worker lacks heartbeat
mapping/identity contract; source report already sent, no other owner's edits.
Actual122 remains frozen987checkpoint, unrun helper retained, no bulk release.
