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

The base-compatible BKYI test is RED on unchanged `origin/main` (`fc68b238`): the next SELL still refuses its first rest after two earlier closes. The branch's focused retry and flip-owner suite passed 86 tests; the full unit suite had exactly the same 47 failed test names as the untouched base (4481 passes on the branch versus 4468 on the base). Ruff and `git diff --check` passed.

The previous `CAUSAL_BACKTEST.md` covers 2026-08-24 through 2026-09-23 and groups opportunities by day/name, not by an observed ATR SELL-cycle ID. Its 163 trips / 253 broker trades / 144 winners / -5.2979 sum return percent at one daily retry are **not** a per-cycle estimate. A valid requested 08-24..09-29 comparison still needs SELL-cycle attribution to each filled trip and a fresh-cross proof for each counterfactual added trip. The production v2 log rotations available on 09-29 begin on 08-31, and `[V2-ATR-PROBE]` is not a complete recorded cycle ledger for every historical symbol. Therefore exact added-trip names, winners and return for the full window are **UNKNOWN**; no return or fill is imputed from a bar high. Keep this PR draft until the retrospective evidence is resolved or the operator explicitly accepts this limitation after independent review.
