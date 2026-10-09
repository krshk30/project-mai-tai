# RPGNOWIRE1 final review receipt - 2026-10-07

Step 0: AGREE, communicated before implementation. Own code read and bounded
read-only SXTC captures are in ASSESSMENT.md and raw/. No production changes.

Authoritative source/test freeze: 74ad6c709aeb99a7fcb44db071b5c2befb8923d5.
This receipt is a subsequent docs-only commit; no tested byte changes.
Python 3.12, this worktree's PYTHONPATH=src, pytest -p no:cacheprovider.

## Measured results

| Run | Failed | Passed | Skipped |
| --- | ---: | ---: | ---: |
| Parent's independent main 1a70da19 baseline | 56 | 7077 | 0 |
| Frozen RPGNOWIRE1 full unit suite | 56 | 7125 | 0 |
| Frozen RPG/NFQ/MIRRORHOLD focused suite | 0 | 766 | 0 |
| Parent's isolated HOTFIX + RPGNOWIRE integration | 0 | 85 | 0 |

Full-unit failed-name set difference: added=[], removed=[]. The actual 56-name
baseline, including volatile momentum throughput, is retained, not the older
48-failure baseline. Parent composition deselected one benchmark; it is not an
additional full-suite run. CI remains separate from these local receipts.

## Mutation controls

Each mutation ran alone in an isolated subprocess against in-memory function
source; committed files were unchanged. All 17 RED, no ERROR or survivor.

| Mutation | Failing tests | Verdict |
| --- | ---: | --- |
| N1 remove proof release | 7 | RED |
| N2 remove entire-generation wire veto | 6 | RED |
| N3 remove positive refusal origin | 1 | RED |
| N4 remove slot identity | 1 | RED |
| N5 remove quantity identity | 1 | RED |
| N6 remove journal revision CAS | 1 | RED |
| N7 remove both Fill evidence vetoes | 2 | RED |
| N8 remove contradictory wire hints veto | 3 | RED |
| N9 remove retained wire-count veto | 1 | RED |
| N10 allow retained unknown phases | 3 | RED |
| N11 retain stale queue token | 1 | RED |
| N12 feedback clear instead of terminal refused | 4 | RED |
| N13 remove filled/no-rebuy veto | 2 | RED |
| N14 remove wired-loop dirty/signal wake | 2 | RED |
| N15 remove account proof scopes | 8 | RED |
| N16 remove generation proof scopes | 6 | RED |
| N17 remove positive-filled metadata veto | 1 | RED |

N14 supplies held_unknown -> clear at an explicitly controlled controller seam;
later loop progression is real. It is not a recorded production wired-recovery
claim. See TEST_CONTRACT.md for that disclosure and every legacy assertion change.

## Raw receipts and SHA256

Local receipts (not production files):

| Path | SHA256 |
| --- | --- |
| /tmp/main-1a70-hotfix-rpgnowire-baseline-20261007.xml | c263beb94f0eee1d205b67c952a79e49c8e8c746259ad643020da34970473581 |
| /tmp/rpgnowire1-frozen-74ad6c70-unit-20261007.xml | b8234be2f07f488f49c68931750b61392acc293c6686eb0011f02e8e26c9d2b1 |
| /tmp/rpgnowire1-frozen-74ad6c70-focused-20261007.xml | e7f2a85dc5a27fa8a1e3eaa8578e77a0c61c40cf324b394f37c9935a646fbcc6 |
| /tmp/rpgnowire1-frozen-74ad6c70-mutations-20261007.jsonl | 98684d5f9494ffc611ff28efa9bf0ae50c516708e98c40d4c26a1da817ef1885 |
| /tmp/hotfix-rpgnowire-parent-composition-20261007.xml | 339ef9eac72ea81fcfd75d9181a02714e2d6a1c62e22e5747b9c67e7e0f606b5 |

## Safety and limits

Exact positive opening-intent proof is required for both old and saved orders.
Later dispatches are vetoed across the entire generation and exact client IDs,
without limiting the proof to the initial capture time. Reads=0, metadata labels,
age or row absence alone never release ownership. Genuine wired/unknown orders,
positive fills and dispatch uncertainty remain protected. Terminal feedback reaches
the real bot authorization lane and only its matching account/generation latch;
the next normal draft is tested, not merely ORM ownership becoming false.

The actual retired/archived SXTC row and controlled current-held/queued races are
distinct. An exact prepared transfer invalidates stale serial claims before v2
is told, without resetting the submission budget. HOTFIX parent composition
independently verifies cache eviction and stale-copy rejection.

Schwab cached ineligibility is unchanged: a normal draft is not venue acceptance.
Supplemental bounded live wired-control pulls returned zero sampled rows, so an
own live accepted sample is UNMEASURED. Existing recorded APUS wired sequences
and explicit accepted/partial/fill controls preserve the wire path.

No new flag; no service.py, mirror_retained_hold.py, strategy source or catalog
change. Retained-hold catalog remains false pending HOTFIX review. No merge,
deployment, broker order, ledger write or production mutation in this lane.
