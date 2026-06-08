# Design: strategy-engine main-loop resilience (SPOF Workstream A)

**Status: DESIGN — awaiting operator review. No code until approved.** Same discipline as
fix v3 (design doc → review → code). This file touches `strategy_engine_app.py`, a shared
hot file, so the implementation PR will require the mandatory Pre-Merge Regression Check.

**Origin:** the 2026-06-03 and 2026-06-07 strategy-engine zombie outages. A
dead/failing Schwab token (06-03) and an uncaught streamer-side `RuntimeError` (06-07) each
killed strategy-engine's main loop while the process stayed `active` — a zombie that darkened
the whole bot fleet (incl. Schwab-independent polygon_30s) until a manual restart. Full
incident records: the 2026-06-05 / 06-07 / 06-08 entries in `docs/session-handoff-global.md`,
and memory `[[project-mai-tai-context-operational-reference]]` (2026-06-05/06-07 SPOF notes).

This is **a doc, not code.**

## 1. The bug being fixed — proven mechanism

`StrategyEngineService` runs its work in a single `while not stop_event.is_set():` main loop
(`strategy_engine_app.py:5876–5958`). **The entire loop body has no try/except.** Every
`await` in it is unguarded:

```
5877  await self._read_stream_group(...)
5884  await self._drain_market_data_stream(...)
5888  await self._drain_schwab_stream_queues()
5891  await self._publish_intent(intent)
5899  await self._monitor_schwab_symbol_health()
5915  await self._immediate_schwab_1m_history_refresh(symbols=stalled)   # Schwab REST
5925  await self._refresh_stale_schwab_1m_history()                      # Schwab REST
5937  await self._sync_subscription_targets()        # (already guarded internally — see §3)
5938  await self._publish_strategy_state_snapshot()
5954  await self._sync_subscription_targets()        # at the 08:00 scanner roll (5949→5954)
5957  await self._publish_heartbeat("healthy")
```

If **any** of these raises an uncaught exception, the loop coroutine propagates it, the task
dies, and the loop never iterates again. The process stays alive because the Schwab streamer
runs as an **independent asyncio task** — so it keeps reconnecting and logging, masking that
the main loop is dead. That is the zombie: `active (running)`, NRestarts=0, but no
`snapshot batch processed`, no heartbeat, `mai_tai:strategy-state` frozen, market-data
`active_symbols→0`, whole fleet dark.

### Pinned escape points (per occurrence)

- **06-03 (dead token):** at the 08:00 scanner roll and/or the bar-flow-stall recovery, the
  loop calls `_immediate_schwab_1m_history_refresh` (5915) / `_refresh_stale_schwab_1m_history`
  (5925). Both `await self._load_schwab_history_bars(...)` (e.g. line 7362) — a Schwab REST
  call through the broker adapter — with **no internal try/except**. A dead token makes the
  adapter's `_get_access_token` raise `RuntimeError: failed refreshing Schwab token` (the
  `unsupported_token_type` / `invalid_grant` we saw). It propagates out of the history-refresh
  function → out of the unguarded loop body → loop dies. The death coincided with the 08:00
  roll because that's when warmup/hydration/refresh activity peaks against the dead token.
- **06-07 (streamer-side RuntimeError):** the same architectural gap, different trigger — an
  uncaught `RuntimeError: Schwab CHART_EQUITY channel stale...` / `TimeoutError` surfaced into
  the loop body during the cold-start refresh/drain window and killed it (~05:48 UTC). Token
  was healthy that day (~4 fails). This proves the fragility is **any uncaught exception in the
  loop body, not just a dead token.**

**Correction to earlier notes:** `_sync_subscription_targets` was previously named as the
escape point. It is **not** — it is already guarded (see §3). The real escapes are the
*other* unguarded awaits, principally the two `schwab_1m` history-refresh calls.

## 2. Design goal

A Schwab token failure — or any exception from a loop-body step — must **degrade gracefully**,
not zombify the process:

1. **Main loop stays alive** — one bad iteration (or one bad step) never ends the loop.
2. **Keeps heartbeating** — `_publish_heartbeat` continues so the service is observably alive
   (and observably *degraded*), never silently dead.
3. **Non-Schwab bots keep running** — a Schwab REST failure must not stop polygon_30s bars or
   market-data draining. (polygon_30s going dark on 06-03 is the signature we must prevent.)
4. **Failure is surfaced, not masked** — every caught exception is logged with `exc_info` and
   counted; repeated failures drive a `degraded` heartbeat + a dashboard-visible dead-token/
   main-loop-error state (ties into Workstream B).

## 3. How this composes with the existing 05-22 guard

The 05-22 scanner-stall fix added `_bounded_subscription_sync_step` (`:6696–6716`), which wraps
each subscription-sync sub-step in `asyncio.wait_for(...)` + `except (TimeoutError, Exception)`
and **continues to strategy-state publication**. `_sync_subscription_targets` (`:6660`) uses it
for both sync steps and spawns hydration as a fire-and-forget background task. So that path is
already contained. (`_run_init_phase` has its own bounded guards at `:6006/6024` — init only.)

**The gap the 05-22 fix did not cover:** the bounded guard lives *inside one function*. The
*other* loop-body awaits — the two history-refresh REST calls, `_drain_schwab_stream_queues`,
`_monitor_schwab_symbol_health` — have no equivalent. This design **extends the same proven
pattern** to those, and adds an **outer loop-body backstop** for anything unanticipated.

**Defense in depth (two layers):**

- **Layer 1 — per-step bounded guards (primary, granular).** Wrap each remaining Schwab-touching
  loop-body await in the same `_bounded_*` pattern (bounded timeout + `except Exception` →
  log + continue). This is what lets the *rest of the iteration still run* — most importantly
  `_publish_heartbeat` at the end — when one Schwab step fails. This is the mechanism that keeps
  non-Schwab work (market-data drain, polygon bars, heartbeat) alive. Mirrors 05-22 exactly, so
  it's a known-safe pattern, not a novel one.
- **Layer 2 — outer loop-body backstop (catch-all, absolute).** Wrap the whole loop body in
  `try: ... except Exception:` → log `exc_info`, increment counter, `continue`. This guarantees
  **no exception can ever end the loop**, even one we didn't anticipate or guard per-step. It is
  the zombie-proofing backstop; Layer 1 is the graceful-degradation surface.

## 4. Proposed change (sketch — not final code)

Layer 1 — extend the bounded pattern to the unguarded Schwab steps, e.g.:

```python
# was: recovery_bar_count = await self._immediate_schwab_1m_history_refresh(symbols=stalled)
recovery_bar_count = await self._bounded_loop_step(
    "schwab_1m immediate history refresh",
    self._immediate_schwab_1m_history_refresh(symbols=stalled),
    default=0,
)
# similarly for _refresh_stale_schwab_1m_history, _drain_schwab_stream_queues,
# _monitor_schwab_symbol_health
```

where `_bounded_loop_step` returns a default on `TimeoutError`/`Exception`, logs with
`exc_info`, and bumps `self._main_loop_step_failures[label]`.

Layer 2 — wrap the loop body:

```python
while not stop_event.is_set():
    try:
        ... existing body ...
    except asyncio.CancelledError:
        raise                      # MUST propagate — shutdown depends on it
    except Exception:
        self._main_loop_exception_count += 1
        self._last_main_loop_error = (utcnow(), repr-ish)
        self.logger.exception("[MAIN-LOOP-RECOVERED] iteration raised; loop continues")
        await self._publish_heartbeat("degraded")   # stay observably alive + degraded
        await asyncio.sleep(self._main_loop_error_backoff_secs)  # avoid hot-spin
        continue
```

New state/config:
- `self._main_loop_exception_count: int`, `self._last_main_loop_error`
- `self._main_loop_step_failures: dict[str, int]`
- `Settings.strategy_main_loop_error_backoff_seconds` (default e.g. 1.0)

## 5. Surfacing the failure (ties into Workstream B)

- **Log signatures:** `[MAIN-LOOP-RECOVERED]` (outer backstop fired) and a per-step
  `failed <label>; continuing` (Layer 1) — both with `exc_info`. Greppable, distinct from the
  streamer's `connection loop failed`.
- **Heartbeat payload:** add `main_loop_exceptions_total`, `last_main_loop_error_age_secs`, and
  per-step failure counts to the heartbeat details. When the backstop is firing or a step keeps
  failing, publish `status="degraded"` (the bounded path already does this conceptually).
- **Dashboard (Workstream B overlap):** the dead-token surfacing Workstream B will add should
  read these heartbeat fields so a token failure shows as a visible "Schwab token failing /
  main loop degraded" banner instead of being buried in tracebacks. Workstream A makes the
  state *survivable + measurable*; Workstream B makes it *visible + auto-recoverable*. They
  compose; A is the prerequisite (a zombie can't heartbeat its own degradation).

## 6. Tradeoffs and edge cases (design-first audit)

1. **Masking a real bug as a silent retry loop.** A broad `except Exception` could turn a
   persistent crash into an invisible busy-loop. Mitigations: always `exc_info`; a counter +
   `degraded` heartbeat (loud, not silent); the backoff sleep prevents a hot CPU spin; consider
   a threshold (N consecutive failing iterations → escalate log level / louder alert) — but
   **never exit** (a degraded-but-alive loop is strictly better than a zombie). Open question
   on the exact threshold (§9).
2. **CancelledError must NOT be caught.** Catch `Exception`, not `BaseException`, and re-raise
   `CancelledError` explicitly. Shutdown (`stop_event` + SIGTERM via `_run_init_phase`/
   `_shutdown_cleanup`) depends on cancellation propagating. This is the single most important
   correctness constraint — get it wrong and the service won't stop cleanly.
3. **Partial-iteration state.** If a step fails mid-iteration, some work already ran (e.g. an
   intent published, a sub-sync done). The loop is designed to be re-entrant per iteration
   (it re-reads streams, re-syncs, re-publishes). Audit needed: confirm no loop-body await
   leaves cross-iteration state inconsistent when skipped. Layer-1 granular guards reduce this
   risk (only the failing step is skipped, the rest of the iteration completes normally).
4. **Heartbeat itself raising.** If `_publish_heartbeat` raises (Redis down), the outer backstop
   catches it, but then the degraded-heartbeat in the handler would also raise. Guard the
   handler's own heartbeat publish so the backstop can't recurse into an exception.
5. **Backoff vs latency.** A backoff sleep on every error iteration adds latency to recovery
   when the error clears. Keep it small (~1s) — enough to avoid a hot spin, short enough that a
   transient error doesn't materially delay the next good iteration.
6. **v2 is a separate process.** The operator directive is "fix across ALL bots."
   strategy-engine's loop covers macd_30s/schwab_1m/polygon_30s. `schwab_1m_v2` runs its own
   service (`schwab_1m_v2_bot.py`) with its own loop — it needs the **same** Layer-2 backstop
   applied to its loop. Open question whether to do both in one PR or sequence (§9).

## 7. Proposed unit tests (mirroring fix-v3 rigor)

New file `tests/unit/test_strategy_engine_main_loop_resilience.py`:

1. `test_history_refresh_raising_does_not_kill_loop` — patch `_load_schwab_history_bars` to
   raise `RuntimeError("failed refreshing Schwab token: unsupported_token_type")`; assert one
   loop iteration completes, `_publish_heartbeat` still called, loop proceeds to the next
   iteration. **The 06-03 reproduction.**
2. `test_streamer_side_runtimeerror_does_not_kill_loop` — a loop-body await raises
   `RuntimeError("Schwab CHART_EQUITY channel stale...")`; loop survives. **The 06-07 repro.**
3. `test_cancellederror_propagates_and_stops_loop` — inject `CancelledError`; assert it is NOT
   swallowed and the loop exits (shutdown still works).
4. `test_non_schwab_work_continues_when_schwab_step_fails` — Schwab REST raises; assert
   market-data drain + polygon bar handling + heartbeat still execute that iteration (Layer-1
   granularity proof — polygon stays alive).
5. `test_repeated_failures_escalate_to_degraded_not_exit` — make a step raise every iteration;
   assert heartbeat goes `degraded`, counter climbs, loop never exits, backoff applied (no hot
   spin).
6. `test_heartbeat_carries_main_loop_exception_counters` — assert the new fields appear in the
   heartbeat payload after a caught exception.
7. `test_heartbeat_failure_in_handler_does_not_recurse` — `_publish_heartbeat` raises inside the
   backstop handler; assert no unhandled recursion / loop still survives (edge case §6.4).
8. `test_v2_loop_backstop` (if bundled) — same Layer-2 guarantee for `schwab_1m_v2_bot`.

Plus a regression guard: existing strategy-engine tests still pass (the bounded 05-22 path is
untouched; this only adds outer/again-inner guards).

## 8. Falsifiable post-deploy verification

There's no 04:00-roll "verdict window" here — the proof is the **next time a Schwab token dies
or a streamer exception fires.** Falsifiable prediction:

- When the token next fails, strategy-engine **does NOT zombie**: `snapshot batch processed`
  continues, heartbeat continues (as `degraded`), `mai_tai:strategy-state` keeps advancing,
  market-data `active_symbols` stays non-zero, **polygon_30s bars keep flowing**.
- `[MAIN-LOOP-RECOVERED]` appears in the log at the moment of the failure (surfaced), with the
  heartbeat flipping to `degraded` and `main_loop_exceptions_total` climbing.
- No manual restart required to keep the fleet alive; recovery of the *Schwab* path still needs
  the token fixed (re-auth + Workstream B auto-reload), but the **fleet no longer goes dark**.
- To exercise it safely before a real token death: a staging/one-off test that injects a raising
  `_load_schwab_history_bars` and confirms the loop survives + heartbeats degraded.

## 9. Open questions for the reviewer

1. **Layer scope:** both layers (granular per-step guards *and* outer backstop), or outer
   backstop only? Recommendation: **both** — the outer backstop alone would skip the rest of the
   iteration (incl. heartbeat sometimes) on any failure; the granular guards are what keep
   non-Schwab work + heartbeat alive. Cost: more surface area to touch in a hot file.
2. **Escalation threshold:** should N consecutive failing iterations escalate (louder alert /
   distinct status), or is `degraded` + counter enough? Never-exit is fixed regardless.
3. **Backoff value:** default `strategy_main_loop_error_backoff_seconds` — 1.0s proposed.
4. **v2 in the same PR or sequenced?** Same Layer-2 backstop applies to `schwab_1m_v2_bot`'s
   loop. Bundle (one "all bots" PR) or land strategy-engine first then v2?
5. **Status enum:** heartbeat `status` is a strict Literal (per the v2 watchdog note
   `[[project-mai-tai-schwab-1m-v2-watchdog]]`). `degraded` already exists; confirm it's the
   right signal for "main loop catching exceptions" vs adding a `data_flow`-style detail field.

## 10. Out of scope

- **Workstream B** (auto-reload token on `invalid_grant` + dashboard dead-token banner) — separate
  PR; this design only *surfaces* the state B will consume.
- Fixing the Schwab token/REST itself, or the token's abnormally-fast death (suspected
  Schwab-side revocation — its own investigation).
- The accepted ~20/hr case-2 reconnect residual (fix v3 — decided: accept, no tuning).
- polygon_30s persist-lag growth (parked, Schwab-independent).
- Any change to `schwab_streamer.py` (the streamer's own reconnect handling is correct; the bug
  is strategy-engine not surviving exceptions, not the streamer raising them).

---

**End of design proposal. Awaiting operator review before any code.**
