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

**UNMEASURED:** whether Schwab advances REST `quoteTime` only when NBBO changes,
and whether AMOD's particular old value was stable NBBO, a delayed response, or
a missed poll. Neither a 40,151-share bar nor LEVELONE receipt time proves that.
Do not substitute local poll-receipt time or a trade timestamp for the age of the
ask used to approve a buy stop. This change retains the original REST quote time
and the 10,000 ms limit.

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

Results: 29 new cases pass, and 467 tests pass across this file, the
`test_schwab_1m_v2*.py` suites, entry-window, flip-owned-entry and RETRY_ONE suites.
All five mutation controls fail as required. Ruff on the new test file and
`git diff --check` pass. The full unit suite was not run for this scoped addition.

This is an addition to RPG1, not completion of its broker replacement work.
The 19 reported long-no-feed cases have not all been individually replayed.
End-to-end composition with NFQ1's OMS deferred hold remains unverified here;
the test proves both leg intents and identity, not broker acceptance.
No live installation, main merge, or first-session zero-full-bar-wait result is
claimed. The broader RPG1 assessment and PR must retain these limitations.
