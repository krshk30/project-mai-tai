# October 7 Event-Driven LINESRC1 Build

[codex] AGREE within the narrowed human card. Old full-session polling proofs
are SUPERSEDED. Draft PR1107 is not ready, pinned, installed or activated.
Base: 5b8b4f642bbc3c312be436d0e92adbc22d9e9f95.

## Frozen Source Contract

Full-session GETs are serialized, coalesced one-shot scanner repair/startup
events, not clock or callback retries. Initial membership with no actual
closed candle waits. First eligible actual closed candle >=07:00 triggers
startup once; earliest availability is 07:01. Real confirmed_at, not snapshot
publication/quotes, identifies subsequent re-confirmation. No-hole re-add
does not fetch. A stale re-add endpoint keeps the unused event until an actual
bar reveals whether repair is needed. Ordinary incremental REST delivery stays.

The provider seed remains immutable; consecutive validated closed live bars
form separate suffix provenance. Healthy suffix appends advance unchanged ATR
mathematics without additional full-session GETs or whole-session DB reads.
Provider-prefix conflicts revoke admission, never authorize unsolicited GETs.
Matching provider-listed DB rows may repair the original hash; new IDs/changed
prefix values wait for a qualifying event. Missing tail/R6 positive tape stay
closed; ten later arrivals cannot manufacture missing provenance.

Strict once-empty limitation: an empty/missing-current event stays waiting,
without callback/worker retry, until another qualifying scanner event.
Flag OFF, ordinary quotes/seeds, entry/hold/ATR rules and ownership remain
unchanged. No environment default or production activation is performed.

Service SHA256:
44980d471b497856ad4d841437e5e9fd0d9ac83d49939619e0712bc5642617e0

Ledger SHA256:
823ca0c51c8239fffa89a20f1bd5325bd0fd9d6fc98cdc1ebfb6795d2f2a0f5c

## Controls And Preservation

Own pre-fix review regressions: 7 failed, zero errors, retained at
/tmp/linesrc1-event-review-red.{xml,log}. Four causes reproduced: premature
DB/tape completion, worker-before-ingest lost wake, suffix correction wrongly
invalidating provider seed, and initial membership consuming a repair before
the first actual candle. Fixes preserve pending stale reconciliation, wake
after real strategy ingest, distinguish prefix/suffix, and wait initial close.

Final focused: 385 passed, zero errors, 12.54s at
/tmp/linesrc1-event-final-focused.{xml,log}. Includes eight review variants,
29 event controls, provider parsing, R6, restoration, bot and confirmation exit.
Final mutations: 28/28 semantic assertion kills, zero harness errors, at
/tmp/linesrc1-event-final-mutations.{json,log}. Earlier run had 27/28 semantic kills:
the DB-tail test returned at a stale endpoint before testing its claimed
behavior. The final test explicitly observes the same replay tail in the
ledger so it exercises reconciliation without pretending DB is live source.
No source change was made to obtain that mutation kill.

Independent parent exact current-byte focused receipt: 29 passed (1.57s) plus
8 review variants passed (0.95s); before/after source hashes stable. Paths:
/tmp/linesrc1-event-parent-focused-20261007.{xml,log} and
/tmp/linesrc1-event-parent-review-20261007.{xml,log}. These are not full-suite
proof. Earlier independent exact persistence-pause controls observed recorded
RETO 11:25 SELL close 2.06: zero before/during pause, one after recovery,
still one after duplicate wake, no second full-session GET.

Unchanged 123-case runner/factory and retained price inputs:
/tmp/linesrc1-event-final-123.json SHA256
b3104ee6525171e5ae427f4a8c806b2925e8586166244b68d2aa0851122a7f81,
byte-identical to the preservation baseline. 90 MATCH/33 HELD; plus 1098
MATCH/25 not applicable; zero incomplete entries. This is not all-admitted.
Four actual 06:39 empty Schwab envelopes remain retained unchanged; no later
four-name price/recovery receipts are invented. Counterfactual timestamp,
withheld-row, tape and volume controls are labeled, not raw market receipts.

## Full Pair And CI Gates

Parent TODAY baseline on verified identical main5b8 tree b28df7b3:
6931 passed /56 failed /0 errors, 332.71s. XML
/tmp/linesrc1-event-base-5b8-20261007.xml SHA256
a1b679d6f061b6d8635349889ef9060a3c375602bbebd0262bc5d89fd38f59c8;
log SHA256 5bbf018e796446cc6647a9bccef0ca351ad2e2eed4f37428c4c3c38c3554b1a7.
Do not change inherited failures. Its isolated throughput control passed
1/1 (1.04s); this does not erase the full baseline failure.

Run one full unit suite after source commit freeze with actual own-worktree
import verification; compare all failed names against this XML, not old counts.
Publish exact results, source/head/import paths, artifact hashes and any flaky
removed name in the PR receipt. Both fresh Validate runs on the pushed head
are required. Full-head comparison and hosted CI are PENDING at this source
freeze checkpoint; this document does not certify a green full suite.

No production, order, service, token, Redis, ledger, handoff, merge, pin or
installation writes. Parent alone controls deployment and morning operations.
