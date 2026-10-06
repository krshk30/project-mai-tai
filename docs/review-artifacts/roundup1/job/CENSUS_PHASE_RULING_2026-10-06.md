# October 6 Post-Close Census Admission

Latest human instruction supersedes the old 14-ticket/64-row admission.
Application remains exactly 4805ddc81184c76b4d5cef5c483c809edb666fe6.
Full all-date population is captured; count is informational. Only requested,
price_wait and submitting prohibit admission by ticket phase. This does not
clear waiting/held_unknown ownership, write a journal row, or prove broker flatness.

Own read 16:13:31 ET: 103 rows, refused80 / expired11 / filled10 / held_unknown2;
requested0 / price_wait0 / submitting0. Payload footprint558313 bytes.
Full captured payloads are TICKET_CENSUS_2026-10-06_1614ET.jsonl; capture completed
between the 16:13 phase pull and the 16:15 strict-flat read. The filename is not
a claim of a database capture timestamp. Unknowns: AIXI4e16a30c and XHGb725a133,
both exact_old_order_unproven; neither is cleared or relabelled by this gate.

Own exact SQL helper: complete103, linked orders95, linked intents170, zero
nonterminal database orders/intents, installed schema0022. The capture has a
1024-row/4MB resource ceiling, not a reviewed population size; overflow is
UNREADABLE, never truncation or assumed clearance. Actual runtime jobs() output
is compared with the complete read and count in one read-only repeatable snapshot.

## Gates And Current Evidence

| Gate | Rule / own current evidence |
| --- | --- |
| Ticket phases | 103 captured; zero requested/price_wait/submitting, passes |
| Snapshot completeness | Count agrees with full captured IDs; malformed/source/identity changes block |
| Broker positions / books | 16:15 direct both accounts flat; managed/virtual/account rows empty |
| General preflight | Strict-flat helper rc0; MI180/NXL2 exact standing allowances printed unchanged |
| v2 restart | 16:18 raw rc0 with reviewed clock-only override; zero armed, zero managed, both flat |
| OMS restart | 16:18 raw rc0, strict all-account-position-flat option, fresh books |
| Broker working orders | Still mandatory fresh complete direct lists; not inferred from a ticket phase |
| Historical exact parents | Expanded check stopped after24 proofs: two rejected rows lack broker IDs; not called proven |
| Schema | Verify0022 unchanged; official report and daily command use --no-schema-change |
| Entry activity | No new buy order/intent/fill after install start; unchanged immutable ticket bindings |
| Fleet/source/Redis | Rechecked by actual attended runner; not claimed passed from these partial receipts |

Missing-ID rows: Schwab OLOX schwab_1m_v2-OLOX-open-b95fc559b0a2 and Webull MOBX
schwab_1m_v2-MOBX-open-07bc532a7fdc. Missing identity is not a terminal broker proof.
Parent requested an explicit disposition for these two historical rows; until
received, no production staging, approval, checkout/env change or restart occurs.
The full broker audit has a600s bound and2s spacing between Webull detail reads;
bounded unreadable re-read policy remains three attempts60s apart, measured
blockers stop immediately. Direct flatness is refreshed last before each action.

## Mechanics Changes

All-date phase admission replaces fixed14 semantic refusal and fixture-specific
immutables. Initial complete capture establishes actual bindings; subsequent
reads may account/expire an existing ticket but cannot change old order/slot/
segment/replacement, revive a finished ticket, add/delete identities or move
revision backwards. Full before/after payloads and phase tables remain evidence.
No runtime trading source is edited. Global SQL nonterminal checks recognize
aborted as terminal, matching the merged application's audit status.

Local final job suite693 PASS46.62s, including the real Real.sql method with
complete103, each forbidden phase, accounted restart changes, missing capture
and new-buy controls. Initial688 PASS44.50s remains an earlier receipt.
No installation PASS or COMPLETE exists.
The earlier census refusal and this new historical proof refusal remain recorded.
