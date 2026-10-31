# ORBCOUNTRY1: 07eb ON/OFF and ALL-ON source/control proposal

Original sole owner Cicero. Source-only; no tests/imports/runtime by author.
Authority: C1 whole20:23:14 overnight next-package release, Oct28 ET.
Base literal07ebcbaf12e76522e35e2809f0caaf43eb9b68f4; no auto-descendant binding.
Main owns all actual shared Settings/service/catalog/integration and execution.

## Portable source

Old commits075c958c09935459071f1fdc0dcdb0160cd21231,
8a26424ab2986af797408ad36a9b0add2e505d1b,
7dd703c1be0084b81a4b689fd3e7eb872680f489 are preserved, not rebased in place.
They reapply without conflict on07eb as001813b62 /01185b63a /364448893.
Rebased implementation tip364448893818a265b88acb232074837fc3e1ef66,
actual commit20:28:04 ET. New own checkout:
`/private/tmp/oct28-cicero-orbcountry1-07eb-20261028`.

Byte diff empty for old7dd vs rebased364 across orb_country.py, both ORBCOUNTRY1
test files, reused test_orbroute1_oms_chain.py and test_orbroute1_manual_net_092.py.
No Settings/service source imported into the author root. Those actual files
remain07eb. No WBEXIT2/NOCHASE/POM/SMART source adopted from Main's broadf440.

Mainf4404664aa4fc144041f346c57cc823cac789c53 shared hook source was read
literally, not followed via a moving HEAD. Its pre-hook whole-file Git blobs
match actual07eb exactly:
- service.py f5ef11e4c01c4e886e11491c3d5b250bda50cbd7
- settings.py c1f99e484e41d41c6ffc0d8c934776866ab6394d
Thus the three existing service hook hunks and one Settings hunk are portable
against this exact base; no observed source conflict. Main alone applies them.
The separately proposed patch is `ORBCOUNTRY1-07EB-MAIN-HOOKS.proposed.diff`.
No claim that those two wholef440 files are an approved combined package.

## ON/OFF call graph

Flag proposed code defaultFalse: orb_country_semantic_admission_enabled.
Future explicit installTrue only after final head/profile/review/GO.
No switch/env/catalog activation by author; no all-Settings-bools-true profile.

OFF: process_trade_intent -> original routed open gate -> fresh country load ->
four-field exact broker/reason/status/as_of. Ordinary cancels unchanged. Existing
reprice parent five-field exact comparison and successful metadata merge remain.
Missing flag on the module-only checkout defaultsFalse, not silently enabled.

ON: original event admission/source/session guard -> fresh country verification
for routed open OR cancel-encoded reprice -> broker/reason/status equality ->
unchanged risk/botcontrol/manual/owner/quantity/quote/native/wire guards.
Schwab unsent-plan continuation uses same semantics but keeps plan/day/account,
dedup/higherstop/fresh-check/risk/MACD/owner fences. Working Schwab replacement
keeps original parent stamp after confirmed SDK receipt; event/receipt keeps its
own stamp. Webull replacement keeps exact original nativeID/COID/qty/account,
naive-parent clock refusal, cancel-terminal-zero/fillarrival/owner/CAS/ACK fences.
No economic DML, fill/position reparent, source record rewrite, or new provider.
Flagged reprice adds one existing offloop CountryStore.load, not zero DB work.
No per-quote/tick loop hook. Original09:28 entry/deferred-MACD rule and
09:29..09:30:15 reprice windows stay unchanged. RTH means legal09:30:10 sample,
not permission to replace at an arbitrary time later in the session.

## Prior evidence, not a new rebase PASS

Main's first controlled58 receipt:
`/Users/velkris/.codex/task-queues/analysis/OCT28-ORBCOUNTRY1-MAIN-FIRST58-FIRST.receipt.json`
scope ORBCOUNTRY1_MAIN_FIRST58_CONTROLLED_NOT_HISTORICAL, exit0,
2026-10-28T17:17:18.278215..17:17:25.991847Z.
XML reports58 tests/0 failures/0 errors/0 skips/time6.286s. This is the prior
source/fixture execution, not a new07eb/all-ON qualification or a native replay.
Raw/provenance retained. Main decides any needed changed/combo execution only;
do not automatically rerun all58 byte-identical controls for this source rebase.

## Proposed minimal combined controls for Main review (UNAUTHORED/UNRUN)

Use the existing reviewed ORBROUTE private-DB/typed SDK fixture boundaries and
the package's reviewed real boot fixture where actual lifecycle flags require
it. No new rig, permission lambda, forced RUN, fake native receipt or timeout.
Main must pin one final combined APP/test head and an explicit approved flag
ledger BEFORE any test claim. Other owners supply their literal new settings;
currentf440 does not yet contain the completed WBEXIT2/POM/SMART integration,
so their final names/hooks must not be guessed or accepted by getattr(default).

1. ALL-NEW-OFF equivalence: preserve installed07eb profile and all old flags;
   set only exact reviewed new switchesOFF. Replay one retained controlled
   Schwab and Webull route-restamp intent through actual process_trade_intent.
   Expect old stamp refusal/no new request, same output/DB ownership; include
   no-country metadata legacy event unchanged. Logging-only items without a
   switch must prove decision equality separately, not invent a disable flag.
2. ALL-ON legal ORB: both brokers, existing09:28 PM entry and09:30:10 replace,
   actual fresh store stamp-only change. Reuse the connected fixtures' actual
   preview/prewire/replace or cancel-zero/replace consumers. Assert selected
   account/quantity, original parent UUID/COID/stamp and exact wire cardinality.
   Other new lifecycle/logging hooks must remain composed, not mocked away.
3. ALL-ON semantic/owner negatives: US->nonUS, nonUS->US, READY->INCOMPLETE,
   future/naive provenance, real foreign VP/open managed reservation and manual
   excess. Actual OMS guard decides; no borrowed v2 position or new lot mapping.
4. ALL-ON cross-lane WBEXIT2 interaction: owner supplies an original-children
   close obligation/resume fixture. Keep that obligation live while routed
   ORB on the same account/symbol is refused by real owner census. Assert zero
   ORB wire/cancel and no WBEXIT2 state/owner/generation change. On a distinct
   symbol, actual ORB owned attach must still reach its original protection
   consumer. This cannot be claimed independent merely because source files
   differ: Main must inspect final WBEXIT2 strategy/account dispatch guards.
5. ALL-ON logging/shadow interaction: actual v2 postflip no-chase refusal on a
   second symbol and actual POM logging/shadow evidence on their original code
   paths, alongside the ORB cases above. Assert decisions/wire/owners unchanged
   by formatting and zero new phone/provider transport. Do not substitute unit
   SDK call counts for request/minute or hot-loop cost qualification.

Suggested source-only selector names if Main approves authoring in this lane:
tests/unit/test_orbcountry1_next_package_combo.py::test_all_new_off_preserves_orb_stamp_and_legacy_decisions
tests/unit/test_orbcountry1_next_package_combo.py::test_all_on_keeps_exact_orb_parent_and_legal_replace
tests/unit/test_orbcountry1_next_package_combo.py::test_all_on_never_borrows_wbexit2_foreign_owner
These are PROPOSED names, not present files/collected IDs. Do not run them yet.
Main owns any combined test shared with the other lane owners. WBEXIT2 next-day
boot actual PASS remains a separate mandatory9l gate, not an ORB unit claim.

## History and deadline

Six-case manifest remains the same GIXI/GENK/SAIQ/OLOX/STAK/ELPW scope under
C11131 missing-input disposition. Zero complete callback packets are claimed;
ELPW exact stamp pair is bound for route comparison, not downstream permission.
First country-routing activation timestamp remains unbound to this author;
conditional instructions are not an actual process flag receipt. Do not recapture
or backdate. Existing per-case UNKNOWNs and no-country legacy control remain.
Review-ready source is not an install forecast: all package items reviewed,
ALL-ON combination and WBEXIT2 next-day boot must actually pass; entire install
including watch/guard/preopen rebaseline must finish Oct29 02:30 ET or wait for
next close. No partial OMS rush; independent ATR page priority unaffected.
