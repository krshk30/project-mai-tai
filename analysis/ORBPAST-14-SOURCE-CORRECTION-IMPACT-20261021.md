# ORBPAST14 Source Correction And Unresolved Quote Contract

SOURCE AUTHORING ONLY / ALL CONTROLS UNRUN / NOT14-GREEN OR INSTALL READY.
See14-REGRESSION-CLASSIFICATION for all14 exact JUnit nodes and source causes
recorded BEFORE edits. Existing tests, RED7ef and original JUnit are untouched.
Main owns queue/LATEST and sole runtime; no worker test/import/compile/AST/
application/replay/DB/provider/SQL/SSH/orders/install/restart/push occurred.

## Four Independent Source Corrections

1. broker_adapters/schwab.py submit/replace now call
   _authorized_order_request_json(request,method,path,body). It validates the
   initial request fence, binds it to a task-local ContextVar and invokes the
   ESTABLISHED _authorized_request_json(method,path,body) shape. A finally resets
   the context on return/error/cancellation. Real _authorized_request_json picks
   up that fence before token awaits; existing _request_json passes it into the
   scheduled transport thread. No TypeError fallback/retry is introduced. The
   old PUT stub can supply its201/Location, so existing unknown-confirmation
   logic can retain NEW-PARENT under HOLD instead of losing receipt before PUT.
   Actual old-parent/unfilled checks, native OTOCO confirmation and unknown/
   late-fill protections are unchanged. Fenced401 still has no automatic retry.
2. submit_order guard scope restores the original unmarked native STOP_LIMIT
   bracket contract for pre-existing shared-adapter callers. A policy key of
   ANY value, any prewire callback or LIMIT selects fenced validation and must
   carry exact policyv1 PLUS callback; malformed/unknown policy cannot downgrade.
   Nonbracket/exit-only/invalid-shape ORB entries still refuse. New OMS selection
   ALWAYS marks policy+fence and still refuses missing/stale/above-cap quotes;
   this is NOT an OMS missing-quote fallback. Main must review this legacy/new
   contract boundary; no new unmarked LIMIT authority is granted.
3. orb_country_route.routed_orb_metadata calls legacy one-argument geometry
   builder for Webull, enabling tick_cap=True only on Schwab. Webull geometry
   mutation remains observable instead of raising unsupported-keyword TypeError.
   New Schwab cap/anchor geometry and all country/control fields are unchanged.
4. log.configure_logging adds optional preserve_handlers=False; default callers
   retain existing force-configure behavior. OMS requests preservation when
   root handlers already exist, preventing each service construction from
   removing external/capture handlers. Its named logger level is explicit;
   no-handler startup still configures the original console sink. OMS Webull
   SDK redaction install remains on both paths. Only initialization/log routing
   changes, not order authorization. Broader logging/native controls need main
   execution; no performance claim from source.

## Ten Original Assertions Still Have An Input Conflict

Nodes1,5,6,7,8,9,11,12,13,14 in the classification supply no current quote for
their selected symbol. They expect a SEND based on old pre-ORBPAST inputs. New
scope requires missing quote => no send. No source can truthfully satisfy both
conditions for the same request without another explicit input/policy contract.
No synthetic ask, ATR-bar-as-quote, test-type detection, mock-capability bypass,
default-price branch or live freshness waiver was added. Existing tests remain
unchanged, and these ten MUST NOT be represented as fixed or passing.

New tests supply explicitly synthetic fresh streams to those same original
functions and preserve their assertions. This checks native control, owner/net,
country routing, prior-session owner and ATR-after-preview behavior under the
new valid-input contract. It is not retroactive acceptance of the original14,
not historical consumed-quote proof, and not original11 native replay.

Main/reviewer disposition required: authorize an input-only test fixture
amendment supplying current quotes while preserving assertions, or explicitly
revise approved feature rollout scope. Worker has neither altered existing
fixtures nor weakened new real safety. Until disposition and actual tests,
overall zeroNEWvs556 remains BLOCKED; only four independent source issues have
author corrections. No automatic broad suite/reviewer repeat run by worker.

## New Regression Controls, All UNRUN

tests/unit/test_orbpast_source_regressions.py is the ONLY new test file. Existing
test files have zero delta vs7ef. It reuses unchanged original test functions:
ten quote-dependent assertions with explicit fresh synthetic input; legacy
momcancel caller; native unknown-PUT identity; Webull geometry mutant; visible
above-cap refusal. Additional controls preserve unfenced-new-LIMIT/marked-STOP
refusal, task-local fence isolation, after-token/thread stale refusal before
transport, fenced401 no retry, context reset, and missing-quote no-preview/no-send.
No tests collected/executed; counts are source definitions/parameter variants.

Interface graph: process_trade_intent -> original risk/owner/route/quote/
preview/permit -> ControlledBrokerAdapter if present -> Schwab submit/replace
-> order-request fence context -> legacy authorized transport signature ->
token -> original thread fence -> HTTP. Existing direct non-ORB adapter calls
remain unmarked; wrappers clear context for their own request. Context lifecycle
and native uncertainty require actual main controls, not this diagram as proof.

Integration overlap: OMS logger construction and Schwab dispatch/thread path
overlap MOMBUFFER/LOSTACK/ORB; main must compose reviewed hunks and preserve
durable payload/permit binding and unknown ownership. No source from those
other lanes was merged, copied over or edited. log.py is shared: optional
default behavior unchanged for other service names/callers, OMS preservation
is deliberately opt-in and redaction remains installed.

## Saved Scope And Resume

Input adapter551dcecfea65710389deac0eec7314be7cd3fffd remains clean/frozen at
/private/tmp/mai-tai-orbpast-input-adapter-20261021; exact checkpoint and raw
hash6597e8a3/bundle799bcdef retained. Its hardcoded7ef source pins intentionally
cannot qualify a changed candidate. Resume after urgent source disposition:
review new approved source head, update input binding in a separate version,
obtain full original plan/P/clock/settings/native/trade supplement and reconcile
all22 including APUS/GENK without guessing. No actual adapter execution now.

Original eleven acceptance, four MI/JAGX/FLYE/GIXI unchanged cases, real native
fills/partial/children, country/owner/permit/cap/stale, boot04/restart/full/ALLON
and independent source+impact review remain owed. Missing input/trade horizon
requests are preserved, not superseded by a regression review. RED JUnit remains
an immutable failed receipt; no new PASS from static diff/hash/format checks.
