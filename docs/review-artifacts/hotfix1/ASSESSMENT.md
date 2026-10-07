# HOTFIX1 independent Step 0

Assessment reported before the first source edit on 2026-10-07. The parent
received the assessment after edits had started; implementation was paused
until the parent acknowledged it and released the scoped work again.

Issue: AGREE. On base main 1a70da19176d2cad486e8518e1c9030f1cabb968,
both quote/trade callbacks reach `_mirrorhold_evaluate`, which opens a
synchronous session and scans/locks all held/queued owners before filtering by
symbol. `_cancel_drifted_working_orders` also awaits a database candidate read
for each eligible quote, including when MIRRORHOLD1 is off.

Independent bounded production reads: `/var/log/project-mai-tai/oms.log`
contains BIYA NO_FRESH_QUOTE at 12:20:00.439 and 12:20:06.322 UTC and SXTC's
no-wire/re-authorization sequence at 13:46-13:47 UTC. A 14:11 UTC query found
one retained owner, SXTC, retired by the operator rollback, zero wire clients.
Two whole-database samples at 14:11:29.890010 and 14:11:41.763415 UTC increased
commit+rollback totals by 3,462 (about 292 transactions/second). This is NOT
attribution to these paths. The initial tool receipts were not saved as files;
`production-pull.txt` is an explicitly repeated bounded read, not a fabricated
copy of the initial pull.

UNMEASURED independently: historical py-spy fraction, sync latency percentiles,
SXTC's exact decision-cache age and pre-rollback held phase. A held row after
rollback cannot prove that historical phase. No live benchmark has been run.

Proposed fix: AGREE in principle, subject to tests. Symbol memory indexes are
eligibility hints only. Commit publication, rollback exclusion, revision/token
fences, broker-sync order refresh, and off-loop dispatch revalidation are
required. Merely awaiting `to_thread` from the serial tick consumer still
delays later exit callbacks: the first draft was NOT safe on that point.
Scoped continuation uses deduplicated background dispatch; no database query
is performed just because a tick arrives. Genuine dispatch still rechecks
durable ownership, quantity, phase, generation, and current price.

No production environment, services, rows, orders or Redis keys were changed
by this lane. MIRRORHOLD1 remains false live until a reviewed deploy.

Subsequent own pull at 14:16:40.681243 UTC (`recorded-cases.json`) proves the
original SXTC Webull intent at 13:42 had
`refusal_code=webull_mirror_no_fresh_quote_held`, stop 3.2300 and quantity 92.
It also retains both BIYA intent refusals, quantities 236/118, and three bounded
Schwab quote captures. `recorded-trades.json` retains eight bounded trade
captures. These prove intent/tape records, not the historical OMS cache timing.

Scope limitation: legacy `mirror_fresh_price._evaluate_nfq_holds` still has its
own synchronous preparation path when legacy NFQ ownership is active. That is
the separately owned NFQ2 lane; this patch does not edit it or claim to remove
all OMS database work. Retained-owner dispatch claims still use the serial
intent lane's existing durable proofs; the new tick-triggered preparation and
retirement transactions run off-loop. Real broker exits and live whole-process
throughput remain unexercised by the offline benchmark.
