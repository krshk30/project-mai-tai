# L7 ORB Webull Tick-Cap Source Handoff

Task: L7-ORB-WEBULL-TICKCAP-20261022. Source authoring only; qualification pending.
Base: 55204356807fd9f081d14a235abafce888a8838a (unchanged).
Branch: codex/l7-orb-webull-tickcap-20261022.
Worktree: /private/tmp/mai-tai-l7-orb-webull-tickcap-20261022.

## Cause And Narrow Correction

Reviewer-provided STAK incident: 2026-10-22 09:28:03.452 ET, intended trigger
1.65, legacy raw stop/limit both 1.65, Webull local refusal before broker request.
The source confirms provider=schwab was the only open-path ORBPASTLEVEL caller;
Webull country routing emitted a floor(1.65*1.005) cap of 1.65. The adapter's
strict raw limit>stop refusal is correct and remains unchanged.

The same select_entry/assert_same_selection functions now accept an explicit
provider, defaulting to unchanged Schwab semantics. Webull uses .0001 below $1,
.01 at/above $1 (including rounding across $1). Its cap is the greater of the
existing floor nominal cap and the ceiling-rounded trigger plus one legal tick.
This preserves healthy existing caps, including >=10, without a magic threshold;
it does not adopt Schwab's ceiling nominal cap for Webull healthy bands.
Webull ask>trigger selects LIMIT at ceiling ask only within that cap; equal,
below, missing or stale ask retains STOP_LIMIT. Invalid/future/naive fresh proof
and rounded ask above cap refuse. STAK effective cap is 1.66 (nominal remains
0.5%, effective tolerance is explicitly disclosed).

Webull selected metadata is single-leg, not a native entry bracket. Original
trigger-anchored target/protect fields are retained. Original admission event
and country-route metadata builder are unmodified. No entry/fill/PnL reparenting,
economic writer edits, SQL, new provider reads, native retries or order repair.

## Call Graph And Impact

Country selection -> build_orb_schwab_open_intent -> unchanged route/admission,
owner/ATR/risk gates -> process_trade_intent Webull/open branch ->
_orb_pastlevel_request(provider=webull) -> shared selector -> unchanged subsequent
preparation -> final synchronous same-selection/admission fence -> existing
broker_adapter.submit_order -> Webull _prepare_single_leg_prices raw guard ->
existing tick rounding/native enum mapping/fake-or-real SDK single-leg path.

Schwab open/reprice/default selector callers keep provider=schwab. Its equality
to LIMIT, bracket/preview, plan-budget receipts and transport fences are unchanged.
Webull reprice still takes its existing separate orb_country path; no new
continuation authority. Webull native post-fill pair creation/release/terminal
proof and ATR Webull mirror shaping are byte-unchanged. No actor/runtime/UI/ops,
broker adapter, POM, L8 or shared queue/status/LATEST file was edited.

Fence scope limitation: this change checks Webull at the final OMS dispatch
boundary; Webull's existing adapter does not consume request.prewire_check after
thread scheduling/instrument lookup. Do not claim Schwab-equivalent transport
freshness or quiet-host/performance qualification from this source change.
Admission windows are unchanged. EH/RTH controls exercise wire shape/session
flags, not authority to open ORB outside its existing staging window.

## Authored Controls And Proposed Main-Only Commands

One NEW test module: 13 test functions, 51 hand-enumerated parametrized inputs;
neither collected nor run. Existing tests/assertions are untouched.
The incident fixture uses actual OMS dispatch, non-US country routing, STAK,
1.65 and the supplied incident clock, but synthetic country/native identities.
It is not an actual broker packet/replay or evidence a live order was repaired.
New controls include absent/equal/below asks, fresh bounded LIMIT, over-cap/
nonfinite refusal, final-preparation quote/window drift, actual Webull fake-SDK
wire in NORMAL/PRE/POST, sub-dollar/$1 ticks, higher-price unchanged geometry,
Schwab default, stale fallback, invalid clocks and same-shape fence controls.

Planned focused command, NOT EXECUTED (main chooses claim/environment):

```sh
python -m pytest -q tests/unit/test_l7_orb_webull_tickcap.py
```

Whole-module preservation selection, NOT EXECUTED; no selected-case-only PASS
or whole-module claim is supplied by author:

```sh
python -m pytest -q tests/unit/test_l7_orb_webull_tickcap.py tests/unit/test_orb_pastlevel.py tests/unit/test_orbpast_fresh_ask_applicability.py tests/unit/test_orbpast_source_regressions.py tests/unit/test_orb_schwab_bracket_plan.py tests/unit/test_orb_schwab_order_route.py tests/unit/test_orbroute1_oms_chain.py tests/unit/test_orbroute1_session_admission.py tests/unit/test_orbroute1_country_contract.py tests/unit/test_l8_orb_control_envelope.py tests/unit/test_webull_adapter.py tests/unit/test_webull_native_bracket.py tests/unit/test_webull_exit_pair_session.py tests/unit/test_countryroute_atr_wire.py
```

These are proposed future main-owned executions, not authorization to overlap
the ongoing LOSTACK/native/full claim. Main/Claude retain serial runtime control.
No worker tests/imports/compile/AST/parser/app/DB/provider/SSH/native orders,
install/restart/push or incident replay were performed. A whitespace diff check
is static only and is not a test PASS. No pending denominator or account state
is inferred from the reviewer incident census.

## Source SHA256 Pins

- src/project_mai_tai/oms/service.py:
  d148e7b294b4650f338a6441d20b573c1f2010e250d43935b7f85e34dcf1fae0
- src/project_mai_tai/strategy_core/orb_pastlevel.py:
  7342dc33d9bbaa06b7dd48c26f8658efa98c0361d56b852c1bf2702581a3ccf5
- tests/unit/test_l7_orb_webull_tickcap.py:
  056267a5367cabcd9e8e870d553e9e1f14554a8b2b02be3f652a195cf3640bf1

Commit/full head, numstat and bundle hash are reported separately after commit
so the immutable committed artifact does not contain a self-referential hash.
POM L4 remains completed-copy/frozen-live-Claude-only, with no rerun.
