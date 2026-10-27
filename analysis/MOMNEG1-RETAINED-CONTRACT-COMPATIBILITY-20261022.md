# MOMNEG1 retained-contract compatibility

Task: MOMNEG1-RETAINED-CONTRACT-COMPATIBILITY-20261022.
Owner: Cicero, existing MOMNEG1 sole source author. SOURCE ONLY; no execution.
Base: cac98b036e0663911c7582b7f7af112446c99623.
Worktree: /private/tmp/mai-tai-momneg1-retained-contract-20261022.
Branch: codex/momneg1-retained-contract-20261022.

## Fixture correction

The partial-9a triage identifies 26 baseline-passing synthetic cases whose intended
volume, country, ownership or worker branch is now preempted by the legitimate
MOMNEG missing-prior refusal. This is input-contract drift, not evidence that the
new guard should be relaxed. The real engine's detection payload already contains
`prior_close` (momentum_paper/engine.py:803).

Five unit modules change only controlled fixture inputs, comments, and four new
shared-fixture controls. No existing assertion is removed or altered. No production,
PRICE, retained input, proof script or acceptance file changes.

- test_gap6_warned_volume_cache.py: 12 affected cases; controlled prior 1.50 vs 1.7341.
- test_momlive1_country_integration.py: one affected case; controlled 2.50 vs 3.
- test_momlive1_handoff.py: six affected cases; controlled 2.50 vs 3, including the
  explicit payload override in country evidence sealing. Replacement payloads remain
  exact: the helper does NOT merge a default prior into a caller's missing input.
- test_momold1_live_proofs.py: three affected cases; SYNTH-only prior 2 vs 2.125.
- test_momvol1.py: four affected cases; controlled 1.50 vs 1.7341.

Four NEW parameter cases exercise the actual LiveHandoff.offer: missing key,
down-day, equal and up-day. They check exact outcomes, original payload conservation,
refusal logging, queue/ledger state, routing and no Redis emission. Missing/down
never route or claim. Existing production MOMNEG negative/composition tests remain
unchanged. These values are CONTROLLED inputs, not historical symbol evidence.

Impact: shared row fixture -> real handoff; overridden detected payload -> actual
service _offer_live_signals -> handoff -> real EpisodeLedger. Synthetic MOMOLD proof
fixture -> existing compare_amod -> real handoff. No dispatch worker is started by
the new four cases. Existing worker tests retain their original assertions.

## AMOD: explicit pending C1 contract, not silently refreshed

Original tests/acceptance/test_momold1_retained_acceptance.py remains byte-identical
to base, SHA256 6443ba91dbb889300e5f5e9e41e5bb0602f003a796a45a1b5d67c817e7036a64.
scripts/momold1_live_offer_proof.py also remains identical, SHA256
c7339b23e7a4b804998e8c3ceddca5e34861e35f43d6cd2e51b92c4f0579be56.
The original handoff_source_proof equality is still expected to refuse the
intentional MOMNEG production delta; do not claim this patch resolves acceptance.

The two local actual AMOD 2026-10-21 08:15 warning DETECTED CSV lines were manually
read: both seal prior_close 1.29, detect.price 1.57 and observed_decision_ns
1792584906753471330. No value was inserted into that packet. Existing declared CSV
pin: 1ca01bf7c6987cc634f04e37553df3b97da8e8f87453edbad32d55ea5c320490.
This task did not rehash the packet or execute its verification/replay.

Proposed C1-reviewed additive contract (NOT implemented/approved by this document):

1. Keep the original failed byte-equality evidence and the original test. Allowlist
   ONLY the exact Git delta for handoff.py from PRE_REF
   99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1 to the pinned base above. Its only hunks
   are @@ -83,6 +83,23 (signal_price + sealed-prior check/refusal logging) and
   @@ -96,7 +113,7 (reuse parsed signal_price). No other code or path is permitted.
   Reject any additional delta; do not use an arbitrary new digest as a waiver.
2. PRE handoff SHA256 is
   06e1a17b50b8dd4b3a70508ba7f1c828801c61e6e78dddb0c101e2b1effb008d;
   POST handoff is
   07871a7dac021a82fc8f4f80f0f7e81f60e962a9fad49123791121f45750b049.
   admission.py is exactly unchanged at
   0d04022ac0dbced6571f7fdc5005b3f7b3aaa81dfb4eac15c9490d6f5bfb1cac.
   Main can retain the raw diff using:
   `git diff 99fcbeaacb8e8d9a487ad5534a3862c488fcbdb1 cac98b036e0663911c7582b7f7af112446c99623 -- src/project_mai_tai/momentum_live/handoff.py src/project_mai_tai/momentum_live/admission.py`.
3. Under a NEW explicit main claim, verify all original packet/log pins and select
   exactly the same two actual DETECTED rows, preserving entire payloads, identities,
   sealed clocks, SEC route and volume 15361. Run actual PRE and POST handoff code in
   separately pinned source roots; the existing compare_amod alone is insufficient
   for that distinction because its handoff_factory uses the current imported class.
4. Compare per-record payload hash, forwarded volume, clocks, result, queue, ledger,
   emission and no-worker state for both actual roots across all five original cache
   contexts. Expected source-based hypothesis: both preserve low-volume refusal for
   fresh/boundary and unknown-volume refusal for absent/stale/future, since 1.57>1.29.
   Any unequal outcome is a failure to investigate, not an allowed MOMNEG difference.
5. Pair with real missing/down/equal/up guard controls. Do not mutate AMOD to create
   those controls, substitute synthetic input for retained acceptance, or claim native
   orders/fills/PnL from call-boundary proof. C1 must explicitly reconcile the original
   byte-equality criterion before acceptance or replacement is authored.

## Main-only next claim / evidence limits

Suggested functional selection AFTER source review/new claim, NOT run here:

```sh
python -m pytest -q tests/unit/test_gap6_warned_volume_cache.py tests/unit/test_momlive1_country_integration.py tests/unit/test_momlive1_handoff.py tests/unit/test_momold1_live_proofs.py tests/unit/test_momvol1.py tests/unit/test_momneg1_live_admission.py
```

Targeted triage scope is 26 existing cases plus four new parameter cases. Whole-module
collection count must come from main's actual collection/result, not this source review.
All fixtures, new controls, retained AMOD decision comparison and composed regression
are UNRUN. No tests/imports/AST/compile/app/replay/DB/provider/network/production action
occurred. Existing actual paired MOMNEG performance receipts are not a qualification
of this new fixture head. GIXI actual prior/P1 evidence is not established here.
