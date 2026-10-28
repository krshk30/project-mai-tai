# C11330 L12-excluded source proposal

SOURCE ONLY. Not adopted, not an operator decision, not OFF-equivalent-to-OLD,
not qualified, not deployment authority. No imports, tests, compilation, PG,
replays, provider calls or production actions were performed by this author.

## Literal inputs and ownership

- Proposal root: `/private/tmp/oct26-l12-excluded-proposal-20261026`.
- Branch: `codex/l12-excluded-proposal-20261026`.
- Parent: `b4403c7fd8a243ea725f4ae8d149a1defd1c477f`.
- Installed comparison: `9c10a7de1cd2fc7a7df2b0e587fe132d46faa4a7`.
- b440 and c6d2e9f5ae63bb55a0a6d08375394e046a1d2bb6 have identical src/sql/ops.
- C1 2026-10-26 13:30 item 3 requests this proposal; deployment scope remains
  the operator's decision. L12 acceptance and its zero actual seals are preserved.
- Cicero classification input: `/private/tmp/OCT26-C11330-MOM-PATH-FILELIST-EXCLUSION-CHECKLIST.txt`,
  SHA256 `b960f862c3c97ffa482a7cf200b0be976078b2c96efd23ddae116b4e3657e875`.

## Projection versus b440: 31 runtime/schema/profile paths

Delete exactly the 17 `src/project_mai_tai/brokerdecides1*.py` modules present in
b440 and `sql/migrations/versions/20261022_l12_dispatch_retirement.py`.
This is a source proposal for an unapplied component, NOT a downgrade or removal
of evidence from a database. L14's chain through `20261023_atr_aggregate` stays.

Remove only L12 hunks from these 11 shared APP paths:

| Path under src/project_mai_tai | Removed L12 scope |
| --- | --- |
| broker_adapters/routing.py | native entry-book routing |
| broker_adapters/schwab.py | native entry-book reader |
| broker_adapters/webull.py | native reader and four foreground wrapper changes |
| broker_adapters/webull_order_reads.py | L12 foreground/background priority state |
| db/models.py | two projection classes and three shared index declarations |
| oms/service.py | refresh scheduling, sync counter and shutdown drain |
| services/schwab_1m_v2_bot.py | initialize, callbacks, refresh, alerts, closed-owner scope |
| settings.py | L12 switch and native-card max-age setting |
| strategy_core/schwab_1m_v2.py | L12 admission/recovery/native/reservation/publication hooks |
| v2_flip_entry_ownership.py | L12 closed-owner episode field |
| v2_removed_wait.py | prewire capability construction and result propagation |

Remove only L12 expectation/activation entries from `ops/health/expected_flags.json`
and `ops/install/OCT24-L11-L12-L14-ENV-MANIFEST.json`. The latter retains its
historical filename and L14 operands; explicit proposal metadata prevents treating
the changed document as a historical unchanged profile or activation approval.
`install_ready` remains false. No environment or live flag was changed.

## Sensitive files restored to exact installed bytes

The four adapter paths above, OMS service, db/models, v2_flip_entry_ownership and
v2_removed_wait have an EMPTY byte diff versus literal 9c (eight files).
The 17 deleted modules and removed migration are also absent in 9c.
No L12 identifiers/imports remain under proposal src/sql/ops (case-insensitive
source scan). No test acceptance was deleted or relaxed to obtain this result.

## Actual remaining diff versus installed 9c

YES follows Cicero's whole-file/transitive consumer definition, not a claim that
the changed function was exercised. NO is not an economic compatibility proof.
These 19 rows are the complete src/sql/ops difference, not a selected subset.

| MOM/ORB/OMS consumer | Path | Retained difference |
| --- | --- | --- |
| YES | ops/health/expected_flags.json | L11 expectation |
| YES | ops/install/OCT24-L11-L12-L14-ENV-MANIFEST.json | explicit review-only L11/L14 profile and proposal metadata |
| YES | sql/migrations/env.py | existing percent escaping for ConfigParser URL |
| NO | src/project_mai_tai/atr_simulation/aggregate_entry_display.py | accepted UI display |
| NO | src/project_mai_tai/atr_simulation/cp_sim_display.py | accepted UI display |
| NO | src/project_mai_tai/market_data/keepwatch_history.py | L11 history |
| NO | src/project_mai_tai/market_data/keepwatch_producer.py | L11 producer |
| NO | src/project_mai_tai/market_data/massive_atr_seed.py | L11 seed |
| YES | src/project_mai_tai/market_data/schwab_v2_rest_client.py | L11 ChartBar/Quote producer shape, ORB transitive import |
| NO | src/project_mai_tai/market_data/schwab_v2_streamer.py | ATR streaming |
| NO | src/project_mai_tai/services/atr_simulation_legacy_routes.py | accepted read-only UI |
| NO | src/project_mai_tai/services/control_plane.py | accepted L14/UI reader changes |
| NO | src/project_mai_tai/services/keepwatch_bar_owner.py | L11 owner |
| NO | src/project_mai_tai/services/keepwatch_halt_proof.py | L11 proof |
| NO | src/project_mai_tai/services/schwab_1m_v2_bot.py | L11, SPRC, AHT retained |
| YES | src/project_mai_tai/settings.py | L11 keepwatch flag |
| NO | src/project_mai_tai/strategy_core/keepwatch.py | L11 membership |
| YES | src/project_mai_tai/strategy_core/schwab_1m_v2.py | L11/SPRC/publicConfig/publication; V2Indicators transitive consumer |
| NO | src/project_mai_tai/strategy_core/session_line_restore.py | L11 restoration |

Strict C11330 SOURCE_IDENTITY is therefore NOT met: six YES files still differ.
Do not remove L11 or rewrite V2Indicators imports to manufacture whole-file
identity. Main/C1 must review these exact retained hunks or obtain the explicit
operator risk disposition; limited 27 refusal observations do not reproduce
historical fills or certify request/exit/fill paths.

## Retention and review risks

- L14 `34a9d4dc2516f7e1c154d8a0f1a1e4192f260ee6` is NOT an L12 commit. Its
  bootstrap/control-plane/settings inputs were not removed. Existing L14 schema,
  UI and accepted artifact references remain unchanged from b440.
- All AHT b440 recovery code remains. L11 owner shutdown, pending SELL retry,
  terminal-unfilled proof and publication_guard remain. SPRC current-position,
  foreign-account and terminal-empty-ID protections remain.
- `_restore_flip_owner` keeps 9c's missing-owner provenance guard. Removing L12
  must not remove that legacy UNKNOWN behavior.
- L11 SELL retry no longer consults L12 memory. It retains current restart,
  position/waiting-order, fresh evidence, exact identity, never-dispatched and
  independently terminal-unfilled checks. Runtime acceptance is UNRUN.
- Native admission/recovery/phone benefits of L12 are intentionally absent in
  this proposal. OLD stale-owner/cap holds can remain; this is not an entry fix.
- Historical L12 tools, tests, acceptance and failures are preserved unchanged.
  Some such tests import the excluded modules and will not collect on this
  variant. Main/C1 must select the actual candidate's qualification scope; do not
  delete historical tests or weaken them to force a green unscoped run.
- No installed or private schema was changed. If any target actually applied
  L12, this source deletion is NOT a downgrade recipe or schema acceptance.
- The old expected-count/profile tests may require an explicitly reviewed
  candidate-specific scope. No passing count or install profile is fabricated.

## Reproduction of static evidence

`git diff --check` must be empty. Compare parent to this proposal for the exact
31-path removal; compare installed 9c to this proposal for the 19 residual rows.
The source handoff accompanying the commit records its literal HEAD, src tree,
clean status and generated full diffs. These checks are static, not qualification.

Remaining: Main independent diff/dependency review, C1/operator scope choice,
fresh exact source/profile binding and all gates required for that choice.
No new runtime claim was issued here. Frozen 9dd4 mechanics and all L12 runtime
failures remain unchanged; reconstruction resumes only within authorized scope.
