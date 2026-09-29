# RETRY-ONE per-SELL-cycle change

## Rule and live evidence

The former rule spent two closes per symbol per 04:00 session. The operator's 2026-09-29 rule spends two closes (first entry and one retry) per ATR SELL segment. A second close blocks the remainder of that segment, including its BUY flip. The next *live* SELL creates a new zero-count segment. An unknown segment or unreadable budget admits no first rest.

BKYI on 2026-09-29 is the observed counterexample to the old rule. In `/var/log/project-mai-tai/schwab-1m-v2.log`, the v2 process reported one close and release at 11:02 ET, a second close and hold at 11:23 ET, a SELL flip at 12:11 ET, and `retry_budget_exhausted` first-slot refusals starting 12:15 ET. The code followed its then-current daily specification; the specification did not match the operator's per-cycle intent. This patch changes the rule, not the live interpretation of those past events.

## Safety and migration

- Budget snapshots now carry the ATR SELL bar's timestamp as `segment_id` and the count for that segment. Restore selects the newest segment and never reduces its count if a late write arrives for the same segment.
- The first-rest ownership record carries its causal retry segment and count at placement, so a late close cannot spend the next segment's allowance. Count persistence precedes ownership release; a retry after a crash cannot count the same close twice.
- Legacy day-scoped snapshots and ownership records have no provable cycle identity. They hold first-rest admission until a fresh, observed SELL establishes a new segment. Replayed or pre-watch SELL bars cannot mint a cycle.
- The scanner's actual watch-add time, not the first strategy-state allocation, bounds a re-add's observed segment. The existing seed-cap and Webull mirror admission gates remain in place.

## Verification and historical-study limit

The base-compatible BKYI test was RED on unchanged `origin/main` at `fc68b238`: the next SELL still refused its first rest after two earlier closes. The initial branch's focused retry and flip-owner suite passed 86 tests; its full unit suite had exactly the same 47 failed test names as that untouched base (4,481 passes on the branch versus 4,468 on the base). The branch was subsequently rebased onto `90106fb4` after unrelated ORB changes reached main; that new base requires fresh independent review.

The previous `CAUSAL_BACKTEST.md` covers 2026-08-24 through 2026-09-23 and groups opportunities by day/name, not by an observed ATR SELL-cycle ID. Its 163 trips / 253 broker trades / 144 winners / -5.2979 sum return percent at one daily retry are **not** a per-cycle estimate. A valid requested 08-24..09-29 comparison still needs SELL-cycle attribution to each filled trip and a fresh-cross proof for each counterfactual added trip. The production v2 log rotations available on 09-29 begin on 08-31, and `[V2-ATR-PROBE]` is not a complete recorded cycle ledger for every historical symbol. Therefore exact added-trip names, winners and return for the full window are **UNKNOWN**; no return or fill is imputed from a bar high. The operator waived this informational P&L comparison; it is not used as a safety or approval claim.

## Late SELL audit requested in review

The 11 SELL bars first probed more than 120 seconds late during 09-22..09-29 were checked against the v2 watch-add/remove log and the first `[V2-ATR-PROBE] flip=SELL` for each exact `ts_ms`. Times below are ET. A watch start after the SELL bar means the segment was reconstructed; the existing seed-cap path already prevents a first rest. The operator waived the informational P&L comparison, not this live-admission check.

| Symbol/date | SELL bar | First SELL probe | Watch evidence | Could the old live path place a first rest for this SELL? |
| --- | --- | --- | --- | --- |
| IMCC 09-22 | 12:48 | 13:37:58 | Removed 10:35; next added 13:20:30, after the bar | No, pre-watch reconstruction |
| IMCC 09-22 | 13:28 | 14:04:41 | Removed 13:27:24; next added 13:37:58, after the bar | No, pre-watch reconstruction |
| WETO 09-24 | 08:06 | 09:04:36 | Added 09:04:35 | No, pre-watch reconstruction |
| PFSA 09-24 | 08:36 | 09:04:36 | Added 09:04:35 | No, pre-watch reconstruction |
| YMAT 09-24 | 10:11 | 10:59:04 | First added 10:46:21, then removed/re-added | No, pre-watch reconstruction |
| YMAT 09-24 | 10:50 | 10:59:04 | Watched from 10:46:21, removed 10:51:03; re-added 10:59:04 | No actual live flip processing before removal; replay was seed-capped |
| JAGX 09-25 | 08:45 | 09:31:46 | Added 09:31:46 | No, pre-watch reconstruction |
| IPST 09-25 | 13:00 | 13:53:35 | Added 13:53:35 | No, pre-watch reconstruction |
| ONFO 09-28 | 18:05 | 18:58:03 | First added 18:58:00 | No, pre-watch and outside entry window |
| ONFO 09-28 | 18:46 | 18:58:03 | First added 18:58:00 | No, pre-watch and outside entry window |
| MSGY 09-29 | 09:35 | 09:44:17 | Removed 09:23:42; re-added 09:38, removed 09:43, re-added 09:44:17 | No, absent at the SELL bar and seed-capped on re-add |

Raw evidence: `/var/log/project-mai-tai/schwab-1m-v2.log-20260923.gz`, `schwab-1m-v2.log-20260925.gz`, `schwab-1m-v2.log-20260926.gz`, `schwab-1m-v2.log-20260929`, and `schwab-1m-v2.log`. The first four are 09-22, 09-24, 09-25 and 09-28 UTC-day rotations; the last is 09-29. The active-watch YMAT exception is not inferred merely from age: the exact 10:50 `ts_ms=1790261400000` has no probe before its 10:51:03 removal, first probes at 10:59:04, and the 10:59 re-add logs `[V2-CW-SEED-CAP]` for that SELL. Thus 0/11 is a demonstrated old-path first-rest opportunity removed by the live-only reset.

Review follow-up tests also pin two fail-closed seams: a replayed SELL cannot borrow a prior zero-close segment's budget, and a failed zero-count reset write marks the budget unreadable before any admission. Both pass normally and fail when the reviewer-specified guard or unreadable assignment is individually removed.

After these tests, 94 focused retry/flip-owner/seed-cap/SELL-stamp tests passed; Ruff and `git diff --check` passed. The full macOS unit run before rebase had 4,483 passes and the same 47 host/tooling failure count reported on the prior head, with no RETRY-ONE failure. On rebased base `90106fb4`, the focused 94 passed again and the full run had 4,639 passes, 47 failures in the known host/tooling areas, and no RETRY-ONE failure. The exact FAILED-name comparison to the new base is for independent review; it is not inferred from the equal failure count.
