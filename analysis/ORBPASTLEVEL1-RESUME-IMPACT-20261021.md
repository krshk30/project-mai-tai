# ORBPASTLEVEL1-20261021 Source Impact Handoff

AUTHOR ONLY / ALL UNRUN. Resumes b45385dd on exact556. Stable task and all
additive original scope retained. No queue/status/LATEST changes by worker.
No PASS from source inspection, whitespace checks, counts, hashes or Git.

## Caller Graph And Behavior

`orb_schwab_order_route.build_orb_schwab_open_intent` and
`build_orb_schwab_reprice_intent` -> bracket builder `tick_cap=True` ->
`orb_schwab_intent_refusal` compares the same tick-cap plan ->
`OmsRiskService.process_trade_intent` keeps risk/collision/session/control
admission -> `_orb_pastlevel_request` -> pure `select_entry`.

Country path: `orb_country_route.routed_orb_metadata` enables tick_cap only
for Schwab; Webull/default builder retains prior rounding and shape.
Actual OMS quote cache is populated by `_handle_quote_tick_event` at the quote-book
assignment using `_event_time(event)` as received_at. That is event age, not
proof of historical local availability; replay preserves this distinction.

Pure selector: below rounded trigger -> STOP_LIMIT at effective cap; at/above
trigger and within rounded cap -> LIMIT at rounded ask; missing/stale/future/
above-cap -> no new send. Upward nominal cap with one-tick floor explicitly
permits >0.5% effective tolerance. Exits stay +5%/-8% from rounded trigger.

Initial place -> native preview -> post-preview session/ATR checks -> same-price/
shape fence -> durable MAY_HAVE_SENT -> control/permit -> adapter submit.
`OrderRequest.prewire_check` is an in-process callable, not provider metadata.
`SchwabBrokerAdapter.submit_order` -> `_authorized_request_json` token await ->
`_request_json` -> thread fence -> blocking HTTP. Fenced401 has no automatic
resubmit. Existing shared non-fenced behavior is preserved by the conditional
callback, but broad ATR/momentum controls still must be run by main.

Existing-parent reprice -> `_orb_schwab_cancel_refusal` keeps ownership/unfilled
parent checks -> `_process_orb_schwab_reprice` -> preview/quote fence -> adapter
`replace_bracket_order` -> exact native old-parent read -> PUT -> full native
OTOCO replacement confirmation. STOP_LIMIT or LIMIT old parent accepted; same
identity/quantity required. Unknown results keep existing unknown hold behavior.

No parent -> `_orb_pastlevel_continue` -> `unsent_plan` -> `day_intents` and
BrokerOrder budget census -> same-route root/local-zero/terminal-zero only ->
risk/country/owner/collision/MACD/ATR -> normal open path. Any old native order,
missing receipt, prior fill or possible dispatch refuses. Same-plan continuation
does not create another daily budget. Above-cap requires a later explicit
higher strategy reprice; no background quote-tick submit was introduced.

Resume source delta: `_orb_pastlevel_request` now records selection refusals
with reason/trigger/raw ask/cap/quote timestamp as well as successful selections.
The four pre-existing authored OMS shape cases now include an above-cap log
assertion; no safety assertion was removed. Prewire refusals remain governed by
the existing fence; the selection log is not proof of a native send.

## State, Dependencies And Overlap

State lives in existing TradeIntent.payload.orb_pastlevel; no new DB schema,
flag, dependency or service. Local-zero must precede any possible wire; the
durable MAY_HAVE_SENT phase is conservative and is not automatically cleared
even after a positively local adapter refusal. This may hold an otherwise safe
retry; availability is not waived to clear an unknown exposure.

MOMBUFFER overlap: oms/service.py placement/control path,
broker_adapters/protocols.py OrderRequest callback, and Schwab authorized/thread
send path. Main must reconcile BOTH callbacks and preserve each lane's durable
payload/permit binding and MOMSTUCK2 guards. No blind whole-file replacement or
automatic merge was attempted. Other lane source is untouched.

Consumer review required: broker order type persistence/fanout, replacement
metadata and child protection adoption, owner/day budget, country route and
shared-account MI/GIXI; Webull path/default cap; ATR/momentum non-fenced adapter;
daily guard manifest if its pinned files are affected by the final composition.
Thread check bounds a local transport call, not server arrival or network delay.

## Replay And Verification Limits

V2 price comparator calls the actual candidate selector, binds all eight src
files plus driver, validates packet hashes/plan/decision/quote identities and
explicit Massive provenance. It preserves raw decisions/quotes/broker records,
missing declared decisions and UNKNOWN results. Strict arrival and event-time
studies are separate. Nanosecond precision is not silently discarded.
Conditional touches are descriptive only and never named native fills or wins.

New synthetic contract module checks selector comparison, cross-symbol/plan and
provider provenance, quote availability/future/inverted clocks, missing source
inventory/census receipt, wrong settings/binding, missing-decision denominator,
tampered bytes, naive/nanosecond clocks, trade correction/cancel/eligibility/
receipt uncertainty, opposing touches and unchanged broker-event baseline.
None imported, collected, parsed, compiled or executed by this worker.

Required main execution: focused tests and zero NEW vs exact556; original eleven
state/quote replay with actual native baseline; native LIMIT OTOCO and replacement
confirmation; late partial/unknown/restart and04ET state; owner/country/MACD/ATR/
body/session/control safety; final full and ALL-ON with MOMBUFFER. Independent
source+impact review/Claude GO still owed. Original snapshots and Massive packet
are not present in the bounded ORBPAST-specific inventory; exact request is in
ORBPASTLEVEL1-REPLAY-V2-INPUT-REQUEST-20261021.md. No provider/DB retrieval done.

Expected replay cost remains unmeasured, O(packet bytes + decisions*trades),
memory O(packet bytes); no latency or benchmark claim. No worker runtime, tests,
imports, compile, AST/parsers, app/DB/PG/provider/SSH/SQL/orders/push/install/
restart. Static review and git diff --check are formatting evidence only.
