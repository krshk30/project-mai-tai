# ORBCOUNTRY1 next-package source handoff

Owner: original Cicero. Task OCT28-1056-ORBCOUNTRY1-NEXT-BUILD.
Authority: operator YES/C1 2026-10-28 10:56; Main independent assessment 10:59.
Base 32b0f6f6860bf6441363cddd91aa20709005f691, branch codex/oct28-orbcountry1-next.
Tonight's frozen package, historical roots and all other lanes remain untouched.

## Reviewable source

One production module: `src/project_mai_tai/oms/orb_country.py`.
One new test file: `tests/unit/test_orbcountry1_semantic_admission.py`.
Main-only Settings/service proposal is external:
`/private/tmp/OCT28-CICERO-ORBCOUNTRY1-MAIN-HOOKS.proposed.diff`.
It is NOT applied in this checkout. Main must review/compose both hooks before
running connected controls; this module-only checkout is not deployable.

New setting proposed: `orb_country_semantic_admission_enabled=False` in code;
future qualified install TRUE only under separate Main/C1/operator authority.
No catalog/env/shared-service edits by this author. No new IO/provider API.

OFF keeps four-field exact admission comparison and five-field exact parent
comparison. ON ignores only as_of equality, logging both stamps and all three
semantic fields. Neither input dictionary nor parent stamp is rewritten.
Fresh CountryRouter/CountryStore read remains the admission authority.

Connected impact: process_trade_intent -> fresh verify -> original session,
MACD/ATR/risk/botcontrol/native/manual/foreign-owner admission -> original wire.
Webull reprice -> exact original target binding -> original cancel/read-zero ->
original owner/fill/CAS/price/session/risk/ACK/quantity fences -> replacement.
Schwab unsent continuation -> same matcher -> existing fresh verify and remaining
guards. No changes to economic writers, protection, exits, route selection,
CountryStore, paper, ATR, D1, SDKs, SQL, migrations or worker scheduling.

### Important service proposal review

Reprice events have intent_type=cancel. Existing top-level fresh verification
checks only opens. Proposed hook also checks flagged routed reprice events,
BEFORE any cancel/wire. This ensures an actual fresh US/non-US/status change
refuses replacement despite cached producer metadata. OFF retains original
open-only behavior; ordinary cleanup cancels remain outside the new check.
This is explicit source review scope, not a claim the old path already did it.

## First controls — all UNRUN

Main command after composing hooks into a new literal review head:
`python -m pytest -q tests/unit/test_orbcountry1_semantic_admission.py`

33 authored parameter-expanded controls (source count, NOT collection result):
1 default OFF; 6 same-route OFF/ON/three semantic routes; 6 changed-field/route
marker negatives; 1 absent-switch original behavior; 6 real store OFF/ON;
2 actual router US/non-US transitions; 2 status/future-clock negatives;
4 real OMS original-owner/foreign-owner dispatch controls; 4 actual Webull
replace paths (OFF stamp refusal, uncertain cancel, zero-terminal replace,
fresh route transition refusal); 1 source-read-error propagation.

Fixtures reuse the existing private ORBROUTE setup and controlled broker ports.
Country records are explicitly CONTROLLED and validated by actual CountryStore;
real router and OMS consumers decide. No permission predicate replaced in these
new controls. Inherited base fixture patches completed-bar/session-control ports
as disclosed in test_orbroute1_oms_chain.py; this is not native replay proof.
No enlarged session window: existing 09:28 entry/09:30:15 reprice bounds remain.
Schwab RTH actual replacement-chain qualification remains a separate required
related control/replay; pure shared matcher is not claimed as that full proof.

Suggested related Main selection after source review (no author execution):
`tests/unit/test_orbroute1_oms_chain.py`
`tests/unit/test_orbroute1_manual_net_092.py`
`tests/unit/test_orb_pastlevel.py`
Preserve all original assertions/failures. Main decides exact first-run roster
and private DB/network fence; no blanket full-suite evidence requested.

## Historical six-place acceptance — OPEN, not fabricated

GIXI 2026-10-12; GENK 2026-10-14; SAIQ 2026-10-20;
OLOX 2026-10-21; STAK 2026-10-22; ELPW 2026-10-28.
Membership is C1's six observed places, not a complete all-history population.
For each require retained exact original event/parent/account identity, selected
route metadata, contemporaneous CountryRecord and decision clocks; required
owner/quote/cancel/ACK operands must be retained or explicitly UNKNOWN.
No later store snapshot represents an earlier as-of country/owner record.
ELPW paired stamps and 91-share owner fact are C1-attributed evidence; exact
as-of owner row/execution packet and restamp writer remain UNKNOWN to author.
No lost-entry, historical send/fill, or original replay PASS is claimed.

Retained candidate sources previously identified (not newly exported/validated
as complete replay packets here): SOURCE21 full order/fill capture; STAK exact13
`/private/tmp/OCT23-STAK-EXACT13-CURRENT-FIRST-20261023.jsonl`; old country/ORB
source receipts under the queue analysis directory. These cannot invent missing
historical clocks or owner state. Main should supply the already-retained exact
six-place selector/packet paths before binding the original replay consumers.

## Runtime gates

All controls, imports, compilation, replay, provider/PG/SSH/SQL and install UNRUN
by author. Main alone owns actual execution, shared hooks, commit integration,
PR/push and qualification. Source-only delta is not install or trading GO.
