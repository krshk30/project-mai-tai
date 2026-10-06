# Reviewer Acceptance Factory

Stable import for the claude-1-owned `scripts/line_restore_acceptance.py`:

```python
from tests.line_restore_acceptance_factory import make_line_restore_case

case = make_line_restore_case(
    symbol="JAGX",
    now_ms=recorded_event_time_ms,
    settings_overrides=recorded_settings,
)
```

Run from the repository root with `PYTHONPATH=src:.` (the runner must be able
to import the repository's `tests` package). Cases must run **serially**:
the scoped historical clock temporarily replaces the strategy/service module
clock, then restores both bindings, including when a call raises.

## Interface

- `case.bot`, `case.strategy`, `case.settings`: real service, strategy and
  resolved settings. Restoration is ON; an OFF override or unknown setting
  name raises. Pass the session's other switches explicitly in
  `settings_overrides`; the factory does not pretend all combinations are ON.
- `case.add(now_ms=...)` / `case.remove(now_ms=...)`: watchlist/epoch lifecycle
  and the real strategy release/drop path. A re-add creates a new epoch, not a
  copy of the old entry permission.
- `await case.feed_bar(row, now_ms=..., phase="replay"|"live",
  source="callback"|"rest"|"streamer")`: recorded `ChartBar` or a stored-row
  mapping with timezone-aware `bar_time`, OHLCV and symbol. `callback` calls the
  real service bar handler with the supplied phase; REST/streamer call their
  real source callbacks, which determine phase themselves. No future/unclosed
  bar or foreign symbol is accepted. Feed source/arrival order from the runner's
  evidence, not sorted bar time when measuring delayed arrival.
- `case.hold(detected_at_ms=..., last_bar_age_s=..., last_print_age_s=...)`:
  real gap-hold transition. Its existing cancellation queues remain real;
  drain using the bot's normal method within `case.clock()` when the sequence
  calls for immediate delivery. No Pause classification is manufactured here.
- `case.attest(SessionCoverage(...))`: explicit caller-owned source evidence.
  The adapter does not infer coverage, prefix completeness or tape silence
  from candle count. Missing evidence leaves the actual gate closed.
- `await case.rebuild(now_ms=...)`: real off-callback service reconstruction,
  identity fences, atomic publication, confirmation evaluation and intent
  drains. It returns the service's admission result, not an oracle comparison.
- `case.snapshot()`: state/trail/age, entry readiness, incomplete reason,
  epoch, emitted buy count, all intents, ATR SELL observations, confirmation
  decisions and publications. The recording emitters keep deep copies; the
  runner must compare actual outputs to its independent expectations.
- `with case.clock(now_ms=...): ...`: call other real bot/strategy methods at
  the recorded wall time. `case.strategy._now_ms()` uses that same clock.

## Boundaries And Controls

The adapter never starts the service or opens Redis, SQL or broker connections.
Both real emission paths have recording sinks: no-buy cannot pass simply
because the emitter is absent. Only the confirmation durable-outbox boundary
is replaced with an in-memory one-shot record; the real tracker, evaluation,
publication and acknowledgment remain in use. This proves decision/delivery
behavior, **not** production SQL durability or broker execution.

Ten adapter tests pass. They exercise the real bar handler and rebuild, RETO's
recorded 11:18 trail **2.0639 / long**, explicit missing-coverage refusal,
both-leg direct-drain refusal with a protective close still delivered, a
positive control that records two permitted buys, real one-shot confirmation,
re-add epoch invalidation, historical clock restoration and input refusals.
The adapter's deliberately controlled coverage fixture is labelled synthetic;
it is **not** a historical completeness measurement.

These are interface tests, not the reviewer's population acceptance result.
The draft's first-available-bar admission, fixed worker/delivery bounds and
remaining composition/replay evidence are still open. Do not label the
45-symbol-day / 18-hold / 8-attempt population PASS from these ten tests.

## Review Contract

Review target: **Tuesday 2026-10-06 at 21:00 ET**. Reviewer-owned runner requires
100% of re-add and bars-missing cases to pass line equality, no rebuild buy and
unchanged exit decisions. The nine real pauses are reported but excluded from
this PR; Pause remains separate. Green runner plus mutations and exact-head
review permits pin/merge, then an authorized after-close install tonight.
No Wednesday deferral is assumed and no install is claimed by this interface.
