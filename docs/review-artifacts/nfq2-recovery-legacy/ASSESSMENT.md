# Released NFQ residuals: independent assessment before code

Own starting head c6004bd59f06fd856b1a8bb5a1ff6d96cbe094dc, stacked on
SLOT9951b473f8203590f48c667770c07e1f9c3096b6, main994 unchanged. Sole NFQ
writer in /tmp/codex-nfq2-on-slotclear1-linear-20261007. Original SLOT branch,
ORB, RPGSTALE/#1115, production, staged jobs and parent handoff remain untouched.

Reread confirms: `_nfq2_reason` unconditionally preserves an uncertain hold;
`_nfq2_pre_submit` returns to normal pricing when economic identity is absent.
Thus a fresh ask can bypass the intended legacy refusal policy. Generic
BrokerOrderEvent payloads do not consistently include explicit cumulative fill
quantity; a cancellation label/ACK cannot independently authorize recovery.

Plan: durable serial CAS captures dispatch event/client/hold/generation identity.
Periodic, bounded strict parent BUY detail reads reuse the existing read-only
Schwab/Webull adapter API, never submit/cancel. Require exact durable order,
intent, account, strategy, symbol, quantity, slot/segment and generation proof,
then explicit terminal-zero readback and a post-read DB recheck/CAS. Recovery
retires the proven-clear attempt; it never automatically resends an old BUY.
Unknown/missing proof remains blocking; fills are sticky no-rebuy evidence.
Older bound NFQ snapshots may acquire dispatch identity only from an exact
durable order+intent token link. No identity is inferred from age or latest flip.

Legacy unbound incoming EH entries explicitly refuse with durable reason under
NFQ ON, regardless of fresh quote; do not disturb bound holds/owned orders.
No guessed migration. Only a pending pre-wire intent with exact event identity
and no linked order may be locally drained/refused. Owned/unprovable records
are preserved and remain non-admissible. OFF/non-EH paths remain unchanged.

Recorded controls: retained BIYA intent/account/slot/segment shape, and unchanged
AMOD Schwab and AIXI Webull terminal-zero broker-body fixtures plus recorded AMOD Webull filled
body. Dispatch/clock/transport/ledger scaffolding and BIYA recovery transitions
are controlled scenarios, NOT historical BIYA broker outcomes. No fabricated
prints/bars or actual placement claim. Existing SLOT recorded/RPG-owner controls
stay intact. Fast tests/mutations first; ask parent CPU slot before any60s gate
or current-main full suite. Publish one tested source follow-up; readiness only
with actual safety, CI and fresh independent-review prerequisites satisfied.
