# Session Handoff — CURRENT STATE (read this first)

> ## ⛔ HOW TO MAINTAIN THIS FILE — two verbs, never merge them
> 1. **OVERWRITE this file.** It answers one question: *what is true RIGHT NOW.* If a line here is
>    no longer true, **delete or rewrite it** — never append.
> 2. **APPEND to [`handoff-log.md`](handoff-log.md).** That is where *what changed today* goes.
>
> **Target: ~150 lines.** To onboard an agent: *"Read `docs/session-handoff.md`."*

> **⛔⭐ OUTPUT CONSTRAINT (every study): report per-trade %, MEDIAN-FIRST, with a drop-one.
> NEVER a bare dollar total.**

---

## ⚡ FIRST SCREEN — act on this alone

**Fleet: 7 services active. Deployed HEAD `cb30fcd`** (verified in the CHECKOUT, not just the PR).
**Broker FLAT except `CYN 5000` (operator manual).** **`SESSION_TIME_ROLL_ENABLED=true`.**

⛔⭐⭐ **TWO ENTRY-PATH CHANGES ARE LIVE, ENABLED, AND UNVALIDATED — unattended from 07:00 ET.**
`#674` (reactive = band-capped LIMIT) and `#676` (reclaim RESTS at the segment high). Both change
what the bot sends. Nothing could be exercised at deploy time (market closed) — see the validation
split below. Exposure is bounded: **qty 2**, and both make entries **more** price-controlled.

⛔ **`AUUD MGIH WAFU WYHG` carry a re-issued entry cap** (Bug 2, accepted in the override). The cost
is a **lost entry, never a duplicate**; the 04:00 roll clears stale arms.

## ✅ DEPLOYED 2026-08-10 20:21 ET — `ca0cf92 → cb30fcd`

Merged in order: **#672** `4692948` → **#674** `6668042` → **#676** `cb30fcd`
(#675 conflicted after #674 was squash-merged; rebased as #676, full suite re-run on the new main).

| step | result |
|---|---|
| gate | **GO BY OPERATOR OVERRIDE, exit 0** — see below |
| restart | stop v2 → restart oms → start v2. All 6 services `active`, `NRestarts=0`, **0 tracebacks** |
| boot | `[V2-BOOT-HOLD] released — 0 reconstructed-uncapped segments; CW-v2 entries open`, watchlist 8 |
| **bar gap** | newest bar **19:59 ET** — EH closed 20:00, **no bars were due, no hole possible** |
| post-deploy | **0 orders · 0 intents · 0 working · 0 managed rows**; broker flat except CYN |
| suite | 1925 (baseline) → **1978 passed**, +53 tests, zero regressions, ruff clean |

### ⭐⭐ THE GATE WORKED AS DESIGNED — first time. Note it as ACHIEVED.
```
[ok]       past 18:00 ET
[OVERRIDE] 4 ARMED SEGMENT(S) accepted by the OPERATOR: AUUD MGIH WAFU WYHG
           Bug 2 WILL re-issue the entry cap on each. That BLOCKS entries;
           the cost is a lost entry, never a duplicate.
[ok]       zero open managed rows
[ok]       broker flat on both real-money accounts (operator manuals excluded)
===> GO **BY OPERATOR OVERRIDE**.   TRUE EXIT CODE = 0
```
⛔ Friday and today's 16:08 run both had blocks with **NO token**, bypassed with nothing in the
gate's own record. Tonight: **one block, one documented token, exit 0, complete audit trail.**
⭐ **The anti-reuse property fired and was RIGHT:** the armed set moved `JWEL SCKT WYHG XHLD`
(16:08) → `AUUD STKH WYHG` (18:05) → `AUUD MGIH WAFU WYHG` (20:15). A copied list is refused by
design. **Always re-read the set at the instant of the run.**
⚠️ Read the exit code **UNPIPED** — a pipe reported a false `EXIT=0` on Friday.

### ⛔ VALIDATION SPLIT — one confirmed, the rest UNEXERCISED **by construction**
| what | status |
|---|---|
| **#672 census denominator** | ✅ **CONFIRMED LIVE.** Before/after across the restart: `window=300s evaluated=0` → `window=300s **submitted=0** evaluated=0` |
| #672 `[OMS-INTENT-DROPPED]`, `flip_no_fill_soft_rest` | **UNEXERCISED** — needs a dropped intent / a soft-rest flip |
| #674 `[OMS-V2-RTH-REACTIVE-LIMIT]`, `[V2-CW-RULE7-BLOCK]` | **UNEXERCISED** — needs an RTH reactive cross |
| #676 `[V2-RESTING-PLACE] slot=reclaim` | **UNEXERCISED** — needs a resting placement |

⛔ **Market was closed at deploy time. Silence is NOT a pass — do not write it up as one.**

---

## 👀 WATCH TOMORROW (2026-08-11) — from 07:00 ET

1. **⭐ IMMEDIATE — `[V2-RESTING-PLACE] slot=reclaim` on the first reactive cross.** That is #676's
   acceptance as a **quoted line**, not a statistical inference hours later. `slot=first|reclaim`
   separates the two paths on the tape.
2. **Then the price comparison** — reactive fills should show the RESTING path's dispersion
   (**SD ~25 bps, nothing past ~60**) instead of **SD 57.0 / worst +351.7**. ⛔ **PRICE ONLY. No
   outcome measures** — strategy is parked.
3. ⚠️ **Stop-above-ask rejects.** `cw_segment_high` sits AT the recent high by definition, so this
   path is **more** exposed than the trail-based one. **4 leaks in 11 days on the LESS exposed path
   is the BASELINE, not the expectation.** A rise is expected in direction, unknown in size.
4. ⚠️ **A limit that does not fill.** A market order always fills; #674/#676 sometimes will not.
   Both placement and abandon are logged — read the non-fill rate off the tape.
5. **`[V2-CW-RULE7-BLOCK]`** — turns the 1.3% upper bound into a measured number within days.
6. **#668 `[VIRTUAL-CLEAR]`** — still UNEXERCISED. ⛔ **A line there is a FINDING, not a pass.**
7. **#664 CW_FLIP fan-out** — opportunistic; **UNEXERCISED is the expected outcome, not a failure.**

### Tomorrow's queue
1. **Commit the exec bit on `ops/health/bar_gap_watch_cron.sh`** — the box has a hand-`chmod +x`;
   the **committed mode is still 100644**, so a fresh checkout gets a cron that silently never runs.
2. **Decide deliberately whether #674's price cap stays** now that #676 rests. It was shipped as a
   **STOPGAP** on that path. ⛔ Do not leave it by default — decide.
3. **Two sweeps** (below).
4. Pre-flight override paths for the flat blocks (still no token; not needed tonight, still owed).

---

## 🔴 OPEN THREADS (detail: [`handoff-open-items.md`](handoff-open-items.md))

1. **⛔⭐⭐ `broker_order_events` conflates CLIENT-SIDE aborts with BROKER refusals.** Three "Webull
   rejected" events were our own adapter's `RuntimeError`, stored in the same shape. **Every reject
   count on that table is contaminated — A2, A3, the API-open ~3/day included.** Needs a `source`
   field, **separable retrospectively**. ⭐ **Measured cost: it produced the 07-25 MARKET fan-out
   decision** — the reasoning was sound, the evidence was misattributed.
2. **⭐ SWEEP — dead guards dominated by an earlier return.** 3 found by accident in 4h; each reads
   as protection and provides none. **Only mutation finds them** (delete it, tests stay green).
3. **⭐ SWEEP — inferences from WHEN a latch is set, not THAT it is.** Found in `update_position`
   while building #676. No test fails; the test encodes the old timing too. Grep the **comments**.
4. **⭐⭐ SELECTION — we buy moves already SPENT.** ⛔ DISCUSS BEFORE BUILDING.
5. **Redis evicts the HEARTBEAT stream ⇒ false "fleet down" RED page.** `allkeys-lru`, still zero
   `xreadgroup`. ⛔ Do NOT fix by cutting `snapshot_batch_stream_maxlen` (180 is load-bearing).
6. **Reconciler severity INVERTED** — an UNOWNED position pages CRITICAL. ⛔ Blocked behind item 12.
7. **Schwab API-open rejects ~3/day, nothing evicts.**
8. **Order churn / `-close-` route unattributable** · **per-lot attribution gap**.
9. **⛔ `virtual_positions` reads ZERO for a position we HOLD (DSY 08-07).** Ownership =
   `oms_managed_positions`. Item 12 from the previous board; unchanged.
10. **Webull is OFF THE TABLE** until the Schwab reactive→resting work is proven. The re-scoped
    combo probe (can a combo MASTER be STOP_LIMIT? RTH, preview-only, EH repeat, **session recorded
    beside each result**) is parked with it. ⭐ **Fallback if refused:** the leg can still REST with
    protection attached separately — the bracket is a convenience, the resting entry is the fix.

✅ **CLOSED today:** #663 (complete, both drivers) · #666 (PASSED) · D1a #657 (validated 08-08) ·
the vol-floor flap (measured: 279 cancels/7d, only **4** crossed a level the segment still wanted —
**count says urgent, cost says no**) · `limit_price=0` (**not live** — zero since 07-24, closed by
#547) · the fan-out sequential-fallback question (**it is B**: both legs queued together,
unconditional; 87 pairs where Schwab FILLED and Webull fired anyway).

---

## 🔔 ALERTING — what reaches the phone
`bar_gap_watch_cron.sh` (auto-repairs) · `reconcile_alert_cron.sh` · `entry_fix_watch/watch_cron.sh`
⛔ **silence is NOT green — read `STATUS.txt`** · `entry_fix_watch/eod_cron.sh` (18:05 ET) ·
OMS liveness · pre-open readiness · token expiry · OCO capture · orphan orders.
⛔ All ROOT crontab, ET-guarded **inside** the script (`CRON_TZ` is ignored on this box).
⛔ **A script committed from Windows lands mode 664 AND carries CRLF** — both make it silently never
run. Verify `stat -c %a` and `bash -n` **on the box**.

---

## 🧠 MEMORY POINTERS (auto-load each session)
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project-mai-tai-restart-bar-gap-checklist]] **(READ BEFORE ANY RESTART)** ·
[[project_mai_tai_fanout_order_type_asymmetry]] · [[project_mai_tai_broker_order_events_conflates_client_aborts]] ·
[[feedback_an_absence_is_evidence_only_against_a_known_denominator]] ·
[[feedback_an_ambiguity_fix_that_rebuilds_the_ambiguity]] ·
[[feedback_the_tools_status_is_not_the_things_status]] · [[feedback-be-crisp-no-essays]]
