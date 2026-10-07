# LINESRC1 Source Assessment

[codex] Sole-writer isolated branch `codex/linesrc1-anchored-session-poll`,
created from actual remote main `4805ddc81184c76b4d5cef5c483c809edb666fe6`.
Remote identity was checked before creation; later main movement is not a rebase
or installation permission. No production, service, DB, broker API or token
writes. Production and merge/install are owned by parent and require exact pin.

## Own Bounded Recorded Count

Read-only service configuration identified the retained file
`/var/log/project-mai-tai/schwab-1m-v2.log`. A single `tail -n 4000` read produced
1742 lines. Own frozen raw path:
`/tmp/linesrc1-step0-retained-v2-tail4000.log`.
SHA256 `78635eabcb27a245c2af3fa01375d8fe5b69f4d60a96e99c06b605d6dff7b975`.
Own parser receipt `/tmp/linesrc1-step0-counts.json`, SHA256
`e3c33b6b5b9664701fee11e75879274eafeb2cc596f4e20a7b5447f1308438d1`.

Frozen timestamp range: `2026-10-07 00:00:05.344` to
`2026-10-07 00:11:08.069 UTC`, equivalently October6
`20:00:05.344` to `20:11:08.069 ET`. This is a bounded retained-file count,
NOT a full-day count, source response census or evidence of market silence.

| Symbol | Anchored poll error headers | Paired tracebacks |
| --- | ---: | ---: |
| OLOX | 11 | 11 |
| SMXT | 11 | 11 |
| BIYA | 11 | 11 |
| APUS | 11 | 11 |
| BURU | 1 | 1 |
| IPDN | 11 | 11 |
| MTEN | 10 | 10 |
| MOBX | 11 | 11 |
| Total | 77 | 77 |

All77 final exceptions are `ValueError: current closed candle absent` and
all77 headers are outside06:55<=ET<16:00. Headers and their traceback frames
are paired, not counted as separate failures. No foreign/duplicate error was
observed in this bounded capture; that is NOT proof such errors never occur.

## Code Cause And Verdict

AGREE on the source-scoped remedy. The base bot's `_line_source_request` admits
up to16hours after04:00, not just the declared polling window. The provider
raises when its last candle differs from the requested latest closed minute.
The anchored loop treats that as an epoch failure and uses `logger.exception`
on every occurrence. Recorded frames identify that exact exception path.

Inside-window sparse/pending-current behavior is verified through controlled
payloads built from retained candle prices, NOT a new live provider pull.
Real provider completeness, market silence and historical fills remain
UNMEASURED. Token reads/refreshes, source GETs, services and Redis/DB actors are
not exercised in production by this lane.

## Released Build Boundaries

Only the anchored full-session source polls from06:55 inclusive to16:00
exclusive ET. The bot uses its current clock; the client/parser also fence the
declared poll minute. Outside calls do not fetch, validate, invalidate epochs,
or emit source warnings/cycle logs. Ordinary recent-bar/quote paths, cadence,
four-request concurrency and quota formula are unchanged.

An in-window response missing the current close returns actual validated bars
with `proof=None`, not a fabricated full-session proof. It is no new bar, not
a failed epoch: no strategy/persistence current-bar callback and no coverage
invalidation. A same-epoch source-wait fence blocks first/reclaim/RPG/draft
entry readiness until a valid current response arrives. A prior rebuild cannot
clear this fence; stale-epoch responses cannot set or clear a new owner's wait.

Foreign/duplicate/malformed/truncated responses remain real source errors and
invalidate coverage. One WARNING per symbol/state transition replaces the
traceback flood. Recovery is observable, repeated identical states are quiet.
No reset, gap-span, R6 trade-evidence, ten-clean-live-bar or entry assertion is
relaxed. The reviewer-owned runner/factory and its recorded population are not
edited. Default restoration flag/configuration is unchanged.
