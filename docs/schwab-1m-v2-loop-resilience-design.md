# Design: schwab_1m_v2 loop resilience (SPOF Workstream A — v2 follow-up)

**Status: DESIGN — awaiting operator review. No code until approved.** Same discipline and
two-layer pattern as the production fix (PR #249,
`docs/strategy-engine-main-loop-resilience-design.md`), adapted to v2's different loop
structure. Touches `services/schwab_1m_v2_bot.py` (+ `market_data/schwab_v2_rest_client.py`,
`settings.py`) — v2-owned files only; **no shared production code**, so this is NOT a
Pre-Merge-Regression hot-file change in the production sense, but the regression check still
runs additively.

**Origin:** Workstream A (production) shipped + survival-test-verified 2026-06-08 (100/100
injected failures survived, escalated to degraded-persistent, self-cleared, bars kept
flowing). v2 rides the **same shared Schwab token**, so it is exposed to the same failure
*family*. This doc pins v2's *actual* exposure (which differs from production's) and applies
the proven pattern. **v2 streamer flag stays OFF — this is loop resilience, not Day-1
activation.**

## 1. v2's loop structure — and why its failure mode differs from production

Production runs **one** main loop; a single uncaught exception kills it → whole-engine zombie
(no heartbeat, no bars). **v2 is different: it runs several independent asyncio tasks**
(`schwab_1m_v2_bot.py:291–300`), and `run()` does **not** await them — it
`await self._stop_event.wait()` (`:307`); the `asyncio.gather(*tasks, return_exceptions=True)`
is only in the cleanup `finally` (`:312`). The tasks are:

| Task | Loop | Schwab-touching? |
|---|---|---|
| `_heartbeat_loop` (`:508`) | `while not stop` | no |
| `_state_publish_loop` (`:521`) | `while not stop` | no |
| `rest_client.run()` → `_bar_loop` (`schwab_v2_rest_client.py:137`) | `while not stop` | **yes (REST bars)** |
| `rest_client.run()` → `_quote_loop` (`:180`) | `while not stop` | **yes (REST quotes)** |
| `_scanner_consumer_loop` (`:650`) | `while not stop` | no (Redis) |
| `_position_poll_loop` (`:566`) | `while not stop` | no (DB) |

**v2's failure mode = silent dead task, not whole-service zombie.** If a task raises an
uncaught exception, that task ends; nothing awaits it during normal operation, so it dies
**silently**. The bar loop dying → bars stop — while `_heartbeat_loop` (a *separate* task)
keeps publishing `status=healthy`. This is arguably *worse-hidden* than production's zombie:
production's single-loop death at least stopped the heartbeat (a visible signal); v2's
heartbeat survives a dead bar loop.

## 2. v2's actual escape points (pinned from the code — NOT assumed identical to production)

**Good news first — the exact 06-03 dead-token escape is ALREADY contained in v2.** Both REST
loops wrap the Schwab fetch in `try/except Exception`:
- `_bar_loop` `:151–158` — `_fetch_recent_closed_bars` (which raises `RuntimeError` on a dead
  token, `schwab_v2_rest_client.py:214/228/230/233`) → caught, log + sleep + continue.
- `_quote_loop` `:187–192` — `_fetch_quotes` → caught likewise.

So a dead/failing token at the **fetch** does NOT kill v2's loops. v2 is **less exposed to the
exact 06-03 mechanism than production was.** The remaining gaps are:

| # | Escape (unguarded) | Effect |
|---|---|---|
| E1 | `_bar_loop:177` `await self._on_chart_bar(symbol, bar)` is **outside** the per-symbol try | any raise in the bar-handling wrapper (`_handle_bar_from_rest` warmup/buffer logic, or anything not covered by the inner guards) kills the **bar loop** → bars stop silently |
| E2 | `_quote_loop:194` `await self._on_quote(...)` is **outside** the try | a raise in quote handling kills the **quote loop** |
| E3 | `_position_poll_loop:590` `self.strategy.update_position(...)` outside the fetch try | a raise kills the **position loop** (cooldown/position tracking stops) |
| E4 | `_scanner_consumer_loop:692` `_apply_strategy_state_event(...)` post-validate logic outside the inner try | a raise kills the **scanner loop** → watchlist freezes (the "v2 doesn't reset" file already showed this loop matters) |
| E5 | **No per-task backstop anywhere** | the production lesson: the *known* risky calls are guarded (`_handle_bar`/`_handle_quote`/`_maybe_emit`/`_persist_bar` each guard their sub-calls), but there is no catch-all for the **unanticipated** — a new code path, a `KeyError`, an `AttributeError` in a loop body silently kills the task |
| E6 | **No escalation / dedicated health field for loop exceptions** | the inner guards just `logger.warning` each failure; nothing tracks consecutive failures, escalates, or surfaces "this loop is failing/dead" in the heartbeat. A persistently-failing or dead task is invisible. |

**The exposure is E5 + E6, not the fetch.** v2 already survives the dead-token fetch; what it
lacks is (a) a backstop so *no* task can die from an unanticipated exception, and (b) the
visibility so a failing/dead loop is not silent.

## 3. Proposed fix — same two layers, adapted to v2's per-task structure

### Layer 1 — keep + close the per-step guards
The fetch guards stay. Close E1–E4 by moving the callback awaits inside a guard. Cleanest:
route each loop's per-iteration work through a shared helper (below) so the *entire*
iteration body (fetch + callback) is contained, and one failing symbol/iteration doesn't kill
the loop. The bar loop's per-symbol `for` already isolates symbols; we extend that isolation
to include the `_on_chart_bar` callback.

### Layer 2 — per-task backstop (the key add; v2 analogue of production's outer backstop)
A shared wrapper that runs each task's `while not stop` loop with a catch-all:

```python
async def _run_resilient_loop(self, name, iteration, *, idle_wait):
    """Run one v2 task loop so NO exception can silently kill it.
    `iteration` is an async callable for one pass; `idle_wait` waits on stop_event
    with a timeout (the existing cadence). CancelledError propagates (shutdown)."""
    while not self._stop_event.is_set():
        try:
            await iteration()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_loop_failure(name)
            logger.exception("[V2-LOOP-RECOVERED] task=%s iteration raised; loop continues "
                             "(loop_health=%s)", name, self._loop_health)
            await self._idle(self._loop_error_backoff_secs)
            continue
        else:
            self._record_loop_success(name)
        await idle_wait()
```

Each of the five `while not stop` loops is refactored to call `_run_resilient_loop` with its
existing body as `iteration`. Net effect: a task can only ever exit on `stop_event` /
`CancelledError` — **never on an unanticipated exception.** This directly removes the
silent-dead-task failure mode (E1–E5).

### Escalation + dedicated health field (E6)
Mirror production exactly, using v2's *existing* health-detail convention:
- Track `_loop_consecutive_failures: dict[str,int]` + totals per task `name`.
- After N consecutive failures on a task (`strategy_schwab_1m_v2_loop_persistent_failure_threshold`,
  default 3) → `loop_health = "degraded-persistent"` + one-shot
  `logger.error("[V2-LOOP-DEGRADED-PERSISTENT] task=%s …")`. Single transient → `recovering`,
  clears on success.
- Add `loop_health` / `loop_exceptions_total` / `loop_failing_tasks` to the heartbeat
  `details` — **alongside the existing `data_flow` field, NOT folded into the status
  Literal.** v2 already does exactly this for `data_flow` (`_evaluate_data_flow:346`, comment
  at `:363–366` explicitly says the status Literal is shared and severity goes in the detail).
  `loop_health` slots in beside it. The two are complementary: `data_flow` = "bars not
  flowing" (symptom); `loop_health` = "a loop is throwing" (cause).

### Env-tunable knobs + fault-injection hook
- `strategy_schwab_1m_v2_loop_error_backoff_seconds` (default 1.0)
- `strategy_schwab_1m_v2_loop_persistent_failure_threshold` (default 3)
- `strategy_schwab_1m_v2_loop_fault_injection_count` (default 0 / OFF) — when > 0, the next N
  bar-loop iterations raise a synthetic `RuntimeError` on the **proven E1 path** (inside
  `_on_chart_bar`, post-fetch) so the survival test reproduces v2's real escape on demand.
  Self-clears after N.

## 4. v2-specific differences from production (explicit, per your ask)

1. **Multi-task, not single-loop** → the backstop is applied **per task** via
   `_run_resilient_loop`, not as one outer `try`. Five wrap sites, not one.
2. **`run()` fire-and-forgets the tasks** (`await stop_event.wait()`, gather only on cleanup)
   → the failure mode is **silent dead task**, and the dedicated `loop_health` field is the
   primary visibility (the heartbeat task is separate and always alive, so a dead bar loop
   won't show as a missing heartbeat the way production's did). **Optional defense-in-depth
   (open question §7):** have `run()` also supervise task liveness (detect/relog/optionally
   re-spawn a task that somehow ends). The per-task backstop already makes unintended exits
   impossible, so this is belt-and-suspenders.
3. **v2 already guards the dead-token fetch** → unlike production, the exact 06-03 mechanism
   is already contained at the fetch. This design's value for v2 is the *catch-all backstop*
   (the production lesson: don't rely on having guarded every known call) + the *escalation/
   visibility* that v2 lacks entirely.
4. **v2 already has the `data_flow` detail + status-not-overloaded pattern** → `loop_health`
   reuses that established convention rather than introducing a new mechanism. No change to
   the existing `data_flow` watchdog (it stays — complementary).
5. **Risk/blast-radius is lower** — v2 is paper, flag-OFF, not Day-1-active. Deploy + survival
   test are even lower-risk than production's.

## 5. Tradeoffs and edge cases

1. **CancelledError must propagate** — catch `Exception`, not `BaseException`; re-raise
   `CancelledError` so `run()`'s cleanup (`task.cancel()` + gather) still works. Explicit test.
2. **Masking a real bug as a quiet retry loop** — same mitigation as production: always
   `exc_info`, counters, `loop_health=degraded-persistent` (loud) + backoff (no hot spin).
   Never exit the loop.
3. **Backstop heartbeat recursion** — N/A for v2 the way it was for production: the heartbeat
   is its OWN task with its own guard (`_heartbeat_loop:511`), independent of the failing
   task. A bar-loop backstop firing doesn't touch the heartbeat path. (Cleaner separation than
   production had.)
4. **Per-task vs per-symbol granularity in `_bar_loop`** — the bar loop iterates symbols in a
   `for`. Keep the per-symbol fetch guard (one bad symbol doesn't block others); the per-task
   backstop wraps the whole pass as the catch-all. Decide whether `iteration()` = one full
   round-robin pass or one symbol (open question §7).
5. **`data_flow` vs `loop_health` interaction** — both can be `degraded` simultaneously
   (e.g., bars stalled AND a loop throwing). They're independent detail fields; the dashboard
   (Workstream B) should surface both. No collision (separate keys).

## 6. Tests (additive, new file `tests/unit/test_schwab_1m_v2_loop_resilience.py`)

1. `test_resilient_loop_contains_exception_and_continues` — `iteration` raises; loop does not
   exit; failure recorded.
2. `test_resilient_loop_propagates_cancellederror` — `iteration` raises `CancelledError`;
   re-raised, not counted. (Shutdown guard.)
3. `test_persistent_failures_escalate_then_recover` — N consecutive → `degraded-persistent`;
   success → `healthy`.
4. `test_heartbeat_carries_loop_health_fields` — after a failure, `_publish_heartbeat`
   details include `loop_health` + counters, status Literal unchanged, **`data_flow` still
   present** (no regression to the existing watchdog field).
5. `test_bar_loop_survives_callback_exception` — **E1 reproduction**: `_on_chart_bar` raises
   (post-fetch); the bar loop survives, records failure, keeps polling.
6. `test_bar_loop_survives_dead_token_fetch` — regression guard: confirms the existing fetch
   guard still contains a `RuntimeError` from `_fetch_recent_closed_bars`.
7. `test_quote_loop_survives_callback_exception` — E2 reproduction.
8. `test_fault_injection_raises_then_self_clears` — the hook raises N times on the bar-loop
   callback path then clears.

Plus: existing v2 tests (`tests/unit/test_schwab_1m_v2_bot.py`) still pass (the `data_flow`
watchdog and warmup/gating behaviour are untouched).

## 7. Open questions for the reviewer

1. **`iteration()` granularity for `_bar_loop`** — wrap one full round-robin pass (simpler) or
   one symbol (finer isolation)? Recommendation: one pass; per-symbol fetch guard already
   isolates symbols.
2. **Task-liveness supervision in `run()`** (difference §4.2) — add it as defense-in-depth, or
   rely solely on the per-task backstop? Recommendation: rely on the backstop for this PR; note
   supervision as a possible later hardening.
3. **Fault-injection target** — inject on the E1 callback path (reproduces v2's *real* remaining
   escape) vs on the fetch (reproduces the already-guarded path). Recommendation: E1 callback
   path — that's the gap this PR closes.
4. **`loop_health` field naming** — `loop_health` vs `task_health`? (production used
   `main_loop_health`; v2 has no single "main" loop, so `loop_health` reads better.)

## 8. Out of scope
- v2 streamer Day-1 activation (flag stays OFF — separate step, separate decision).
- Workstream B (dashboard surfacing of `loop_health`/`data_flow`/dead-token) — separate; this
  design only *populates* the field B will render.
- Any change to the existing `data_flow` watchdog, warmup gating, or strategy logic.
- Production `strategy_engine_app.py` (already shipped in PR #249).

---

**End of design proposal. Awaiting operator review before any code.**
