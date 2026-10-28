# Narrow PRICE1 PM stream validated-ask repair

Task PRICE1-PM-STREAM-VALIDATED-ASK-PROVENANCE-20261022. Separate branch/worktree
from exact group1 `cac98b036e0663911c7582b7f7af112446c99623`, no L11 source.
Galileo owns ONLY the narrow core slice and new controls; Avicenna retains all
other PRICE wire/envelope files. Main composes and owns runtime/review/install.

## Cause and production diff

`SchwabV2Strategy.on_stream_trade` already rejects a missing/nonfinite/nonpositive
ask and an ask above the existing cap. The service additionally passes only a
remembered ask within its existing ten-second cache bound; the core's flag-ON
confirmation path retains its own existing semantics. After those checks, Quote
construction used `ask if pm_print_ask_confirm else 0.0`. The unconditional PRICE1
check in `_eh_resting_cross_check` therefore rejected healthy flag-OFF entries.

Only that Quote ask assignment changes to `ask_price=ask`, with one provenance
comment. It is the ACTUAL ask just validated, not trigger/cap/trade/line substitution.
All floor/freshness/price-cap/pre-market/session/country/line/slot/ownership/hold/
settle-latch/no-duplicate gates are unchanged. No L2/OptionA hook or L11 code added.

Call graph: existing LEVELONE parser/service cache -> `_offer_stream_trade_to_eh_rest`
-> `strategy.on_stream_trade` ask+cap validation -> Quote -> actual
`_eh_resting_cross_check` PRICE1/other guards -> primary draft and configured Webull
fanout -> unchanged publication/routing. Stream raw35/native service requirements
and service cache freshness remain untouched. No adapter or OMS change here.

## Controlled source controls, ALL UNRUN

New15 = flag-OFF/ON actual cross retaining exact5.57 ask with primary600/Webull300
draft witnesses and no duplicate; six missing/zero/NaN/infinite/past-cap/sub1 ask
refusals; four boot/position/line/country guard refusals; actual service/parser
cache exactly10000ms positive vs10001ms negative; raw sub1 line cannot be rescued
by an above1 trigger/cap/ask. Existing helper fixtures are reused but old test
functions are not invoked. Service external publication ports are CONTROLLED;
no draft witness implies native acceptance/fill/protection.

Proposed first NEW15 after main review/exact claim/head/fresh paths/inventory:

```sh
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u DATABASE_URL -u MAI_TAI_DATABASE_URL PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q --tb=short -o addopts= -p no:cacheprovider tests/unit/test_price1_pm_stream_validated_ask.py --basetemp=/private/tmp/mai-tai-price1-pm-ask-new15-20261022 --junitxml=/private/tmp/PRICE1-PM-ASK-NEW15-20261022.xml
```

## Exact old13 reconciliation risk, not hidden fixture edits

Main's triage identifies13 baseline-PASS regressions. This source fixes their
primary-entry zero-placeholder cause, but NOT a claim all13 now pass. Two existing
tests explicitly characterize the Webull zero-ask loss that retaining the real ask
also fixes:

- `test_pmprint1_pmflip1::test_t10_flag_off_reproduces_saiq_false_buy_and_zero_ask_webull`
  expects no Webull leg and a price0 log.
- `test_pmrest1::test_p10_cross_drafts_match_unchanged_rest_with_600_300_sizing[True]`
  expects no Webull leg specifically because the old stream Quote lost the ask.

These files/assertions are NOT edited here. Main must review their retained
characterization contract explicitly; do not weaken PRICE1 or reintroduce zero to
make them green. Other13 selectors remain exactly in the retained triage report.
No new runtime/no unchanged old run is authorized by this source card.

## Preserved L11 checkpoint

C8345 source and main first40 actual37P/3F receipt remain preserved in the separate
L11 WT; nothing lost/canceled. After this urgent PM handoff, author resumes the
three C preflight/fixture failures with source proof of real unstarted vs active
writer state, retaining native refusal. No37 automatic repeat. All final
composition/native/performance/PRICE1 real momentum-wire gates remain separate.
