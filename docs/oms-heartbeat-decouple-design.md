# OMS heartbeat flap — decouple the liveness signal from the sync loop (DESIGN)

> **Design-first (OMS main loop = shared hot path, real-money adjacent). No code in this doc —
> review before any PR.** Addresses the 07-20 watchdog flap (3 RED trips, each self-recovered
> <1 min). [[project_mai_tai_oms_liveness_watchdog]] [[project_mai_tai_oms_zombie_blocking_db]]

## The mechanism (confirmed 2026-07-22, not assumed)

The OMS `run()` loop is single-threaded and does, in order, every iteration
(`oms/service.py` ~lines 438-509):

1. read + handle strategy intents;
2. **every ~5s: `await sync_broker_state()`** — which `await`s `list_account_positions` for
   **each of the 12 active broker accounts** in series, plus `sync_broker_orders`;
3. still inline: `_window_flatten_armed_stops`, `_v2_overnight_flatten`, native-guard re-arm;
4. **every `service_heartbeat_interval_seconds`: `await _publish_heartbeat(...)`**.

**The heartbeat publish sits AFTER the broker sync in the same loop.** So any slowness in step 2
— a slow Schwab/Webull round-trip, a briefly-hung connection — delays step 4. The liveness
watchdog trips RED when the published heartbeat is >180s stale. That is the flap: not a zombie
(the loop is alive, `NRestarts=0`, 24 log-lines/min throughout), just the signal arriving late
because it is queued behind a slow sync.

**The 6 dead-credential accounts are NOT the 180s cause.** `live:polygon_30s`,
`live:webull_30s`, `paper:{macd_30s_reclaim,orb,schwab_1m,schwab_1m_v2}` fail the Alpaca
credential check **immediately** (no network) and just spam a WARNING each pass. They add loop
work and log noise but cannot themselves push the heartbeat past 180s. The real coupling is
structural: heartbeat behind sync in one loop.

## The fix — decouple the heartbeat into its own task (recommended)

Publish the heartbeat from an **independent `asyncio.Task`** on its own timer, so a slow (or
briefly hung) broker sync can never delay the liveness signal. The heartbeat becomes a pure
"the process's event loop is turning" signal — which is exactly what the watchdog needs — and
stops being an accidental proxy for "the broker sync finished quickly."

Sketch (for review, not final):
- a small `_heartbeat_loop()` coroutine: `while not stop: await _publish_heartbeat(...);
  await asyncio.sleep(heartbeat_interval)`;
- started as a task in `run()` alongside the existing consumer/sync loop; cancelled on shutdown;
- the existing inline heartbeat block in the sync loop is **removed** (the task owns it now).

### Why this over "prune the dead accounts"
Pruning reduces the account count but leaves the coupling — a single slow *live* account round-trip
still delays the heartbeat. Decoupling removes the failure mode entirely. Pruning is a worthwhile
**secondary** cleanup (below), not the fix.

## Edge cases / overlap audit (the parts that need care)

1. **The heartbeat must NOT become a liar.** If it publishes "healthy" on its own timer while the
   MAIN loop is actually wedged, we would mask a real zombie — the exact failure the SPOF work
   fought. Mitigation: the heartbeat task publishes a **monotonic counter that the main loop
   increments each iteration** (e.g. `_main_loop_ticks`). The task reads that counter; if it has
   not advanced since the last publish, it publishes **`degraded`/stale**, not `healthy`. So the
   signal stays honest: "loop turning" is proven by the counter, not assumed by the timer.
2. **Shutdown ordering.** The heartbeat task must be cancelled and awaited in the same
   stop-path as the consumer loop; a lingering task publishing after shutdown is a phantom-alive
   signal. Mirror the existing task lifecycle.
3. **`_run_db` interaction.** `_publish_heartbeat` currently runs on the loop; confirm it carries
   no DB `await` that itself blocks (it publishes to Redis). If it does touch the DB, route it
   through `_run_db` so a stalled DB cannot wedge the heartbeat task — otherwise we have just
   moved the coupling.
4. **Two publishers.** During the transition, ensure the inline block is fully removed so the
   heartbeat is published from exactly one place (the task). Two publishers racing the same
   `last_heartbeat` stamp would be a subtle bug.
5. **Watchdog threshold unchanged.** 180s stays; the fix makes the signal timely, it does not
   move the goalpost.

## Secondary cleanup (independent, low-risk, separate PR)

Silence the 6 dead-account credential WARNINGs: either (a) mark those accounts inactive if they
are genuinely dead, or (b) demote the missing-credential log from WARNING-per-pass to a one-shot
per account per process. This is log-hygiene + a small reduction in per-pass work; it is **not**
the flap fix and should not be conflated with it.

## Test plan (when built)
- Unit: a `_publish_heartbeat` that sleeps 200s must NOT delay a second heartbeat (prove the task
  is independent). Pin the counter-staleness rule: main-loop counter frozen → next publish is
  `degraded`, not `healthy` (mutation-check: remove the counter check → a wedged loop publishes
  healthy → red).
- Survival: run the OMS with a deliberately slow `list_account_positions` stub; the heartbeat
  cadence must stay flat.

## Status
Design only. No code. Awaiting review before a PR touches the OMS main loop.
