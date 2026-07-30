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

### d. ⛔ Webull close-retry STORM — the floor exit retries forever against a flat broker
Live 2026-07-29, found validating the day's trades. **NCRA: 145 rejected sells in 55 minutes.**

    145 x [OMS-V2-MANAGED-EXIT] oms_v2_managed_exit:CW_FLOOR sym=NCRA acct=live:orb ref=3.0587
          ^ the SAME ref every time -- one exit decision, retried ~145 times (10:28-11:23 ET)
      8 x [OMS-V2-CW-FLOOR-ARMED]        (the floor keeps re-arming)
      3 x [OMS-V2-OCO-RESOLVED-FLAT]     (row closed 14:26, 14:28, 15:23 UTC)
    broker reject: `NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT`

Same shape on **AMIX x25** (11:31-11:32) and **STFS x7**. Also **AMIX x3**
`ORDER_NOT_SUPPORT_REVERSE_OPTION` at 10:00 — the known Webull fill-settlement-lag family.

**Mechanism (inferred, needs pinning):** the OMS believes it holds the position, Webull answers "no
position", the sell is rejected, and the retry never terminates. `_v2_close_reconcile_flat` is meant
to clear a phantom row after `_V2_EXIT_RECONCILE_AFTER_FAILURES = 3` rejects — but it only clears on
a **positively-confirmed flat**, and for Webull `_broker_symbol_is_flat` may be returning UNKNOWN
rather than FLAT_CONFIRMED, so the counter never satisfies the guard.
⛔ That guard is deliberate and must NOT simply be loosened — `[]` from Webull is genuinely ambiguous,
and treating UNKNOWN as flat is how the ERNA naked position happened
([[project_mai_tai_false_flat_naked_position]]). The fix is a TERMINATION bound, not a weaker flat
test: cap the retries per exit decision and stand down loudly.

**Cost:** ~180 rejected orders in one session — API budget, log noise, and it masks real rejects.
No money was lost (every position did exit), so this is correctness + hygiene, not a P&L leak.


---

## ✅ AFTER-CLOSE BATCH 2026-07-29 — RESULTS

| item | outcome |
|---|---|
| operator's manual fills | ✅ **BOTH removed** (1000 sh @4.68 and 2000 sh @4.5555) + their synthetic `-ocoexit` order rows. Contamination check now **0 suspect of 36**. Full row contents were printed before deleting. |
| **(e)** claim-a-manual-trade | ✅ **FIXED + LIVE** (#605). Ownership is structural: walk only our entry's `childOrderStrategies`, FAIL CLOSED without `entry_broker_order_id`. Webull was never affected (suffixed coids). Suite 1664 green. |
| **(a)** dead-bot prune | ✅ **DONE.** 1,091,270 rows across 6 codes. `strategy_bar_history` **1962 MB → 815 MB** (~1.1 GB reclaimed). Only the 2 read codes remain (polygon_30s 817,331 · schwab_1m_v2 202,756). **Backtest re-verified byte-identical afterwards** (STKH 07-28: 379 bars, +1.90%). |
| **(c)** readiness RED | ✅ **FIXED + LIVE** (#606). Verdict **RED (3 FAIL) → AMBER (0 FAIL)**. Keyed on `systemctl is-enabled`, NOT the env var. |
| **(b)** restart trade-coach | ⚠️ **DONE BUT INEFFECTIVE — see below.** |
| **(d)** Webull close-retry storm | ⏳ still open, carried below. |

### ⚠️ (b) did NOT work — the CPU is inherent, not drift
`trade-coach` was **43.0%** before the restart and settled at **47.2%** after, on 21 days vs 1 minute
of uptime. So this is not a leak that a bounce clears — the service genuinely burns ~45% of a 2-vCPU
box continuously. Load is still ~2.2 with `strategy` 36.5% and `control` 33.7% alongside it.
⇒ **The OMS heartbeat starvation WILL recur**, and the 09:00/09:09-style liveness pages with it.
⇒ Folded into open item 2 (polygon/strategy-engine freeze): the real question is why an AUXILIARY
service costs half the box, and whether it should run during market hours at all.

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

## 3. 🧹 VPS retention — PART DONE 2026-07-29
✅ **`strategy_bar_history` DONE**: 1,091,270 dead-bot rows removed, 1962 MB → 815 MB. Backtest
re-verified byte-identical afterwards. `scripts/prune_strategy_bar_history.py`.
Remaining candidates (disk is only 24% used, so this is hygiene):
Disk is **not** under pressure (25% used, 88 GB free) — this is hygiene.
✅ **Already self-managing, verified 2026-07-29:** `market_capture_trades`/`_quotes` hold exactly
14 days and `market_trade_ticks` 30 days; both systemd timers ran that morning. **Leave them alone.**

| table | size | proposal |
|---|---|---|
| `reconciliation_findings` | 1142 MB, 2.5M rows, oldest 2026-03-30, never pruned | **prune > 30 days** — diagnostics only |
| `dashboard_snapshots` | 1017 MB for **5002 rows of a single day** (~200 KB each) | needs a retention policy; confirm nothing reads history first |
| ~~`strategy_bar_history`~~ | ~~1955 MB~~ → **815 MB** | ✅ **DONE** — dead-bot rows only. ⛔ The surviving `schwab_1m_v2` rows are the backtest bar source and must never be pruned. |

[[project_mai_tai_retention_inventory]]

---

## ⚠️ Watch items live in [`session-handoff.md`](session-handoff.md), not here
Verification is a *state* ("is this behaving?"), not a *task* ("do this"). Keeping them here is what
made an open-items file that could never reach zero.
