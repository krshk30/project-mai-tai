# MIRRORHOLD1 plan receipt

[codex] Sole-writer isolated branch `codex/mirrorhold1`, base
`c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`. No production changes, installation,
merge, shared HANDOFF edits, or WEBULL429 branch changes.

## Accepted Step0 and ruling

Own frozen cutoff: 2026-10-06 12:55:51.850465 ET. 76 forgotten events,
75 price-wait/cap events (62 RPG + 13 caps), 71 before 11:30; AIXI 54,
IPDN 9, XHG 8, OLOX 4; seven symbol-segments/slots. Four named acceptance
segments comprise 66 events, not the complete census. Own raw receipts remain
in `/Users/velkris/.codex/sessions/2026/10/06/rollout-2026-10-06T11-12-58-01a111c6-6c38-7b83-a351-feca34a7999c.jsonl`:
records 845, 859, 873, 878, 908, 954, with hashes recorded in the independent
`mirrorhold1-retained-price-wait` assessment. That branch is not modified here.

The subsequent operator ruling resolves ONLY the cap ambiguity: an attempt is
an actual Webull submission, never an outside-8% evaluation, queue write,
freshness/local-risk refusal, or RPG reauthorization nonce. Counter meaning:
one initial wire submission plus at most three actual resubmissions per exact
account/strategy/symbol/segment/slot. Uncertain wire reservations remain owned
and cannot authorize another buy. Counter/generation survive reprices and restart.

## Scope and evidence boundaries

- IPDN 10:27: eight sampled price steps through first sampled in-band 10:48,
  stop 4.73 / ask 4.38. No 8% exception; hypothetical Webull fill UNMEASURED.
- IPDN 11:44: 11:55 stop 4.5839 / ask 4.2 is outside 8% (8.3750%); retain.
  Next sampled in-band 11:56 stop 4.54 / ask 4.19. Acceptance/fill hypothetical.
- XHG 10:32 Schwab rejection is not Webull terminal evidence.
- AIXI 09:57 terminal cancel remains UNMEASURED: bounded evidence has PLACE,
  not a proved terminal cancel. Distinguish reprice handoff envelopes from
  TradeIntent CANCEL and v2 RESTING-CANCEL. Test a controlled terminal cancel
  on recorded identity separately; never label it a historical 09:57 cancel.
- PA1 is RTH `rth_resting_mirror`; PM software rest arms without a broker order
  and emits `eh_resting` marketable LIMIT only at its guarded cross. Preserve
  those PM guards/windows unchanged. No claim of measured PM PA1 recovery.

## Implementation and verification plan

1. Add default-off, durable exact-account retained owner and canonical price
   generation separate from RPG auth nonce. Promote same-slot real prices while
   preserving budget; no same-event or same-price budget reset.
2. Integrate every loss site, not only the two RPG forgets: new mirror event,
   cap, uncertain enqueue, serial completion, NFQ transfer and restart. Unknown
   dispatch remains fenced, addressable and fails closed; no blind retry.
3. Keep normal risk/collision/quantity/notional/freshness/8% and old-ticket-clear
   authorization boundaries. Persist reservation before broker await; spend
   counter only at evidenced wire boundary. Retire by exact Webull acceptance
   or fill, actual segment/session end, not sibling rejection/acceptance.
4. Replay recorded price/quote samples with explicit controlled dispatch; test
   generation CAS, restart, enqueue ambiguity, duplicate copies, stale cancels,
   per-leg outcomes and all-on RPG composition. Mutate budget/no-forget/duplicate/
   cancel fencing controls. Compare focused and full suites against untouched base.

Baseline command (started before edits):
`env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -p no:cacheprovider --junitxml=/tmp/mirrorhold1-baseline.xml`

No runtime proof is claimed by this plan receipt. Source integration, controlled
tests, mutation results, and baseline-pair results will be reported separately.

## Historical blocker and subsequent resolution

The independent Step0's original STOP remains valid as a historical assessment:
three whole-slot changed-price evaluations would delete IPDN at 10:34 and make
required 10:48 placement impossible. Its original text and raw receipts remain
unchanged in the separate assessment worktree.

The later user ruling supersedes that cap conflict, not the historical evidence:
distance outside 8% is a free indefinite segment hold; RPG reauthorization never
increments the mirror counter; three caps actual resubmissions. This plan spells
out the counter as one initial submission plus three resubmissions, not three
total submissions. A canonical new-price hash is distinct from an authorization
nonce; neither resets the segment budget. A lost acknowledgement is not a
proven no-wire result and cannot be retried blindly.

## Freeze boundary and remaining UNMEASURED

This commit contains this plan artifact ONLY. Exploratory implementation files
are uncommitted in the isolated worktree and are not an approved, complete,
tested, or pushed runtime. Build is paused for the parent checkpoint.

- Durable generation/CAS ownership across restart and NFQ transfer, uncertain
  enqueue and wire crash recovery, actual-wire budget bookkeeping: UNMEASURED.
- Post-callback fresh quote, price and final quantity binding; old-ticket clear,
  ORB collision, risk, sizing and duplicate fencing under all-on RPG: UNMEASURED
  for the new implementation. Existing safeguards must remain authoritative.
- Same-account Webull acceptance/fill retires only its leg. Primary refusal
  cannot erase a mirror; a terminal cancel/segment/session end must fence all
  later retries of the retired owner. New implementation controls: UNMEASURED.
- AIXI 09:57 actual terminal cancel remains UNMEASURED. Own bounded raw record
  873 shows 09:54 and 09:56 Webull RESTING-CANCEL with `reason=reprice`, then
  09:57 PLACE; record 878/908 has no TradeIntent CANCEL in 09:56-10:00. Reprice
  handoff envelopes are not proof of a v2 terminal cancel. Controlled terminal
  cancellation on recorded identity must be labelled controlled, separately.
- RTH PA1 recovery does not establish PM recovery. PM `eh_resting` software arm,
  cross guards, marketable-limit shaping and existing windows remain unchanged;
  no window extension or 8% waiver is planned.
- Counterfactual IPDN Webull acceptance/fill, mutation controls and focused/full
  baseline-pair results remain UNMEASURED. Never label hypothetical executions
  as observed venue fills.
