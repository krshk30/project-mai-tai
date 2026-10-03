# COLDSTART1 startup subscription announcements

## Requirement and independent assessment

Operator card, 2026-10-03: each enabled bot announces its current gateway
watchlist on startup, even when empty. No subscription protocol, trading rule,
paper cap, Redis persistence, or production configuration change. This build
is for independent review, not a deployment. Redis persistence stays OFF.

**Step 0: AGREE on the issue and the scoped fix.** I independently read all
five publisher paths and `MarketDataGatewayService.apply_subscription_event`
and `_restore_subscription_state` at base
`250ab18458f4d806aa8bcdf98d787fff5bb57df4`. Four publishers initially suppress
an empty list. V2 already uses a `None` debounce sentinel and publishes an
empty initial set when its existing registration/exit gates are enabled.
Momentum's weekend/holiday/pre-session return additionally bypasses its normal
subscription sync. Gateway #1084 already accepts empty replaces and writes
the full owner map, including empty owners and `_migration_complete=1`.
Its active set remains the union of every owner plus static symbols.

The build changes the first-publication decision, not the event format. An
empty replace clears only that consumer's contribution. It is **not** a no-op
against that consumer's previous nonempty set; therefore it must follow
current-state restoration, never substitute constructor-empty for unreadable
holdings. Other consumers' contributions remain unchanged.

| Consumer | Startup boundary and safety |
| --- | --- |
| strategy-engine | Existing final startup sync, after snapshot/runtime/manual-stop restoration, alert prefill and stream startup. Current symbols and fallback selection unchanged. |
| orb | Existing first universe refresh after paper lifecycle restoration; current paper-owned symbols retained. |
| orb-schwab | First universe refresh; new read-only held-entry hydration before it, including outside RTH. An unreadable ownership read aborts before any empty cleanup. Observe-only announces an empty wire set once, keeps its separate local observation filter, and never claims observed symbols. |
| schwab-1m-v2 | Existing scanner seed sync; a read-only startup ownership fence now completes before scanner/position tasks start. Watchlist union held coverage is unchanged and is never copied into entry scope. Existing registration/exit and service-enabled gates remain. |
| momentum-paper | First enabled/config-valid tick before the weekend/holiday/preparation early returns. Empty announcement does not fetch references, prepare a session, start a feed, or claim symbols. |

Publication caches advance only after ACK. An uncertain write forces the next
sync to republish current state; accepted-write/lost-ACK and cancellation are
tested. V2 serializes desired-set calculation, debounce, publication and ACK
because scanner and position polling overlap. Failed first ORB-Schwab
publication cannot fall through to an empty cleanup, including lost-ACK cases.

This repairs restart announcements, not arbitrary mid-run eviction without
any subsequent event. The separately merged #1084 full-state write remains
responsible for rebuilding an evicted owner hash on the next subscription event.

## Recorded evidence

Read-only file pulls from the production box; no Redis mutation, snapshot-batch
read, broker request, service action, or production edit was made for this build.

| Evidence | Provenance |
| --- | --- |
| Five recorded empty replaces | `/home/trader/after-hours/2026-10-03/resize-run/owners-final.json`, captured `2026-10-03T20:59:57.890696Z`, 2,535 bytes, SHA256 `66dbd12a85b5587522d68a9af381edd6e09dc815e912dc7e9da2d3ba7a780ca0`. Committed fixture `tests/fixtures/coldstart1/owners-final-20261003.json`. |
| Actual replay acknowledgement | `/home/trader/after-hours/2026-10-03/resize-run/owner-replay.json`, 374 bytes, SHA256 `a95c017d3f2f61d773eb9a5c8f6f7243e5268878a85141c5e384d20d9ecdd4a7`; five new IDs `1791064997122-0` through `1791064997122-4`, raw fields unchanged. This was the reviewed manual replay, not COLDSTART1 running live. |
| Nonempty overlapping sets | `/home/trader/after-hours/2026-10-01/option-a-owners-20261001T210055219131375.json`, 1,931 bytes. Committed fixture preserves its raw base64 events: ORB/ORB-Schwab `[NXL,VEEA]`, strategy/v2 `[NXL,SSM,VEEA]`. |

Today's five original source IDs are `1790942746316-0` (paper),
`1791014404417-0` (ORB-Schwab), `1791014405349-0` (ORB),
`1791014406227-0` (strategy), `1791037332271-0` (v2).
All were empty. Nonempty add/remove and overlap tests deliberately use the
separate October 1 capture, not invented nonempty October 3 evidence.
Test scheduling, held-only classification, failure/cancellation/lost-ACK
injection, static ownership and cap-cardinality boundaries are explicit
simulations around these recorded payloads, not historical observations.

## Scenario coverage

`tests/unit/test_coldstart1_subscriptions.py` exercises the actual publish
methods, actual startup paths with external dependencies mocked, and the actual
gateway apply/restore methods. No live orders or network calls are used.

| Card scenario | Assertion |
| --- | --- |
| Empty Redis cold boot | Five current empty announcements, five persisted fields plus marker, no upstream subscription call; both gateway-first apply and consumers-first restore. Healthy mocked startup paths complete in under 60 seconds. |
| One consumer restarts | Exactly one fresh replace for its current set; every other owner unchanged; repeated unchanged sync emits nothing. Parameterized over all five. |
| Weekend paper | Two Saturday ticks emit one empty replace, no reference fetch/session/feed; disabled paper stays inert. |
| Empty then adds/removes | Recorded nonempty October 1 sets change the union correctly; duplicate unchanged sync suppressed; empty release does not affect other owners/static. |
| Two consumers restart | Concurrent ORB/strategy announcements retain the other three entries; same-v2-instance scanner/held-coverage interleaving is separately serialized. |
| Gateway restart | #1084 restores the complete five-owner map and exact union from the hash/checkpoint and retained stream. |
| Must not break | Paper 16-symbol cap; v2 held-only coverage without entry-scope expansion; observe-only never claims symbols; disabled services and startup flag-OFF behavior; existing guard regression suite. |

**Live boot-to-five-owner latency remains UNEXERCISED.** The 60-second tests
are healthy mocked startup/convergence tests, not proof under slow DB,
reference loading or network I/O. Strategy's restoration and gateway reference
loading can delay readiness. The next install must measure all five owners
within 60 seconds and report UNKNOWN if unavailable/late, not manufacture an
empty set or claim PASS from a migration marker alone.

## Validation

The initial RED run before production edits was 16 failed / 6 passed. Failures
included four missing first announcements, weekend paper, failed-write retry,
single-owner refresh, empty-to-add, concurrent restarts and catalog/default.

Final frozen-source local validation (Python 3.12.13, macOS, `PYTHONPATH=src`):

| Run | Passed | Failed | Added failed names |
| --- | ---: | ---: | ---: |
| Independent base `250ab184` export with Git index for metadata tests | 5,050 | 56 | Baseline |
| COLDSTART1 full `tests/unit` | 5,109 | 56 | 0 |
| Focused consumer, gateway, guard and catalog regressions | 483 | 0 | 0 |
| COLDSTART1 scenarios | 59 | 0 | 0 |
| Deliberate mutations | 15 killed | 0 survivors | Not a live exercise |

Full-suite failed-name sets are byte-identical; this is **not** a green-full-suite
claim. Ruff, whitespace and log-marker isolation checks pass. The local raw
logs are `/tmp/coldstart1-release-full.txt`,
`/tmp/coldstart1-verified-baseline.txt`, `/tmp/coldstart1-release-focused.txt`
and `/tmp/coldstart1-release-mutations.txt`; `validation.json` preserves the
counts, exact 56 failed names and mutation results. GitHub validation is
separate and must pass before review/merge; this evidence supplies no pin.

Diagnostics retained: the initial base export lacked a Git index and added
one metadata-only shell failure; the indexed full baseline above removes that
harness limitation. Intermediate runs overlapped source revision and produced
stale `inspect.getsource` offsets; they are not completion evidence. The final
full run was frozen throughout. Focused strategy regressions can print existing
pending-hydration cleanup diagnostics; no corresponding test failure was added.

`run_mutations.py` changes methods only in a child process, never source files.
It tests empty suppression for each newly fixed publisher, v2 lost-ACK cache
invalidation, observe-only symbol claims, held-symbol hydration, unknown
ownership, failed-startup cleanup, concurrent debounce and startup ordering.

## Deploy boundary

`MAI_TAI_MARKET_DATA_SUBSCRIPTION_STARTUP_ENABLED=true` is the default-ON rollback
switch. The catalog checks it in **all five** processes: strategy, schwab-1m-v2,
orb, orb-schwab and momentum-paper. This adds five boolean checks; numeric
settings are unchanged. Disabling it restores the old first-empty behavior
for the four previously suppressing consumers; v2's existing initial sync stays.

No deployment, merge, restart, persistence change, or RDB fallback is authorized
by this PR. It rides the next separately reviewed RPG1/COLDSTART1 install,
which must restart all five consumers and install the matching flag catalog.
The current running services, Monday guard/checks, and RPG1 writer are untouched.
