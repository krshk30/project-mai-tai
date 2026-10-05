# HEALTHLATCH1 - independent assessment, 2026-10-05

Assessment completed before source edits, from my own read-only SSH pull at
06:33 ET and code read at main 124c5f9fcbc81a6f45e8fa8aafd5d5fb8b55d907.
Production HEAD read independently: bbb4360453eb38570a3eb988122d73f97a886a61.
No production checker was run: it writes the shared cursor files.

| Claim | Verdict | Own evidence |
| --- | --- | --- |
| C1: permanently RED, only paper-policy key standing | AGREE | cron.log last GREEN 2026-09-18 07:25:01 EDT; 4,883/4,883 following summaries RED. latest.txt has 19 GREEN rows and only paper-policy RED. paged.active has only that fingerprint. |
| C2: historical condition was real | AGREE | Decompressed all retained paper logs. Each of the eight files 20260922/23/24/25/26/29/30/20261001 has 23 cooloffs. One recovery at 2026-09-25 08:00:07.177Z (04:00 ET). Last cooloff 2026-09-30 13:32:52.498Z. The earlier 20260919 archive has another 9 cooloffs, outside that eight-session denominator. |
| C3: stale since Option A | AGREE | Current paper log is 321 bytes, two session/feed lines; 20261003/04/05 archives/current file have zero cooloffs and zero recoveries. Since October 1, 1,229/1,229 cron summaries RED. |
| C4: cannot clear | AGREE | No src writer of MOMENTUM-PAPER-FEED-POLICY. Check clears its persisted true latch only on decision=recovered. Independent state read shows momentum_policy_active=true and paper offset=321, so zero new bytes perpetuates RED. |
| C5: no trading reader and no historical masked gateway pages | AGREE, historical scope | No src reader of fleet-health state. Eight gateway archives September 21-30 contain 4/4/4/4/5/4/4/4 policy-close lines, total 33. alert.log has matching 33 sent gateway pages in those sessions, plus inactive pages for all nine units over retained history. This does not prove absence of a latent future paging race. |

Raw sources on mai-tai-vps: /home/trader/fleet_health/cron.log, latest.txt,
socket_evidence_offsets.json, paged.active, alert.log; root crontab;
/var/log/project-mai-tai/{momentum-paper,market-data}.log* (gzip decompressed).
Today's state had market-data device=64769/inode=790859/offset=514870 and
paper device=64769/inode=836839/offset=321. The original card's earlier
market offset=506700 is replayed by the regression fixture as well.

## N2 - separate latent defect, assessed only

**REAL latent paging defect; historical missed delivery UNMEASURED.** Both
root */5 cron entries are present. bar_gap_watch_cron runs the whole checker
and retains only its v2-bar-continuity line. That invocation still advances
socket_evidence_offsets.json and restart_counts.json, with no cross-caller
lock. A sequential local reproduction on the unmodified checker, using the
real vendor close signature, produces:

```text
bar-gap caller first: RED new_matches=2 bytes_scanned=91 active=1
pager caller second: GREEN new_matches=0 bytes_scanned=0 active=0
```

The two matches are received/sent in one close line, not two kicks. If the
bar-gap caller wins, it discards the RED and the pager can miss it. The
retained 33/33 pages do not falsify this ordering-dependent defect. No caller,
locking or cursor-ownership change is included here, per the assessment-only
scope. This is not caused by removing the paper latch.

## Scope and compatibility

Remove only the retired paper socket-policy leg. Keep the exact gateway
needle and incremental/rotation logic; all nine runtime units and both
LIVE_MONEY checks stay intact. Old paper cursor/latch entries are accepted
and discarded on the next atomic state write, not hand-edited on the box.
Missing state retains today's first-read behavior; corrupt/unreadable state
retains today's RED fail-closed behavior. No new Option A feed alarm.

The old fifth-close/cooloff test is retired because the vendor connection
and its recovery writer no longer exist. Its negative/positive detector
coverage moves to a test proving old paper policy lines are not read at all;
independent once-only paging, later RED admission, and selective recovery
are re-pointed to real gateway/OMS/inactive conditions, not deleted.

If paper ever regains its own vendor connection, that PR must restore a
matching alarm and recovery writer together.

## Verification

74 focused tests PASS: test_healthlatch1, test_fleet_health,
test_fleet_health_cron and test_fleet_health_bar_continuity. Ruff and marker
collision checks PASS. AST comparison confirms only the socket-state reader,
writer and socket-policy check changed; the existing corrupt-state test is
byte-for-byte unchanged. No src, cron wrapper or bar-gap wrapper diff.

| Scenario | Named test(s) |
| --- | --- |
| S1: exact old state + today's 321-byte paper log; clean summary/exit | test_today_exact_stale_state_and_paper_log_summary_green[True-19] and [False-24] |
| S2: stale paged fingerprint removed silently | test_stale_momentum_fingerprint_drops_without_page_or_alert_log_change |
| S3: real gateway close appended; one page | test_appended_real_gateway_policy_close_pages_exactly_once; test_socket_evidence_is_incremental_and_a_new_1008_rearms_after_a_clean_sample |
| S4: paper unit down still RED/pages | test_inactive_expected_service_is_red_even_without_a_prior_sample; test_momentum_unit_inactive_still_pages_once |
| S5: standing gateway RED cannot mask later OMS RED | test_existing_gateway_red_does_not_mask_a_later_oms_red |
| S6: state corruption/missing retains prior behavior | test_corrupt_socket_evidence_cursor_fails_closed_without_replaying_old_logs (unmodified); test_missing_socket_state_keeps_existing_first_read_behavior |
| S7: new log inode | test_gateway_rotation_keeps_new_policy_close_red |
| S8: 19 runtime / 24 full rows, exactly one continuity row in full mode | test_today_exact_stale_state_and_paper_log_summary_green[True-19] and [False-24]; test_runtime_only_executes_no_market_function_checks |
| S9: old paper policy logs are never read | test_old_paper_cooloff_logs_are_never_read; test_retired_paper_policy_markers_do_not_latch_or_clear_gateway_evidence |

Selective recovery and independent once-only pages are preserved by
test_oms_recovery_clears_only_oms_fingerprint_while_gateway_stays_red and
test_momentum_restart_storm_and_gateway_1008_are_independent_once_only_pages.
Cleanup preserves the gateway cursor, pinned by
test_stale_state_cleanup_preserves_gateway_cursor_without_replaying_old_close.

Mutation evidence, applied individually and restored before the full run:

- Original checker: exact-state/no-paper-read tests RED (3 failures).
- Keep old cursors with updated=dict(prior): RED (3 failures).
- Serialize momentum_policy_active=true again: RED (4 failures).

Local raw results: /tmp/healthlatch1-red.txt,
/tmp/healthlatch1-mutation-cursor.txt, /tmp/healthlatch1-mutation-latch.txt,
/tmp/healthlatch1-baseline.txt, /tmp/healthlatch1-final.txt.
The unchanged main baseline is 56 failed / 5,362 passed (217.82 s). Final
full unit suite is 56 failed / 5,372 passed (208.64 s). The failed-name sets
are identical: zero added, zero removed. Both runs have 311 warnings.
These are local environment/baseline failures, not a claim that all tests
passed. GitHub Validate results must be checked on the pushed exact head.

No install or restart authorized by this report. The install plan is for
independent review and a subsequent operator install GO, not execution now.
