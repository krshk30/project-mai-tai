# OPEN ITEMS

> Threads that are genuinely **open**. One line each in [`session-handoff.md`](session-handoff.md).
>
> **Driven to 3 with the operator 2026-07-29** (from 66 → 19 → 3). Everything closed moved verbatim
> to the bottom of [`handoff-log.md`](handoff-log.md); nothing was deleted.
>
> ⛔ **KEEP IT SHORT. The rule that let this rot: items were only ever ADDED.**
> - When something closes, **MOVE it to the log** — do not leave it here marked ✅.
> - A *study you are not going to run* is not an open item. Close it; it comes back with evidence
>   attached if a live loss points at it.
> - A *standing rule* (e.g. "default flips need the full suite") belongs in **memory**, not here —
>   it will never be "done", so it can only distort the list.
> - A *dormant* item (its feature is switched off) is closed, not carried.

---

---

## 🌙 AFTER CLOSE TODAY (2026-07-29) — a scoped batch, not open-ended
All three are ready to run the moment the fleet is flat after 16:00 ET. **Close them tonight; do not
let them become standing items.**

### a. Run the dead-bot bar-history prune
`scripts/prune_strategy_bar_history.py --go`, then `VACUUM (ANALYZE) strategy_bar_history`.
Dry-run already verified 2026-07-29: **1,091,270 rows** across 6 codes, every one **51+ days silent**
(newest write 2026-06-09), ~1 GB. Deferred from the morning because a live AMIX position was open and
the box was saturated. ⛔ `schwab_1m_v2` rows are NOT touched — that is the backtest bar source.

### b. Restart `trade-coach` — it is the biggest CPU consumer on the box
Measured at the 07-29 open: **43.0% CPU, 21 days uptime**, and it is AUXILIARY (the readiness check
labels it so). The box is 2 vCPU at load ~2.4:

    trade-coach 43.0%  ·  control 32.8% (1.6 GB RSS, 15d)  ·  strategy 29.1%  ·  market-data 22.0%
    oms 2.4%   <- STARVED, not stuck

⭐ **This is what caused the 09:00 + 09:09 OMS LIVENESS alerts.** The OMS never restarted
(`NRestarts=0`) and logged continuous healthy work through both windows with zero errors — its event
loop simply could not get scheduled, so the 15s heartbeat went >180s stale twice and recovered within
a minute each time. **Not a zombie; CPU starvation.**
⇒ New evidence for open item 2 (the polygon/strategy-engine freeze): the load is broader than the
strategy engine alone. Consider `control` (32.8%, 1.6 GB after 15 days) next.

### c. Stop the readiness check RED-ing on a decommissioned ORB
`preopen_readiness_check.py` produced **RED — 3 FAIL** on 07-29, and all three were ORB:
`orb inactive` · `orb (iso-state) NO recent isolated-state` · `ORB no isolated-state`.
ORB was deliberately disabled 07-29 ([[project_mai_tai_orb_decommissioned_but_flag_stays_true]]).
⛔ Left as-is this fires a false **"DO NOT trust the open"** every morning — which is exactly how a
real RED gets ignored. Make the ORB checks conditional on the service being enabled.

---

## 1. 🔬 Re-run the backtest-vs-live comparison on a STABLE-CODE day
07-28 cannot judge parity — six deploys landed mid-session, so live ran >=4 code versions while the
replay runs the final one. **Config parity itself is FIXED and verified** (89/90, #592); what is
unproven is whether the engine reproduces a live day end-to-end. Only STKH has matched so far
(+1.90% live vs +1.90% replay).

⛔ **Respect three structural limits when comparing:**
- the replay takes **ONE round trip per symbol-day** (`if exit_done: break`) — so "1 replay vs 6
  live" is expected; compare only the **FIRST** live trade of each symbol-day
- quote density ~1 per 4s vs a continuous live feed
- sparse-bar symbols are uncomparable (CNET: 71 bars, 1 quote/118s)

Scripts on the box: `_parity_diff.py`, `_live_today.py`, `_cnet_probe.py`, `_density.py`.
Config-drift check now shipped: `ops/health/env_default_drift.py` (#598).
[[project_mai_tai_backtest_live_parity_audit]]

## 2. 🐌 polygon / strategy-engine freeze
60-80s loop freezes at the open and close; py-spy showed ~72% CPU in the JSON snapshot encode.
`#366` (snapshot-persist throttle) was deployed and is **INSUFFICIENT**. Next candidate: offload or
encode-once. F3 health check #1 detects it. [[project_mai_tai_polygon_freeze]]

## 3. 🧹 VPS retention prune — PLAN APPROVED, awaiting the go-ahead on specifics
Disk is **not** under pressure (25% used, 88 GB free) — this is hygiene.
✅ **Already self-managing, verified 2026-07-29:** `market_capture_trades`/`_quotes` hold exactly
14 days and `market_trade_ticks` 30 days; both systemd timers ran that morning. **Leave them alone.**

| table | size | proposal |
|---|---|---|
| `reconciliation_findings` | 1142 MB, 2.5M rows, oldest 2026-03-30, never pruned | **prune > 30 days** — diagnostics only |
| `dashboard_snapshots` | 1017 MB for **5002 rows of a single day** (~200 KB each) | needs a retention policy; confirm nothing reads history first |
| `strategy_bar_history` | 1955 MB, oldest 2026-04-02 | ⛔ **DO NOT PRUNE** — `backtest/data.py` reads it; it IS the backtest bar source, and pruning it would destroy the history item 1 depends on |

[[project_mai_tai_retention_inventory]]

---

## ⚠️ Watch items live in [`session-handoff.md`](session-handoff.md), not here
Verification is a *state* ("is this behaving?"), not a *task* ("do this"). Keeping them here is what
made an open-items file that could never reach zero.
