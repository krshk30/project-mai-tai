# SLOTCLEAR1 Independent Assessment

As of 2026-10-07 09:16 ET. Code base:
`f9c9bd332392e2c905fc39b954421c88970844d7`.
This is an assessment, not a build, pin request, or installation authorization.

## Verdict

- AGREE: LPCN re-add seeded a capped short segment, and its later live BUY left
  both slot claims consumed. My recorded-state diagnostic reproduces that.
- DISAGREE with the blanket cause: fresh SELL handling already clears a
  cap-only, ownerless claim in flip-owned mode. This code has its own passing
  regression test. The missing BUY release is a distinct edge.
- DISAGREE that clearing the cap alone delivers the requested fresh BUY:
  `_cw_v2_quote` explicitly disables reactive entry whenever flip-owned mode is
  enabled. This is deliberate first-rest-only behavior, not a cap check.
- UNMEASURED: complete MTEN/JAGX causal acceptance and both-broker end-to-end
  trading replay. The observed MTEN flip is confirmed, but is not proof that
  its miss shares LPCN's exact cap state.

## Independent Pull

Raw sources are `/var/log/project-mai-tai/schwab-1m-v2.log` and the PostgreSQL
`project_mai_tai.strategy_bar_history` table on `mai-tai-vps`, read October 7.
Bounded SQL used `BEGIN READ ONLY`, statement timeout 3 seconds and `ROLLBACK`.
No production state was changed.

| Symbol | Event (UTC) | Evidence |
| --- | --- | --- |
| LPCN | re-add 12:39:29.032 | Seed-cap log at 12:39:29.050: watch start `1791376769032`, old SELL bar `1791374580000`, both claims 1 |
| LPCN | BUY observed 12:40:03.325 | Bar start `1791376740000` (12:39), close 2.96, high 2.97, low 2.81, volume 609033; flip BUY, flip level 2.9535, trail 2.680298 |
| MTEN | add 12:34:20.410 | DB seed dropped prior sparse sessions; fresh warm-up logged 12:34:22.054 |
| MTEN | BUY observed 12:37:01.140 | Bar start `1791376560000` (12:36), close 1.47, high 1.50, low 1.33, volume 1009309; BUY, trail 1.095198 |

Stored LPCN 12:39 bar: open 2.83, high 2.97, low 2.81, close 2.96,
volume 609033, source `live`. Stored MTEN 12:36 bar: open 1.33,
high 1.50, low 1.33, close 1.47, volume 1009309, source `live`.

LPCN's bar OPEN predates re-add but its CLOSE follows it. The proposed close-time
freshness intentionally differs from the existing open-time discriminator.
Stored-bar provenance and live observation must still prevent a late historical
delivery from being mistaken for a new live flip.

## Own Diagnostic and Controls

On exact base f9c9bd33, a local diagnostic drove `_cap_reconstructed_segment`
then `_cw_v2_track` using LPCN's stored bar and recorded BUY signal/time:

```text
seed_cap= True True 1791376769032
recorded_buy_cap= True True owner= idle drafts= 0
counterfactual_only_cap_cleared= None
```

The last line is a controlled counterfactual, NOT a real fill/quote replay: both
cap bits were cleared, the reactive arm was prepared and a diagnostic quote
offered. The strict-mode early return still prevents a draft independently of
the quote. The source has no existing reactive first-entry producer in that mode.

Existing controls run read-only with bytecode/cache writes disabled: 3 passed
in 0.65 seconds:

- `test_fresh_sell_releases_ownerless_seed_cap_and_places_first_rest`
- `test_post_watch_buy_does_not_release_seed_cap_without_sell`
- `test_whlr_readd_does_not_reopen_flips_whose_bars_started_before_watch`

The latter two encode the old rule. The requested new card would deliberately
change them where a live bar closes after the observation boundary; it cannot
be represented as a tests-only repair or a universal slot reset.

## Scope Needed Before Build

Confirm whether a proven fresh BUY after a cap-only re-add permits a narrowly
scoped first entry through the existing reactive producer in strict mode, or
whether entry remains first-rest-only. The latter cannot meet LPCN acceptance
by clearing cap bits alone.

Any implementation must distinguish seed-only claims from a working rest,
filled/closed episode, unreadable ownership and restored retry budget. A blanket
clear would risk a duplicate or a second trade in an already-consumed segment.
Fresh SELL, stale FTFT-style SELL, restored ownership, removal, restart and both
sessions need separate controls. Indicator/seed mathematics remain untouched.

No source change, population acceptance, mutation pass or full-suite pair is
claimed. Independent assessment found a producer-policy boundary that needs
resolution before building the requested BUY behavior.
