# Fleet health-validation system — "function-not-process"

**Status:** DESIGN-FIRST (design + first checks now; build incrementally after operator approves each).
**Author:** session 2026-07-07. Grounded in a read-only map of `/home/trader/project-mai-tai` + the `/home/trader/` ops scripts.
**Premise:** the OMS-liveness watchdog (built 07-05) proves a service's **loop still stamps a heartbeat**. This layer proves each service is **actually doing its job** — the "process alive ≠ functioning" gap. The motivating class is the blocking-event-loop freeze: the paper strategy-engine freeze and the OMS zombie are the *same* class, and a functional check must catch a loop that has stopped doing work — even if (especially if) it still reports `status=healthy`.

---

## 0. Correction to a prior note (07-06 watchdog pages)

I earlier hypothesized the 5 self-recovered "NO heartbeat" pages on 07-06 were false positives from reading a MAXLEN-capped stream by `COUNT`. **Ground truth disagrees:** `mai_tai:heartbeats` is `maxlen=redis_heartbeat_stream_maxlen=1000` (`settings.py:79`), carries only 5 fixed-cadence publishers (~0.30 events/s), and live `XLEN=229`; `COUNT 200` therefore spans **~11 min** — `oms-risk` at a 15s cadence cannot age out of that window unless it genuinely stopped. So the 07-06 pages were most likely **real brief OMS heartbeat gaps around the two attended deploys** (#391 11:21 ET restart choreography, #392 15:27 ET) — i.e. the watchdog alerting correctly, not falsely.

The window concern is nonetheless **real but latent**: `COUNT 200` on `XLEN 229` already excludes the oldest ~29 entries, and the margin shrinks if publishers are added or cadence tightens (reconciler at 30s falls off first). **This design's Redis-read helper fixes it structurally** (§3), so the new layer never inherits the latent bug.

---

## 1. What already exists (reuse, don't rebuild)

**Heartbeat envelope** (`events.py:20-30, 275-284`): `source_service`, `produced_at` (all freshness derives from this), `status` ∈ {starting, healthy, degraded, stopping} (a **strict shared Literal** — services must not invent values), and a free-form **`details: dict[str,str]`** where all functional signal lives. Publishers → `mai_tai:heartbeats` (maxlen 1000): strategy-engine, oms-risk, market-data-gateway, reconciler, schwab-1m-v2 (all 15s except reconciler 30s). **Not on the stream:** ORB (publishes `mai_tai:strategy-state-isolated` only, and its `data_health` is **hardcoded "healthy"** — no real watchdog), polygon_30s (folded into strategy-engine).

**Functional signal per service, today:**
| service | strongest existing functional signal | can it report healthy-while-frozen? |
|---|---|---|
| strategy-engine | `main_loop_health`/`main_loop_failing_steps`/`main_loop_last_error_age_secs` (`strategy_engine_app.py:6418`) + persisted `strategy_bar_history.bar_time` | **yes** — self-reported loop-health can freeze mid-value if blocked inside an await → needs an EXTERNAL bar-freshness cross-check |
| oms-risk | heartbeat carries **only** `adapter`+`providers` — no functional field | **yes** — watchdog proves only that it still beats |
| market-data | `active_symbols` count; external `market_capture_trades.received_at` (~2s live) | partially |
| reconciler | **best-instrumented:** `total_findings`, `critical_findings`, `run_status`; external `reconciliation_runs.completed_at` | low — this is the model |
| schwab-1m-v2 | **richest beat:** `data_flow`, `secs_since_last_bar/quote`, `loop_health`, `[V2-TASK-DIED]` | low |
| ORB | isolated-state `bar_counts`/`last_tick_at`/`recent_decisions`; `data_health` hardcoded | **yes** — no real watchdog, not on heartbeats |
| stops (cross-cutting) | `oms_managed_positions` says which positions *should* be armed | **armed-stop state is OMS in-memory only** (`_armed_hard_stops`), not persisted, not in any beat, and Webull native STOP is rejected so nothing in `broker_orders` → **zero external observability that the sole safety net is armed** |

**Ground-truth DB tables for external cross-checks** (freshness verified live): `strategy_bar_history(bar_time)`, `reconciliation_runs(completed_at)` + `reconciliation_findings(created_at)`, `fills(filled_at)`, `broker_order_events(event_at)`, `trade_intents(status, updated_at)`, `broker_orders`, `market_capture_trades(received_at)`, `oms_managed_positions(status,...)`. (Note: `market_*_ticks` Schwab tables are chronically stale-by-design — never use as a signal.)

**Alerting + cron template** (fully reusable): ntfy topic `mai-tai-preopen-28806a5a97b7`; tiered push in `preopen_alert.sh` (RED=urgent/rotating_light/🔴, AMBER=default/warning/🟡, GREEN=min/white_check_mark/✅; detail stays on-box, only the verdict line transits); exit-code→level bridge (0/1/2 = GREEN/AMBER/RED); ET-wall-clock cron guard (`TZ=America/New_York`, box ignores `CRON_TZ`) with DST-safe dual-UTC scheduling, NYSE holiday-skip, weekday `1-5`; anti-spam state machine (`$OUT/state` = `STATUS LAST_ALERT_EPOCH`, alert on healthy→bad, re-alert every `COOLDOWN_SECS`, one GREEN recovery). `preopen_readiness_check.py` is the 3-tier multi-section model; `oms_liveness_check.py` is the independent (stdlib + `redis-cli`, no shared loop/DB) single-check model.

---

## 2. Framework design

**Principle: independence.** Every check is stdlib + `redis-cli`/`psql` subprocess only — it shares no event loop, DB session, or import graph with the service it validates (a frozen service cannot mask its own failure). This is why `oms_liveness_check.py` reads Redis via `redis-cli`, and the new layer keeps that discipline.

**A check registry, not a monolith.** Define each functional check as a small unit with a uniform contract:
```
Check := {
  name, service, enabled_flag,
  window: ET-window + holiday-guard (reuse the cron guard),
  probe():  reads ground-truth (DB freshness / heartbeat details / Redis-by-time),
  verdict(): -> (GREEN|AMBER|RED, one-line detail)
}
```
A single intraday runner (`fleet_health_check.py`, the general sibling of `preopen_readiness_check.py`) iterates the **enabled** checks, aggregates to the worst tier, writes full detail on-box, and emits one tiered ntfy line via the shared `preopen_alert.sh` path. Per-check anti-spam state (extend the state file to per-check keys) so one flapping check doesn't suppress another and doesn't spam. Driven by one cron with the standard ET/holiday guard.

**Incremental by construction:** checks are independently flag-gated and added one at a time; a new check ships **disabled**, is validated against live data for a few sessions (does it fire only when it should?), then enabled. This matches the operator's "one validated check at a time" and mirrors how the watchdog itself was proven (`--selftest` + quiet observation).

**Tiering doctrine** (borrow readiness): RED = a functional failure that risks money or means a core job stopped (page urgent); AMBER = degraded/precursor (e.g. bars slowing, findings rising) worth a look but not an emergency; GREEN = functioning. Off-hours / market-quiet / warming-up states map to GREEN or are skipped by the window guard — the schwab_1m_v2 `data_flow` enum (`stalled_offhours_rest_dry` vs `stalled_rth`) is the template for "expected-quiet ≠ failure."

**Redis-read helper (the §0 fix, applied once here):** read heartbeats **time-bounded** — `XREVRANGE mai_tai:heartbeats + <now - N min>` (or `COUNT ≥ XLEN`), or query per-service — so no check can ever false-"absent" from a fixed count window. All checks use this helper.

---

## 3. First checks (proposed order — highest risk-reduction first)

### Check 1 — Strategy-engine bar-processing freshness (external; catches the freeze class) ✅ first
**Why first:** directly catches the blocking-loop class the operator named — a strategy loop frozen inside an await keeps `status=healthy` but stops producing bars. Self-reported `main_loop_health` can freeze mid-value; the robust signal is **external**.
**Probe:** during RTH (+ the pre-market bar window), compare `max(strategy_bar_history.bar_time)` for the live strategy (and/or the live bots' `last_tick_at` from `mai_tai:strategy-state`) against wall-clock.
**Verdict:** bars advancing within N s → GREEN; slowing (N–M s) → AMBER; stalled > M s during active market with live upstream ticks (cross-check `market_capture_trades.received_at` is fresh, to avoid blaming a genuine market lull or an upstream feed outage) → RED "strategy loop not processing bars while feed is live" (the freeze signature).
**Independence:** DB + Redis reads only; never imports the engine.

### Check 2 — OMS order-lifecycle liveness (functional; alive-but-stuck) ✅ second
**Why:** the liveness watchdog proves the OMS beats; it cannot see an OMS that beats but has stopped *executing* (accepting intents but not placing/filling, or intents piling up non-terminal).
**Probe:** during RTH, if new `trade_intents` rows arrive but `broker_order_events.event_at` / `fills.filled_at` don't advance, or intents dwell in non-terminal status beyond a threshold, or a hard-stop-triggering condition isn't producing a close.
**Verdict:** intents flowing → orders/fills flowing → GREEN; intents arriving but no downstream events within threshold → RED "OMS accepting but not executing." Must be quiet when there's simply no activity (no intents = GREEN, not RED) — key off *relative* progress, not absolute counts.

### Check 3 — Stops-armed observability (the highest-value gap) ⚠️ depends on exposing state
**Why:** today there is **no external way to verify the sole safety net is armed** — `_armed_hard_stops` is process memory and Webull native stops are rejected. A silent failure to arm = a naked live position, undetected.
**Prerequisite:** expose armed-stop state externally. Two options, both cheap: (a) add `armed_stop_count` + a per-symbol armed list to the **oms-risk heartbeat `details`** (which is functionally empty today — this also fixes the oms-risk beat's biggest gap), and/or (b) the durable `oms_armed_stops` table proposed in the **restart-while-holding design (Framework 2)** — which makes armed state a queryable table. Framework 2 and this check are natural partners.
**Probe (OMS-owned only — see scoping invariant below):** for every **OMS-placed** open position — `oms_managed_positions status='open'` (v2) and ORB positions with OMS fill provenance / an `oms_armed_stops` entry — assert an armed stop exists in the exposed state. **Manual broker holdings are NOT probed** — they are out of the OMS's universe and must never be flagged.
**Verdict:** every OMS-owned open position has an armed stop → GREEN; an **OMS-owned** held position with no armed stop during RTH → RED "live OMS position unprotected." This is the functional guarantee behind the "never naked" invariant — and it keys off OMS ownership (provenance), never broker presence, so a manual position can never trip it.

> **OMS scoping invariant (operator, load-bearing):** the OMS only tracks/protects/acts on positions it placed; manual Webull/Schwab trades are outside its universe. Every check here that references "positions" means **OMS-owned** positions (provenance = a bot order/fill), never raw broker holdings. The reconciler's current per-symbol `position_quantity_mismatch` findings on manual holdings (CYN/CELZ/BJDX) are noise under this invariant — see the reconciler-scoping note below.

**Also fold in (small, high-leverage):**
- **Reconciler scoping (the structural fix behind the invariant):** today the reconciler flags `position_quantity_mismatch` (critical) for every broker holding vs virtual — including manual positions the OMS never placed (CYN/CELZ/BJDX). Under the scoping invariant those are non-events. Scope actionable findings to **OMS-owned** positions (provenance-based): a mismatch/absence on the OMS's *own* position is RED; the mere presence of a manual holding is out-of-universe and not a finding. This retires the protected-symbols list as the belt and is the structural end-state; it's a reconciler change (design-first), tracked jointly with the restart-while-holding design's open question 4.
- **Enrich the oms-risk heartbeat `details`** with `last_reconcile_age`, `fills_today`, `armed_stop_count` — turns the poorest beat in the fleet into a first-class functional signal and enables Checks 2/3 partly from the beat alone.
- **Bring ORB onto a watchable footing** — either publish an ORB heartbeat to `mai_tai:heartbeats`, or add an ORB isolated-state-freshness check (readiness already reads `mai_tai:strategy-state-isolated`), and replace ORB's hardcoded `data_health="healthy"` with a real per-symbol watchdog. Until then ORB has no functional health signal at all.

---

## 4. Rollout / rollback

- Each check independently flag-gated (`MAI_TAI_HEALTHCHECK_<NAME>_ENABLED`), ships disabled, validated live over a few sessions with `--selftest` for the alert path, then enabled. `git`-tracked on-box scripts (the ops scripts are not in the repo today; consider committing the new framework to the repo `ops/` so it's versioned + CI-lintable — operator to decide).
- Reuses the existing ntfy topic + cron-guard + anti-spam machinery — no new alerting infra.
- Any change that *exposes* OMS internal state (heartbeat enrichment, `oms_armed_stops`) is an OMS code change → design-first, attended quiet-window deploy, same discipline as Frameworks 1/2. The check *scripts* themselves are independent and deploy without touching services.

---

## 5. Open questions for operator

1. **Runner shape:** one aggregating intraday cron (`fleet_health_check.py`, worst-tier ntfy) vs per-check crons like the liveness watchdog (independent alerts). Recommend the aggregating runner with per-check anti-spam — one page with the worst tier + which checks failed, less noise.
2. **Cadence:** the liveness watchdog is every 1 min. Functional checks can be slower (bars ~every 30–60s, order-lifecycle ~1–2 min, stops-armed ~1 min). Confirm.
3. **Prioritize the oms-risk heartbeat enrichment first?** It's small, fixes the fleet's poorest beat, and unlocks Checks 2/3 — a good first *code* step even before the check scripts.
4. **Commit the ops scripts to the repo?** Today they live only in `/home/trader/` (not versioned/CI-covered). The new framework is a good moment to bring them under version control.
