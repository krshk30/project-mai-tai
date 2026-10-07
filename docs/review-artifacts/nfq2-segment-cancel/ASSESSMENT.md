# Canonical RECLAIM opportunity-wide cancel follow-up

Starting published NFQ head 8ea8a824ce2f7c8ee3e0c0e0b62702b4fe6cebb3.
SLOT remains 9951b473f8203590f48c667770c07e1f9c3096b6 and main remains
994f08aee2c35b3809b3c3384e0d8628807dcfb7. This is a linear NFQ-only
follow-up; no SLOT/RETRYLEFT/ORB/RPGSTALE/handoff/production source changes.

## Reachable protocol, not a recorded historical BIYA incident

strategy_core/schwab_1m_v2.py::_cw_v2_quote supplies canonical fanout identity
when NFQ is enabled; reactive source maps to RECLAIM in fanout_identity.py.
oms/service.py admits that non-resting v2 EH BUY route and invokes
_nfq2_observe on the serial intent lane. The actual
_queue_removed_wait_barriers builder emits a BUY-only, exact-opportunity
barrier carrying RESTING identity. OmsStore.find_open_order_for_cancel targets
BUY orders throughout that opportunity, deliberately without a slot filter.
The prior NFQ observer's slot check left local RECLAIM holds/queued tokens alive.

The control uses recorded BIYA event shape/price/quantity/account fields but
explicitly reroutes identity through the actual producer to canonical RECLAIM.
Its local hold, token, state and removal request are test-controlled. Recorded
BIYA itself is RESTING and is NOT a historical reproducer. No invented prints,
bars or actual placements are claimed. RETRYLEFT's separate trigger is not
integrated here; this tests its shared cancel-barrier protocol, not that trigger.

## Preserved RED and Narrow Fix

Untouched 8ea8: four assertion failures (held/queued x primary/mirror), 0.70s,
UTC 2026-10-07T20:59:20.706597Z through 20:59:21.849870Z. Raw SHA256:
f73d50d3b4cb302a091a9db7df389c7d784130247042b70102e91338dba2a2dd.
There were no setup/import failures or invalid slots. The first source edit
occurred after the 8ea8 full suite ended at 20:58:54.487446Z.

The special path requires typed BUY-only RESTING barrier identity, a valid
removal UUID and exact opportunity/segment. Existing account/symbol/strategy
and generation guards remain. Ordinary slot-targeted cancels are unchanged.
Only held/queued owners without dispatch evidence may be retired. It locks
and rereads the durable snapshot, checks full event and exact held intent,
refuses ANY related BrokerOrder (including terminal labels), then performs
durable CAS before dropping memory ownership. It invalidates durable queued
shadow tokens too, preventing restart resend. Dispatching/uncertain/wired
owners are never released by this path. No age expiry, broker cancel or forced
ownership/ticket release is introduced. Serial intent SQL remains outside the
tick path; tick eligibility is still memory-only.

24 unit controls cover held/queued, primary/mirror, typed scope negatives,
ordinary targeted cancellation, dispatching/uncertain, related orders,
intent/durable proof failure, worker queued shadow, CAS race and restart.
Q1-Q8 safety mutations are RED; Q3's first single-guard mutation was masked by
the second durable guard (NOT_RED), preserved unchanged. The corrected Q3
removes both phase guards in that same boundary and is RED. No runtime guard
or assertion was weakened. Exact commands/timestamps/hashes are in receipts.

Working-source final fast controls: 528 passed, 2 benchmarks deselected,
15.92s; UTC21:08:04.586076Z-21:08:21.799327Z. Raw SHA256:
6664e9bbf6f94ff4f2723a4dc0874c7198c70ec18f104c1b71ad620f2d81b012.
The prior R1-R11/L1-L6/N1-N17/C1-C3 mutations were rerun on this changed
source: 37/37 RED. With Q1-Q8, 45/45 named safety mutations are RED.
Ruff and diff whitespace checks pass. The earlier 164-test focused receipt
also passed, but accidentally included an old 60s test; it is not a new-head
frozen benchmark and is not substituted for that gate.
The six combined quote mutations also reran RED: tick SQL, eligible loop SQL,
drift tick SQL, irrelevant-symbol read, duplicate inflight and disabled drift.
Final source SHA256 a16b9d755450d22e75181b859be537a3116670232dd715629b3c2cc5673c68f9;
new unit SHA256 74a6db718a533285965002e51825d4eb84861d946e5de60d781d27afe3913d0d.

## Historical Full Evidence and New Gates

8ea8 full: 7333 passed / 56 failed, 610.17s. Main994 actual baseline:
7114 passed / 56 failed. Sorted exact failed-node added/removed sets both empty;
all 56 remain unwaived. Raw full SHA256:
13a7d7f3311e191998f0b255c296f7d428d4122d974781f25c5caedbc32b4d39.
/tmp/nfq2-recovery-final-pair-20261007/PAIR_AND_SOURCE.json preserves the pair.
8ea8 activehold proof: 14400 events / 60.001543s, 239.993828/s,
16.187417ms max loop stall, drift enabled, zero tick SQL/transactions.
These certify 8ea8 only, NOT the changed cancel source.

Freeze/publish one tested new source head, then run sequential new-head
activehold60 and the standard full unit command with no observer/tee. Compare
against existing main994 using ownmix1/verify_suite_pair.py exact-node parser,
not a naive split (baseline stdout contains appended asynchronous warnings).
Fresh CI and independent review remain gates; no readiness/pin/deploy claim.
Later raw receipts can remain outside the committed tree to avoid CI churn.
SQLite/fake Redis/simulated broker and controlled clock limits remain explicit;
no live OMS or PostgreSQL performance certification is implied.
