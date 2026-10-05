# Own startup-ticket capture, October 5

This is a read-only production census, not a production recovery or a claim
that the candidate tests already pass. Pull scripts are committed alongside
this document; no service/env/broker/ledger/Redis writes were made.

Own database capture12:13:20 ET (16:13:20Z) returned8 jobs,5 old BrokerOrder
rows and5 deferred TradeIntent rows. Every query ran in a READ ONLY transaction
with8s statement timeout and65-row sentinel (refuse above64). Queries include
the exact three deferred generations, not account/symbol latest-buy guessing.
No snapshot-batches read. The credential-protected Settings read ran as root;
the remote process used nice19. Failed first invocation as trader could not
read the protected env and made no database call; rerun as root succeeded.

Raw local artifact:
`/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005/tonight-startup-complete-own-read.json`

SHA256: `5b705a28a37db0b124b66c7474b7e93e1c807ee9c2cfafcbe5b5443d8e57dca7`.

Own broker capture12:13:55 ET made four exact Schwab GETs, no history scan,
no token refresh and no order call. Account identifiers redacted; order IDs
and broker quantities retained for identity tests. Each response had a1MiB
read cap. All four returned200 with3507/3507/3506/3266 bytes respectively.

Raw local artifact:
`/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005/tonight-schwab-ticket-orders-own-read.json`

SHA256: `8e067d93d0a569aa4151dba1109806ef64ea25a9924bdbc782c374f408059234`.

## Exact Census

| Ticket | Account / symbol | Stored phase / reason | Fresh evidence; what candidate must prove |
|---|---|---|---|
| fbfd692d-ec1f-5a39-9e11-1a133abccc96 | live:schwab_1m_v2 APUS09:32 | refused / replacement_refused | exact parent1008165370341 CANCELED,112 shares,0 filled; reject was price canonicalization, not live old order |
| bd6ac0b9-727c-500b-8581-aabdd95992d4 | live:orb APUS09:32 | refused / replacement_refused | originala37e14f5 cancelled, brokerOKH442PUJ5P19N50P4R5V1IFTB; replacement reasonwebull_mirror_precheck_deferred; deferred replacement still needs its own no-wire/retirement proof |
| a007716c-4b50-5759-a354-ddc5b8961154 | live:schwab_1m_v2 VEEA09:36 | refused / replacement_refused | exact parent1008165371009 CANCELED,107 shares,0 filled |
| faa55c1f-262b-52e2-adcc-280ab1e5e2ff | live:orb VEEA09:36 | held_unknown / exact_old_order_unproven | initialfdbc5564 refusedskipped_before_submit/deferred; later3752acc6 refusedclient_abort/rpg_old_buy_still_owned; zero BrokerOrder rows on exact old generation; do not omit later retry |
| 7c7c5afb-30ff-539f-8baa-4c2e585c6e12 | live:schwab_1m_v2 APUS10:27 | refused / replacement_refused | exact parent1008168139729 CANCELED,112 shares,0 filled |
| ee3d0d07-abea-5fe2-98c9-cace5c08d135 | live:orb APUS10:27 | held_unknown / exact_old_order_unproven | initial0450607f refusedskipped_before_submit/deferred; later76f13280 refusedclient_abort/rpg_old_buy_still_owned; zero BrokerOrder rows on exact old generation |
| ff6464ff-d65e-5657-8f47-1dc8d7b053c3 | live:schwab_1m_v2 RETO11:22 | held_unknown / readback_budget_exhausted | exact parent1008171127230 REJECTED,252 shares,0 filled,0 remaining; child tree alsoREJECTED; release needs this proof, not elapsed time |
| ba108172-04f6-5659-892b-a0fc10d22b15 | live:orb RETO11:22 | held_unknown / exact_old_order_unproven | extra eighth ticket found in own pull; initial4f89bb79 skipped_before_submit/deferred, zero exact-generation BrokerOrder rows |

The last three Webull tickets carry cancel client IDs rather than proven old
wire client IDs. Only exact persisted opening intent/generation and absence
of wire rows can establish local no-wire. A truly uncertain dispatch or a
contradicting broker row must remain blocked. No inference from age or an
empty account/symbol latest-buy query is acceptable.

## Findings Exposed By The Required Replay

At reviewed #1093 head28952463, `_rpg_persisted_local_open` accepts only
skipped_before_submit/webull_mirror_precheck_deferred rows and refuses every
later same-generation row of another refusal class. Actual VEEA/APUS later
retries are client_abort/rpg_old_buy_still_owned, so a replay omitting those
rows would not prove real startup recovery. The test lane was given all rows.

At that same head the strict BUY readback `_result` recognizes cancellation
but not REJECTED as terminal-zero. The coordinator does not re-read an exhausted
held_unknown ticket. The recorded RETO body therefore requires a narrowly
tested exact-order recovery path, never a ticket timeout or clearing database
rows. These are candidate-code findings, not permission to alter production.

Final candidate dispositions/test names/results are to be filled from the
follow-up report before eligibility and the exact-SHA GO. Main's old
flag-OFF ticket-block characterization is a real blocker, not a safety PASS.
