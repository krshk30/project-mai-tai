# C10849 exact freeze-source successor, Main-only execution

Authority: C1 LATEST2026-10-27 08:49ET and Main direct relay. This supersedes
ONLY the candidate-not-integrate hold for the exact reviewed17 APP lines.
No threshold/guard/feed/GC-policy redesign, retention change or installation GO.

New isolated clone:
`/private/tmp/oct27-l11-c10849-ecd2-boot-freeze-20261027`.
Literal baseline ecd2e90e799009a9750a379b2dfa182bd613d41b, reviewed candidate
e77245918cb7237caf228567be5978a7a7136faf. APP src and16-case controls are
BYTE-IDENTICAL to that candidate. New successor metadata adds this handoff and
control selection only. Original ecd2, e772 and fddf harness roots untouched.

APP delta to ecd2 remains ONLY17 insertions in
src/project_mai_tai/services/schwab_1m_v2_bot.py: importgc, process lock/attempt
latch/helper, one call immediately AFTER actual restoration-backed/no-danger
BOOT-HOLD release log. No freeze at restoration latch alone, timeout or constructor.
Once is per process, across repeated checks/re-holds/service instances. Exception
from freeze/count remains visible; claimed attempt is never automatically retried.
No threshold/disable/collect/unfreeze/APP bounds, no startup-readiness bypass.

Original process-wide risks and failure policy are preserved in the UNMODIFIED
analysis/OCT27-C10659-T2-BOOT-GC-FREEZE-SOURCE-HANDOFF.md. Frozen cycles may remain
unreclaimed until process exit/unfreeze; reference counting of acyclic objects
continues. This is NOT memory-leak freedom, live-performance or qualification proof.
New post-boot allocations remain tracked. No freeze was executed by source author.

Source controls: analysis/OCT27-C10849-T2B-CONTROL-SELECTION.json lists16 planned
parametrized selectors (10 equality inputs +6 once/order/error/source checks).
Test file is exact old b8268fd5 bytes. Source selectors are NOT actual collection
or PASS; Main must verify membership and is sole executor. Controls mockGC only;
they do not freeze the runner. Original cold restoration/native/PG proofs separate.

NEXT Main: review literal source+pins/newhash tuple; bind clean frozenfddf177
harness to THIS newAPPhead/src/inventory under exactreview and ONE fresh T3c
purpose. No descendant carry of ecd2 app head checks, no editing original harness.
Normal60s/>=200/MAX<50 and minimal64floatgen2 unchanged, no attribution observers.
Actual16 focused controls/T3c/micro960 UNRUN by author. C1 orders T5BOLT-23+AHT-21
only if T3cPASS; originalanchors and all guard criteria unchanged.

T4 Gibbs/Arendt/Main MUST rebind/review to newAPPhead because boot path changed;
old ecd2 boot receipts do not prove this head. Main owns runtime/privatePG/boot,
all source authors stay out of execution. No deployed files, flags, restart or
install: all gates and explicit C1 OPERATORGO still mandatory.
