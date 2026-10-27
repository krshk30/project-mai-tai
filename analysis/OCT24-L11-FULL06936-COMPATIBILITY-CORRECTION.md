# Cohesive L11 full-suite compatibility correction

Galileo sole L11 source; isolated worktree
`/private/tmp/oct24-l11-full-compatibility-20261024`, branch
`codex/oct24-l11-full-compatibility`, parent06936d2ecc9469664bddbcf605896272afd00b25.
Main frozen06936/AED/worktrees and harness94 are NOT edited.
Source/control authoring only: NO tests/import/AST/compile/app/replay/PG/provider
or production execution. New25 and changed controls below remain UNRUN.
Initial7e3d checkpoint is preserved; this successor incorporates C1's explicit
16:29 source conditions, not a claim of old/new runtime qualification.

## Actual evidence and causal classification

Main full source-bound06936:18771P/162F/17ERROR/84S/2X/EXIT1,3056s;
104 A2 baseline bad nodes plus75 NEW according to Main's exact comparison.
Raw `/private/tmp/OCT24-06936-CI-BOUND-FULL-FIRST-20261024.log` and `.xml`
remain immutable. This source packet does NOT claim those75 are cleared.
The raw log directly supplies these74 nodes targeted by this correction:

| Root cause | Original affected count | Concrete evidence |
|---|---:|---|
| New KEEPWATCH bool omitted from ops inventory | 44 |42 expected_flags_check failures; test_all_on_pm process-catalog1; test_keeprest1 effective-catalog1. load_catalog refuses missing key at checker94. |
| New direct setting read breaks legacy public constructor inputs | 22 |17 setup ERRORs across cw_disarm_line/resting_cancel_rth_placed/resting_reason modules;5 orb_momentum_turn_report failures. Core664 reads an absent attribute on SchwabV2Config/SimpleNamespace. |
| Partial fixtures omit real dependency methods |8|4 gateway-subscription,3 held-symbol coverage,1 held-symbol entry refusal. Real service defines both helpers; fixtures bind calling methods without them. |

The remaining new MOM virtual-children finding is Main's independent lane.
Other pre-existing source-digest, D3, financial/native and baseline failures
are NOT hidden, repinned or adopted by this packet. No full-suite retry here.

## Minimal APP correction and unchanged semantics

Only APP path: `src/project_mai_tai/strategy_core/schwab_1m_v2.py`.
The new optional mode now follows the constructor's existing legacy-settings
contract: absent field -> False. Real Settings has an explicit bool defaultFalse.
Public SchwabV2Config now ALSO explicitly declares that same rollout boolean,
defaultFalse, because actual public callers pass it as the constructor's first
operand. This is a real public config source fix, not17 tests expecting absence.
The report fixture's SimpleNamespace explicitly declares False, backed by the
actual production Settings definition. No historical packet changes.

```python
        # Legacy indicator/report configurations predate this optional mode.
        # Absence preserves OFF; an explicit non-boolean must never enable it.
        keepwatch_enabled = getattr(self.settings, "strategy_schwab_1m_v2_keepwatch1_enabled", False)
        if type(keepwatch_enabled) is not bool:
            raise ValueError("strategy_schwab_1m_v2_keepwatch1_enabled must be a boolean")
        self._keepwatch_liquidity_enabled = keepwatch_enabled
```

This is a single named, default-OFF boundary, not generic catch/getattrTrue,
truthiness coercion or missing-evidence-as-permission. Explicit False/True retain
their respective original-volume/first-accepted-volume algorithms unchanged.
Explicit invalid legacy values None/0/1/'false'/'true' refuse before enabling.
Typed Settings still handles process env coercion by its original contract.
No OFF->ON default, floor, threshold, readiness, boot seed, strategy arm,
line/history fingerprint, ownership, permit, journal, publication, native,
provider, query, cadence, buffer or timeout changes. All L12/L14 bytes untouched.

## Actual ops registration, not test-only coverage suppression

`ops/health/expected_flags.json` gains EXACTLY one entry:
strategy_schwab_1m_v2_keepwatch1_enabled, expectedTrue, owner schwab-1m-v2,
no additional consumer. Only that strategy service consumes the switch.
Ruling cites C1/operator14:45/16:00 ALL-ON package, final gates and separate GO;
the catalog is not activation authority. Code default remainsFalse.

The checker implementation is UNCHANGED: complete boolean inventory, duplicate
and unknown keys, owning service, actual PID/env/default/dotenv, unknown and
REAL FAILURE logic remain enforced. Its real145-boolean inventory now has
one additional owner check: existing166 ->167 bool-consumer checks, plus the
unchanged8 numeric checks =>175. Tests keep exact145 keys/exactservice counts,
and keeprest/all_on summary assertions change174/174 ->175/175 ONLY. Existing
zero-mismatch and six-mismatch expectations remain unchanged, not generalized.

### Exact install-env manifest (review-only, not current production)

`ops/install/OCT24-L11-L12-L14-ENV-MANIFEST.json` explicitly lists:

```text
MAI_TAI_STRATEGY_SCHWAB_1M_V2_KEEPWATCH1_ENABLED=true
MAI_TAI_STRATEGY_SCHWAB_1M_V2_BROKERDECIDES1_ENABLED=true
```

These are the only new L11/L12 boolean rollout flags; both code defaults remain
False. L14 has NO boolean enable flag in actual06936 Settings. Its real activation
operand is MAI_TAI_ATR_SIMULATION_AGGREGATE_RECEIPT_PATH in control, required
and bound to34886838bytes/SHA265b847b615df1efbb85cbf3b053bd9d597198309bdebd89d270ca458b0698e8.
The existing source retains its original immutable artifact provenance; physical
box copy path must be sealed by Main as root-owned/read-only before install.
No physical destination is invented here: manifest value is explicitNULL/MISSING,
install_ready=false, not a false assertion of page ON/current box config.
The optional REALIZED_RECEIPT_PATH/REALIZED_RECEIPT_SHA256 pair is separately
listed with actual-copy/external-digest gate OPEN, never a fabricated amount.
No newL14 switch, newrule, env write or installation script is introduced.

## Complete partial-fixture contract repair

Production definitions beside each fixture correction:

| Fixture fix | Actual production definition on06936 |
|---|---|
| report `settings()` SimpleNamespace adds explicitFalse (all5 failed report nodes) | Settings.strategy_schwab_1m_v2_keepwatch1_enabled, settings.py:487, bool/defaultFalse |
| gateway `_Harness` binds `_observation_symbols` (4 failures) | SchwabV2BotService._observation_symbols, services/schwab_1m_v2_bot.py:3114 |
| held `_Harness` and `_PollHarness` bind `_observation_symbols` (3 failures) | SAME real production method:3114 |
| held `_EmitHarness` binds `_entry_membership_allowed` (1 failure) | SchwabV2BotService._entry_membership_allowed, services/schwab_1m_v2_bot.py:3137 |

Each method binding has that source pointer immediately beside it in the
fixture; production method bodies are not replaced or made permissive.

`tests/unit/test_v2_gateway_subscription.py`:
bind actual `_observation_symbols` alongside actual `_subscription_symbols`;
explicit new flagFalse and derive `_keepwatch_enabled` from that setting.
Every existing watch/held/registration/debounce assertion remains unchanged.

`tests/unit/test_v2_held_symbol_exit_coverage.py`:
same real method plus explicit modeFalse for `_Harness` and `_PollHarness`;
`_EmitHarness` binds actual `_entry_membership_allowed`, explicit modeFalse.
No arbitrary permission lambda, swallowed kwargs, fake country/native flat,
or broad APP fallback for incomplete fixtures. Held symbols remain subscribed
for exits and still cannot enter; original assertions are untouched.

These are deliberately legacy OFF fixtures, NOT combined-ON boot proof.
New ON adverse controls call the real observation/subscription/membership
methods with actual KeepWatchSession.update and unreadable boot exclusions:
retained observation can continue, but UNKNOWN still denies entry. No guard is
patched, and no favorable proof/line/flat/ownership capability is installed.

## New adversaries: FIRST25 proposed, all UNRUN

One new module `tests/unit/test_keepwatch_full_compatibility.py`:

- Legacy publicConfig/namespace/SettingsOFF constructors3.
- Explicit public config field/default and actual ON constructor1.
- Explicit bool OFF/ON volume revision + next-minute reset2.
- Explicit invalid None/0/1/strings refuse5.
- Exact catalog boolean/owner/default/activation-separation1.
- Missing/duplicate/unknown catalog entries still refuse3.
- Actual ON/OFF/absent/invalid process env classification4.
- Dotenv-only value missing from actual process remains UNKNOWN1.
- Real observation union/held-exit membership/UNKNOWN-entry guards OFF/ON2.
- Both repaired legacy harnesses bind the actual dependency method2.
- Install manifest exact booleans/catalog/services and no forged L14path1.

Main-only proposed first command selector:
`tests/unit/test_keepwatch_full_compatibility.py`, on the literal clean source
head from this packet, existing memory/IO fence/privateSQLite/fresh raw/XML/
audit/basetemp, normal assertions. No actual DB/provider/source-guard bypass.

After source review, Main can claim the original74 affected bad nodes from
the retained full XML, not an unchanged full rerun or an invented qualified union:
the42 failed nodes in test_expected_flags_check.py; the two explicit catalog
nodes below;5 failed orb_momentum_turn_report nodes;17 ERROR nodes in the
three v2 constructor modules;4 failed gateway and4 failed held-coverage nodes.

```text
tests/unit/test_all_on_pm.py::test_all_on_process_catalog_checker_refuses_three_dark_values_without_live_io
tests/unit/test_keeprest1.py::test_all_on_effective_catalog_zero_with_controlled_process_environments
```

This is a selection proposal, not execution approval or results. No claim of
74PASS, 75closed, fullPASS, ALL-ON, real seed, timing, native or installation.
C1 16:29 requires corrected ALL-ON73/73, then ONE corrected full with exactly
104 named A2 baseline bad nodes and0NEW; these gates stay OPEN and assertions
remain intact. Main comparison source is
`/Users/velkris/.codex/task-queues/analysis/OCT24-06936-FULL-NONREGRESSION-COMPARISON-20261024.json`.

## Impact and retained replay checkpoint

Call graph: strategy constructor -> single optional bool -> existing floor
selector; catalog loader -> complete original inventory validation -> original
process audit; fixture subscription helper -> actual observation union and
exit coverage; fixture entry refusal -> actual membership guard.
No service APP, scheduler, REST reader, owner lifecycle, data or replay-tool edit.
Any final combined successor/binding/carry needs Main's explicit source review;
old06936 case receipts are not retagged to this new source generation.

IZM case06 `2026-10-13/IZM/1791901980000/1791902100000` actual ZERO/HALF
assessment remains the saved replay resume point when Main reports completion.
No case was cancelled or silently dropped; no further pair launched by author.
JZ EXPECTED_PHASE_SENSITIVE amendment and original UNKNOWN receipts preserved.
CF1 real restored-row ALL-ON04:00 ->firstflip07:00 complete seed N/N remains
the TOP install gate under V2/Arendt/Main; this compatibility work cannot
substitute fixture-only success or preempt that proof. Full/hotpath/remaining
case/native/financial/boot/install gates remain open against exact sources.
