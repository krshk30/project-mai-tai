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
