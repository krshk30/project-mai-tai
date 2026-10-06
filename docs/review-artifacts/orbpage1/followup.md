# ORBPAGE1: owned holdings keep the display live through 16:00

PR #1100, branch `codex/orbpage1-live-orb-display`.
Scoped follow-up above the accepted head
`02c52769ed5fd0d51f0aaddaa4220af1a339b84a`; no rebase.
Main base: `3ebde364d4634fdad45992e2ab1cbdf43ffeb221`.
Independent assessment: AGREE with the requested display correction.

## Scope And Boundaries

The old `_orb_session_closed` stopped refresh unconditionally at 10:00,
even with an owned ORB position, while `_orb_within_display_session` removed
later closes from the live tape and completed-trade reconstruction.
The repository already constructs ORB positions from nonzero virtual-book
rows filtered by `strategy_code == orb_schwab` AND the configured broker
account. Shared account quantity and v2-owned rows do not establish ORB
ownership. That accepted attribution/filtering code is preserved.

- SESSION COMPLETE requires time at/after 10:00 ET AND an empty owned book.
- Before 10:00, a flat book retains the existing own-activity/UNKNOWN behavior.
- A nonempty owned book is HOLDING, regardless of missing activity telemetry.
  This claims known exposure, not fresh ticks or a healthy trading service.
- HOLDING refreshes every 30 seconds before 16:00. At 16:00 and later refresh
  pauses, but an open owned row remains HOLDING, never SESSION COMPLETE.
- Live intent/order/fill timestamp labels must belong to today and be no
  later than 16:00:00 ET. The exact boundary is included; 16:00:01 is excluded.
  The same rows feed the existing completed cycles/P&L display.
- The label parser and formatter use seconds only, without `%f`; fractional
  labels are rejected. The cutoff also explicitly clears microseconds so it
  does not accidentally preserve them if parser precision changes later.
- The old weekend shortcut could claim completion before 10:00 or despite
  owned exposure. The display now uses the same time/book rule on weekends:
  pre-10 flat remains UNKNOWN without own activity, while owned rows remain
  HOLDING. Trading session/calendar logic is outside this module's helpers
  and is not changed.
- Paper, v2, accepted labels/attribution, account API identity, and all trading
  entry/exit/cancel/ownership paths remain unchanged.

## Focused Evidence

Command (normal PATH retained after the shared venv):

```sh
env PATH=/Users/velkris/Projects/project-mai-tai/.venv/bin:$PATH PYTHONPATH=src:. python -m pytest -q tests/unit/test_control_plane.py tests/unit/test_runtime_registry.py
```

Final measured: 102 passed in 5.51s. Raw: `/tmp/orbpage1-followup-focused.log`.
The first focused pass, before fractional/weekend controls, was 98 in 5.59s.
The unchanged recorded-day fixture retains JAGX's actual two-share
6.67 -> 6.6001 completed trade and the foreign ATR strategy cycle.
New held/late-exit variants use that recorded row/price shape as counterfactual
controls; they are NOT claims that JAGX remained held past 10:00 in production.

Twenty-four added cases:

- `test_orbpage_managed_row_session_and_refresh_boundaries`: 12 held/flat
  cases at 09:59:59, 10:00:00, 10:00:01, 15:59:59, 16:00:00, 16:00:01 ET.
- `test_orbpage_foreign_or_flat_book_rows_do_not_hold_session`: v2 same-account,
  ORB wrong-account, and zero-quantity ORB rows cannot cause HOLDING.
- `test_orbpage_recorded_shape_late_exit_is_visible_through_sixteen`: exits
  at 10:30, 15:55, and 16:00 appear in intents/orders/fills and completed P&L;
  an exit at 16:00:01 does not.
- `test_orbpage_holding_render_does_not_mutate_trading_rows`: page/API reads
  leave broker order, fill, intent, and managed position state unchanged.
- `test_orbpage_weekend_display_uses_the_same_time_and_owned_book_rule`:
  four held/flat controls before and at 10:00 on a Saturday.

The existing session-boundary test is refreshed without changing its name,
including explicit zero-microsecond and fractional-label rejection assertions.
Ruff, log-marker isolation, and whitespace checks passed.

## Full UNIT And CI

Both full runs use exactly:

```sh
env PATH=/Users/velkris/Projects/project-mai-tai/.venv/bin:$PATH PYTHONPATH=src:. python -m pytest -q tests/unit
```

Fresh untouched main3eb: 6416 passed / 47 failed / 0 errors in 267.47s.
Raw: `/tmp/orbpage1-followup-unit-base.log`.
Follow-up head raw: `/tmp/orbpage1-followup-unit-head.log`.
Final head: 6447 passed / 47 failed / 0 errors in 276.21s.
All 47 failed names match exactly, with empty error sets and zero introduced
or removed failures/errors. This is +31 passing cases versus the original
base: seven accepted ORBPAGE1 cases plus this follow-up's 24 cases.
Complete comparison: `unit-pair.json`; raw `/tmp/orbpage1-followup-unit-pair.json`.
Test and source files remain unchanged between the final head
run and its commit; only derived evidence files are added afterward.
CI receipts for the exact pushed SHA are recorded in the PR description.
Review-ready requires complete failed-name parity and green final-head CI;
it does not claim independent review pin, merge, or deployment authorization.
The initial head run was stopped before the precision/weekend additions and
is not parity evidence. The final head run starts after those source/test edits.
Remote main advanced to `c21d8274fcd1d3129d61207a33dd7b002a7c9e8c`
during validation. The accepted 02c52769 ancestry is retained without rebase;
the local pair is explicitly against original 3ebde364. Final PR merge CI
separately checks integration with the newer main, not a rebased local head.

The gate1 shell processes visible during the full suite are local harness
tests: fake evaluator, temporary output, and fake curl/date; real-curl controls
target loopback only. No real cron is installed or SSH activation issued.

Only this display source, display tests, and scoped evidence are changed
above 02c52769. No production write, restart, environment/flag change,
ledger edit, merge, shared C-row, or handoff edit was performed.
