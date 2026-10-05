# PMREST1 - assessment and replay evidence

Assessed independently before building on main e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89,
2026-10-05 08:02-08:06 ET. C1-C4 AGREE; A1 REAL for the Schwab boundary;
A2 ORB latency UNMEASURED. No production write or restart was performed.

## Independent assessment

| Claim | Verdict | Own evidence and denominator |
| --- | --- | --- |
| C1 software rest is disarmed on reprice | AGREE | `_queue_resting_cancel` clears active/line/trigger/thin streak, but emits no broker cancel for a software rest. The next bar re-arms. RPG1 requires an existing broker order and RTH admission. |
| C2 SAIQ gaps today | AGREE | 07:45:02.368 disarm to 07:46:02.617 arm = 60.249 s (8.3599 to 8.2381); 07:47:02.102 to 07:48:02.463 = 60.361 s (to 7.9699). New triggers 8.2793 and 8.0097. |
| C3 population has a bar-long hole | AGREE; fresh denominator differs | 31 retained v2 files, sessions 09-09..10-05, own 08:02 census: 130 reprice disarms, 127 paired rearms; min 57.194 s, median 60.169 s, max 240.072 s; 0/127 under 10 s, 118/127 in 55-65 s. Three additional moves vs the earlier reviewer snapshot. |
| C4 tape reached a new trigger in a hole | AGREE, tape-high only | The later 08:06 DB/log census contains 129 pairs (two more while building), 129/129 with bot bars. One high crossing: TNON 09-10, disarm 07:46:02.275 at old 3.5714, rearm 07:48:02.462 at 3.5454; bar 07:47 high 3.57. This does not prove an eligible or lost fill. |
| A1 unchanged software rest crosses 09:30 | REAL for Schwab; Webull is not necessarily unwatched | DCOY 09-23 arm 09:28:02.888 level 4.6717, Webull parallel software cross 09:31:58.896 px 4.68, primary broker place 09:35:03.378 at the same level. Code refuses the primary EH cross in RTH; no broker conversion without a later reprice/flip. Assessment only; this PR does not fix the unchanged-rest boundary. |
| A1 repriced software rest crosses 09:30 | COVERED | Old code disarms then places next bar. With this flag, normal RTH placement runs in that evaluation, including configured window, live bar, current ask, sizing and first-entry admission. Refusal leaves the old software rest armed. |
| A2 ORB cancel-to-replace latency | UNMEASURED | Zero completed ORB reprice marker events in retained OMS logs; one ORB Schwab submitted order row since 09-30. Code previews then calls broker PUT replacement/readback, not a next-bar scheduler; no measured latency claim is possible. |

Raw production paths: `/var/log/project-mai-tai/schwab-1m-v2.log*`,
`/var/log/project-mai-tai/oms.log*`; DCOY specifically
`schwab-1m-v2.log-20260924.gz`, TNON `schwab-1m-v2.log-20260911.gz`.
Own local pulls: `/tmp/pmrest1-own-rest-logs.txt`,
`/tmp/pmrest1-gap-assessment.json` (08:02),
`/tmp/pmrest1-own-db-evidence.json` (08:06),
`/tmp/pmrest1-boundary-dcoy.txt`, `/tmp/pmrest1-own-orb-reprices.txt`.
The 129 DB census is later than the 127 initial census; do not combine denominators.
Committed replay evidence is `tests/fixtures/pmrest1/recorded.json`: bot bars,
original arm/disarm lines and the captured Schwab TNON quote at
11:47:30.993Z, received 11:47:32.114201Z, bid 3.54/ask 3.55/last 3.55.
Pre-09-24 legacy `soft-rest at level` means effective trigger; the TNON replay
uses offset zero for that historical state, not today's offset on yesterday's level.
Adversarial callback order, thin streak and owner-state tests are explicitly controlled.

Reads were low-priority, read-only SQL with a five-second statement timeout;
per-gap bars LIMIT 5, fixture bars LIMIT 25, quote LIMIT 10. No Redis snapshot
payloads were read. Two schema-query errors during the pull were corrected;
neither performed a write. C4 remains a minute-high observation, not a fill replay.

## Implementation fence

Default OFF flag `strategy_schwab_1m_v2_pm_rest_reprice_enabled`, catalog expected ON
only at the reviewed deploy. The two existing threshold checks call one helper.
An active software rest changes only line/trigger; no cancel, new admission,
intent, persistence, owner reset or slot consumption in the EH move. Both
callbacks run synchronously on the service event loop without an await in the move.
One `[V2-RESTING-EH-MOVE]` carries old/new values and gap_ms=0. The enabled probe
adds broker/software classification; disabled probe output remains unchanged.
Broker rest and flag-OFF behavior retain the original cancel/RPG1 path.

Reclaim's existing initial EH admission is RTH-only. The narrow exception manages
an already-active software reclaim when PMREST1 is ON; it does not arm a new EH
reclaim or enable the live reclaim switch. This latent state is explicitly tested.
The 0.5% move, trigger offset, band, 600/300 sizing, exits and NFQ1 are unchanged.

## Named tests

All names below are in `tests/unit/test_pmrest1.py` (32 cases).

| Scenario | Test | Outcome |
| --- | --- | --- |
| P1 SAIQ two real moves, first/reclaim | `test_p1_saiq_moves_same_bar_without_a_disarm_or_intent` | PASS |
| P2 captured TNON quote crosses moved trigger | `test_p2_tnon_recorded_tick_crosses_new_trigger` | PASS, REST and stream primary path |
| P3 callbacks around move never see unarmed | `test_p3_quote_before_and_after_move_never_sees_unarmed` | PASS, REST and stream |
| P4 under 0.5% no-op | `test_p4_sub_half_percent_move_is_noop` | PASS |
| P5 third thin bar still cancels | `test_p5_move_preserves_thin_streak_then_third_thin_cancels` | PASS, initial streak 1 and 2 |
| P6 flip grace unchanged | `test_p6_long_flip_grace_and_soft_rest_no_fill_unchanged` | PASS |
| P7 holds/removal/window | `test_p7_move_cannot_make_a_held_rest_live`; `test_p7_other_cancels_keep_their_disarm_reason` | PASS; boot/gap states controlled, not a full SDEV incident replay |
| P8 all state/accounting after 1/2/10 moves | `test_p8_moves_preserve_all_accounting_and_rest_state` | PASS first/reclaim; dataclass comparison excludes only line/trigger |
| P9 OFF reproduces original SAIQ gap | `test_p9_flag_off_reproduces_saiq_one_bar_gap` | PASS; settings default FALSE pinned |
| P10 intents/sizing equal unchanged rest | `test_p10_cross_drafts_match_unchanged_rest_with_600_300_sizing` | REST both legs PASS; stream unchanged-defect characterization, NOT both-leg delivery PASS |
| A1 repriced RTH boundary | `test_rth_move_places_both_brokers_same_evaluation_without_cancel` | PASS |
| Safe ending | `test_failed_preparation_or_rth_admission_leaves_old_rest`; `test_trigger_preparation_exception_keeps_the_old_rest` | PASS |
| Scope edges | `test_rth_broker_reprice_still_uses_existing_cancel_path`; `test_flag_on_does_not_enable_initial_premarket_reclaim`; `test_configured_cutoff_still_disarms_a_moved_rest` | PASS |

### Separate finding: existing stream Webull sizing defect

The real TNON tick replay exposes a pre-existing defect in `on_stream_trade`:
it verifies an ask but creates `Quote(ask_price=0.0)` before the shared cross
builder. Webull dollar sizing then has a non-positive price and queues no draft;
the primary Schwab intent exists. The flag-OFF unchanged-rest control has the
same result. REST polling queues both legs (600/300). This PR does NOT fix the
stream ask propagation or claim two streamed legs pass; independent operator
assessment is required for that separate repair. Do not hide this under P10.

## Regression proof

Eight isolated in-memory source mutations, each run against the recorded replay
suite, all RED: move replaced with cancel; thin streak reset; old trigger retained;
feature flag ignored; RTH placement removed; broker-only fence removed; first site
still cancels; reclaim site still cancels. Raw `/tmp/pmrest1-mutations.txt`.
No production source was edited for mutation runs.

Final focused: **927 passed**, including 32 PMREST cases and existing
RPG1/NFQ1/composition, retry/flip-owner, reclaim, resting mirror and FLAGGATE tests.
Raw `/tmp/pmrest1-focused-final3.txt`.
Full units on macOS/Python 3.12: main **56 failed / 5,372 passed**;
final frozen head **56 failed / 5,404 passed**. Normalized failed-node sets
are identical (diff rc0); no new failure. These 56 failures are baseline,
not a claim the full suite is green. Raw `/tmp/pmrest1-baseline-unit.txt`
and `/tmp/pmrest1-head-unit-final3.txt` (214.43 s).
Mutation final rerun: 8/8 RED, `/tmp/pmrest1-mutations-final.txt`.
Initial full run identified the new catalog cardinality and probe-field exposure
checks; both were repaired. An extra cutoff test initially omitted the session
anchor/configured close; that fixture is now explicit, not a production rule change.
An intermediate concurrent run was invalidated by editing this module while its
source-inspection tests were still running (old code line offsets read newer file
contents); the final comparison uses the frozen source and is the release result.
Ruff and log-marker isolation PASS. Linux CI must be green before pin.
