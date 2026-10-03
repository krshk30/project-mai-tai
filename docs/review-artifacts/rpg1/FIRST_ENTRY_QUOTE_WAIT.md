# RPG1 first-entry stale-quote addition

## Independent assessment

**AGREE** with the issue and the first-entry-only hold-and-recheck change.
The operator explicitly limited this addition to `slot=first` in the side
conversation on 2026-10-02. Reclaim is unchanged: its existing armed BUY segment
must not be replaced with a short-only eligibility rule.

AMOD on 2026-10-02 at 13:38:02.672 ET logged `stale_quote` for both configured
legs, with age 12,325 ms against 10,000 ms, trail 3.360737, short age 31 and
completed-bar volume 40,151. This was not an owner-phase refusal. The independently
captured lines are in
`/Users/velkris/.codex/study-evidence/rpg1-nfq1-20261002/logs.ndjson`, referring to
`/var/log/project-mai-tai/schwab-1m-v2.log` lines 36094-36097. The capture ends
before the session close; it is not a recount of all 360 reported holds.

### Which timestamp

The entry check reads `Quote.quote_time_ms`. `SchwabV2RestClient._fetch_quotes`
copies Schwab REST `quote.quoteTime` into that field. `_quote_loop_pass` calls
the bot for every returned quote; it does not suppress unchanged bid/ask values.
The configured polling interval defaults to five seconds, plus request/processing
time. A new callback therefore does not necessarily contain a newer quote.

LEVELONE is a separate path. The subscribed fields are `0,1,2,3,4,5,8,9,35`;
35 is parsed as trade time, while captured quote ticks use the stream envelope's
timestamp. `_handle_stream_tick` does not replace the RTH strategy's `last_quote`.
Its ask cache feeds the separate pre-market stream-cross path.

**Independent review replication:** own log read and 120 new Massive quote
requests reproduce the reviewer's counts exactly, using `/tmp/qtime.py`'s cohort
selection and its one-second grace after Schwab `quoteTime`:

| Market updates after quoteTime + 1 second | Holds | Share |
| --- | ---: | ---: |
| No quote update | 46 | 38.3% |
| Updates, bid/ask prices unchanged | 38 | 31.7% |
| Bid or ask price changed | 36 | 30.0% |

Two limitations matter. The script takes the last 120 in sorted **file** order,
not timestamp order: the cohort actually spans September 23 15:09:02.611 ET to
October 1 15:15:03.456 ET, and includes no October 2 AMOD example. Its labels also
exclude the first second after `quoteTime`. With that margin removed, the same
120 records classify as 1 no-update, 70 prices-unchanged updates, 49 price-changed.
All 120 requests succeeded and had an earlier quote establishing a price basis.
The evidence supports stale REST snapshots both with and without later price
changes; it does not prove the vendor's general timestamp-update policy, nor
AMOD's particular underlying cause. "Size-only" is the reviewer's shorthand for
unchanged bid/ask prices; quote condition/exchange changes are not ruled out.

Reproducer: `verify_quote_time.py`; cutoff fixed at the review script's mtime,
`2026-10-02T20:30:07.460567Z`. Raw, credential-free evidence:
`/Users/velkris/.codex/study-evidence/rpg1-nfq1-20261002/quote-time-review-fa194cbe.json`,
SHA256 `e51c2660f2279e987e82ed60b163363417451d551ea38a3168d2567d2cac3164`.
Largest reply was 543,059 bytes (4 MiB enforced cap); no Redis reads or production
writes. Do not substitute local receipt time or trade time for ask age. The
original REST `quoteTime` and 10,000 ms limit remain unchanged.

## Implemented behavior

- First-entry stale or missing-timestamp evidence creates only an ephemeral wait,
  not an order, a resting latch, or a consumed slot.
- A quote callback rechecks the existing bar, liquidity, short-state, owner,
  RETRY_ONE and price gates. Both direct broker-leg queues are drained in that
  callback; no new bar is needed to emit the intents.
- A fresh ask at/above the trigger keeps the pending entry waiting. A later fresh
  ask below the trigger may authorize it only while the original opportunity is
  still eligible. A confirmed BUY flip or segment change discards it.
- A line move at the existing reprice threshold re-derives the pending price;
  smaller moves keep the pending line. No price band or sizing rule changes.
- Watch removal, session reset, entry cutoff, stale bar, liquidity loss, or an
  occupied/consumed slot clears the wait with one `action=gave_up` line.
  The existing close-time sweep also clears waits when quotes stop entirely.
- Restart restores no pending quote waits. A subsequent bar must derive a new
  eligible entry. Pre-market soft rests and reclaim remain on their old paths.
- The legacy `no_quote_fail_open` path remains unchanged for an initial entry
  with no quote, as explicitly requested. An existing stale-price wait never
  falls through to that exception, nor uses a nonpositive/nonfinite ask.
- `action=queued` and `waited_ms` measure hold-to-intent latency, **not broker
  acceptance or fill latency**. Live broker latency remains to be measured.

## Verification and limits

The new test file is `tests/unit/test_rpg1_pending_first_quote.py`. AMOD's recorded
hold inputs are replayed; subsequent quotes and invalidations are explicitly
counterfactual test inputs, not invented historical broker answers.

Covered: quote-only resume; unchanged old quote; exact 10-second edge; missing
time; nonpositive/nonfinite ask; trigger-at/above-ask recovery; BUY flip; segment
change; liquidity loss; cutoff; stale bar; removal; close-time sweep; latest-line
selection at the 0.5% boundary; reclaim exclusion; restart; pre-market; RETRY_ONE;
both leg queues and shared slot/segment identity, emitted only once.

RED control: the corrected AMOD quote-only test fails on b8b0dafb with no intent.
In-memory mutations removing quote resume, freshness, segment expiry, the
first-slot restriction, or quote-callback draining are each detected. These runs
do not modify application files for mutation testing.

Initial results at fa194cbe: 29 new cases pass, and 467 tests pass across this file, the
`test_schwab_1m_v2*.py` suites, entry-window, flip-owned-entry and RETRY_ONE suites.
All five mutation controls fail as required. Ruff on the new test file and
`git diff --check` pass.

Review M1/M4/M5 now have six extra cases (35 total). They assert **discarding** the
wait, one reasoned log, and no resurrection after the blocking state clears:
occupied/held/consumed slot (M1), boot/gap hold (M4), and watch removal while the
flip-owner state must remain UNKNOWN (M5). These lines are not redundant: later
entry gates block a current order but do not retire the stale opportunity.
`check_first_quote_mutations.py` makes all three mutations RED in memory without
editing application source. The full-suite SLOT2 fixture bypasses `__init__`;
it now explicitly initializes the new per-instance pending dictionary. No
production fallback or weakened assertion was added for that fixture.

Full `tests/unit` rerun on the same host/interpreter, without skips or xfails:

| Tree | Passed | Failed | Additional FAILED names vs base |
| --- | ---: | ---: | ---: |
| Isolated Git checkout of b8b0dafb | 4,936 | 56 | - |
| This branch, including six review tests and the SLOT2 fixture update | 4,971 | 56 | **0** |

The 56 matching failures are 44 fanout acceptance installer tests, nine
sync-checkout tests, logrotate and unattended-upgrade shell tests, and one
Momentum dead-consumer timeout. Host-tooling diagnostics include missing
`/usr/bin/sha256sum` and shell-selected Python lacking `type | None` support;
the Momentum timeout is a reproduced baseline timing failure, not called a
proven tooling cause. No unrelated failure was suppressed. An initial
archive-only base added a Git-index assertion failure; the reported comparison
uses a real isolated Git checkout instead. The initial branch run exposed the
three SLOT2 fixture initialization errors, which are fixed and absent on rerun.

Raw full logs/JUnit and M1/M4/M5 mutation output:
`/Users/velkris/.codex/study-evidence/rpg1-nfq1-20261002/first-quote-review-20261002/`.
`head-final-tests.txt` and `base-final-tests.txt` contain the complete FAILED lists;
sorted names compare byte-identical. M1 kills three cases, M4 two, M5 one. Ruff
on the touched tests and verification scripts, and `git diff --check`, pass.

This is an addition to RPG1, not completion of its broker replacement work.
The 19 reported long-no-feed cases have not all been individually replayed.
End-to-end composition with NFQ1's OMS deferred hold remains unverified here;
the test proves both leg intents and identity, not broker acceptance.
No live installation, main merge, or first-session zero-full-bar-wait result is
claimed. The broader RPG1 assessment and PR must retain these limitations.
