# KEEPREST1 Candidate Proof - 2026-10-06

Base: `4805ddc81184c76b4d5cef5c483c809edb666fe6`.
Starting evidence commit: `6d77a9f19d12b7bbf6077139c2bb807318afc2a5`.
Branch: `codex/keeprest1-frozen-buy-after-flip`.
Worktree: `/Users/velkris/.codex/worktrees/keeprest1-frozen-buy-after-flip/project-mai-tai`.
This is review evidence, not install GO. No merge, service restart, environment,
auth-store, broker, Redis, production database/ledger or shared-handoff writes.

## Behavior and Scope

One already-active first rest holds its existing raw line, trigger, quantities,
wire prices and generation through completed BUY state in RTH or PM. The new
`resting_buy_frozen` latch is not a cross, intent or slot claim. It never resets
`resting_flip_ms`. Real EH-cross admission, its original band, pricing/sizing,
dedup and fill-settle grace are unchanged. `pm_resting_flip_seen_ms` remains a
PM observation rather than proof of submission. The new floor-bar timestamp is
a completed-bar identity, not an expiry timer. Three consecutive completed thin
bars cancel; repeated evaluations of one bar cannot spend three bars.

Fill, SELL, gap, entry-window and session cleanup clear the freeze through
existing management. Reclaim ownership is not managed by the new branch.
UNKNOWN ownership still cannot rearm after a cross. RETRY max0 remains one
closed trade: a pre-flip close consumes its budget; an already confirmed BUY
owner stays bound after close until its segment ends, without another entry.
No broker adapter, OMS proof, bracket/exit, RPG authorization or pricing code is
changed. Code default OFF; expected catalog ON for `schwab-1m-v2` only.

Historical evidence is separate from controlled dispatch. STEP0.md and
`tests/fixtures/keeprest1/recorded.json` retain all 18 recorded rows. Human chart
labels do not overwrite the bot's 15 LONG / 3 SHORT probes or six exact-level
taken-cross controls. Later bar-high reach is 14/18, not 14 executable entries
or realized outcomes. Missing historical raw line/ask/fill state is labelled,
not reconstructed using a current offset or a synthetic profitable outcome.

## Frozen Local Checks

Focused command uses normal PATH with the existing venv prepended and
`PYTHONPATH=src:.`; ten named unit files cover KEEP, mutations, ALL-ON, PMPRINT,
PMREST, OWN, RETRY, catalog, tonight flags and slot provocations.
Result: **378 passed in 6.37 s**, including eight assertion-red mutations.
Raw `/tmp/keeprest1-focused-final-v2.log`, SHA256
`2c8b86656e4c27a143238909c664f7a16566c779c6d76834c6da30bcef8c6dd7`.

Each mutation is compiled and installed only in memory. Only an AssertionError
from the mapped acceptance control counts as killed; setup, import or runtime
errors do not. The initial indentation error was fixed before any eight-kill
claim. Mutation names and targets:

| Mutation | Assertion Target |
| --- | --- |
| waiting_disabled | PM waiting BUY remains active with exact line for 90 bars |
| taken_cross_misclassified_waiting | Recorded IMCC 08:26 taken cross retains expiry |
| frozen_price_moves | Exact waiting line is unchanged |
| sell_cleanup_removed | RTH waiting rest and mirror cancel on SELL |
| thin_bar_cleanup_removed | Third completed thin bar cancels |
| thin_quote_callbacks_count_as_bars | Five same-bar evaluations count once |
| fill_freeze_latch_not_cleared | Durable fill clears freeze |
| cross_dedup_removed | OLOX controlled REST cross emits once, both sized legs |

ALL-ON uses the eight base switches plus KEEP, not Sagan's separate flag.
Local typed Settings -> controlled process-environment -> real audit:
**9 ON switches; 131 boolean names; 144 Boolean service checks + 8 numeric;
9 services; PASS 152/152, mismatches0, unknown0.**
No real process read or broker call. Receipt
`/tmp/keeprest1-all-on-effective-20261006.json`, SHA256
`5c8ea7e3269c7f4b9bc6f4557318fb698234d4b55d26088c59363da535874506`.
Ephemeral reader `/tmp/keeprest1-all-on-effective-20261006.py`, SHA256
`69e181bf1fdb24c2f89e0bd5a1191dab291a2b6e478c88ea2050af56fd62f94d`.
These local analysis files are not committed runtime code or a production
configuration receipt. A future lane composition needs its own summed catalog
and ALL-ON proof, not reuse of either isolated nine-switch result.

Ruff `src tests`: PASS. `/tmp/keeprest1-ruff-final-v2.log`, SHA256
`82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18`.
Marker collision gate: PASS, 77 token-specific consumers / 381 markers /
976 logging formats. `/tmp/keeprest1-markers-final-v2.log`, SHA256
`293d763bfb7b07d1ab0341bd5870f70bfbaeafe9309f2c7757148106209606db`.

Separate local integration/replay/backtest run:
**72 passed, 2 skipped, 1 xfailed, 12 PostgreSQL setup errors in 8.96 s**.
`/tmp/keeprest1-replay-final-v2.log`, SHA256
`14ffd93fa547f0d35c2ec771f1a2b52d7420ce66abc260bef7b662da0be367f4`.
All 12 errors require the configured PostgreSQL CI service; this run is not
called green or a full UNIT pair. CI must supply the PostgreSQL golden gate.

## Full Unit Pair

Exact command, run in separate managed base/head worktrees:

```sh
env PATH=/Users/velkris/Projects/project-mai-tai/.venv/bin:$PATH PYTHONPATH=src:. python -m pytest -q tests/unit
```

Fresh exact main4805: **6478 passed / 47 failed / 454 warnings / 298.54 s**.
Raw `/tmp/keeprest1-unit-base4805-exact.log`, SHA256
`cf5201ab462a8207fc136b44cf9f8ccf5a482d07e73943d6ff36410e6c5b69fc`.
`BASE4805_FAILED.txt` contains 47 canonical collected test IDs, SHA256
`fe24b2de3074d47d6104576c5d4392a4ac85e0fda55b246755703dc903859c50`.
A warning was concatenated to the last FAILED line without whitespace; names
are matched against actual collected IDs, not split blindly on whitespace.
Sagan's normal-PATH 56-failure baseline is not presented as an exact own pair.

Initial head: **6526 passed / 52 failed / 454 warnings / 305.33 s**, raw
`/tmp/keeprest1-unit-head-frozen.log`, SHA256
`6931626457793051b1a6d7d8ef88141fe82edc21c16d1b84b6d6aad89e87e7ed`.
`INITIAL_HEAD_FAILED.txt` retains those 52 names. Five were new, not waived:
two missing legitimate catalog-count refreshes and three minimal `__new__`
strategy controls lacking Settings. The lookup now defaults OFF when Settings
is absent; all five and the expanded 378-test set are green. Final full rerun
and CI must settle before review-ready.

Final frozen head: **6531 passed / 47 failed / 454 warnings / 294.71 s**.
Raw `/tmp/keeprest1-unit-head-final-v2.log`, SHA256
`f343b215c93ab2254bef741db6d5edfb9127417f1448adb7daf57b55888f6b51`.
`HEAD_FAILED.txt` and `BASE4805_FAILED.txt` are byte-identical: 47 names,
SHA256 `fe24b2de3074d47d6104576c5d4392a4ac85e0fda55b246755703dc903859c50`.
New failed names 0; resolved failed names 0; additional passing tests 53.
The 47 existing macOS failures are retained, not described as a green suite.
Push and PR Validate: PENDING. No ready, review pin, merge or deploy claim.

## Source Fingerprints

| Frozen File | SHA256 |
| --- | --- |
| src/project_mai_tai/settings.py | b0505969e3c5ff16b54bd9e69d9c2f5829804f21717f095850e54800d0113c2f |
| src/project_mai_tai/strategy_core/schwab_1m_v2.py | 8eb264f9845480a17d112cc6dc1e9387404741c194382b401805e500d926fef4 |
| tests/fixtures/keeprest1/recorded.json | f1804fd49edf36eed3568cda63203fae94900f8942557292cbcdcf7887b62bd2 |
| tests/unit/test_keeprest1.py | 05c5a814d0dc353ce724244ec52cf3c04d92f2f9ffebebaaccfbf1677dad1984 |
| tests/unit/test_keeprest1_mutations.py | 53a509e6d442d3eabe5827701d575ce960fb64cc8016c934f10684d88e78b02f |
| tests/unit/test_all_on_pm.py | 4f3d54550f653ba385da31bfda74f39c49c5279033344a2cc9e1a932dfa3a546 |
| tests/unit/test_expected_flags_check.py | 7b71ca4b6aa02cd4057634582efd84a6c72af3ce8d9682f10bdab6a71024f31a |
| tests/unit/test_pmprint1_tonight_flags.py | 8283bc40336c7e2db2fcac6443af17530f8450a270c714beabe8c60777db0e5a |
| ops/health/expected_flags.json | 6c545a04ba0cbec674eb126b05fe8eb1ad184a305da2317f40e188233c6b1c65 |
