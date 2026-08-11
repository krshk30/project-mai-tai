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

**As of 2026-08-11 EOD.** Fleet: 6 services active, `NRestarts=0`. **Deployed HEAD `32926b6`**
(verified in the CHECKOUT and by CONTENT, not just the PR).

⛔⭐⭐ **`FRTT` IS NOW PROTECTED — IT WILL NOT TRADE AT ALL.**
`MAI_TAI_PROTECTED_SYMBOLS=CYN,TE,FRTT` (was `CYN,TE`). Operator-directed after he manually bought
5,000 FRTT mid-afternoon while the bot was trading the same name. **He closed that position into the
bell, so the original reason is gone** — this is a one-line revert + restart when he wants it back.
Backup: `/etc/project-mai-tai/project-mai-tai.env.bak-20260811-deploy`.
⚠️ FRTT vanishing from the watchlist is the protection working, **not a defect**.

**Broker FLAT except `CYN 5000` (operator manual).** 0 working orders, 0 open managed rows.

## ✅ DEPLOYED 2026-08-11 16:08 ET — `cb30fcd → 32926b6`

| step | result |
|---|---|
| merged | **#678** `460b1ac` → **#679** `32926b6` |
| restart | stop v2 → restart oms → start v2. 6 services active, `NRestarts=0`, **0 tracebacks** |
| boot | `[V2-BOOT-HOLD] released — 0 reconstructed-uncapped segments` 16:08:48 |
| **bar gap** | **NO HOLE** — every minute 16:00→16:08 present for all 4 watched symbols |
| suite | **1996 passed / 0 failed** (baseline 1978 + 18 new), ruff clean |
| ⚠️ window | deployed at **16:08 ET, EH still open**, on the operator's explicit call (armed-but-flat accepted). Not the 20:15 quiet window. It came out clean; **do not treat that as licence** |

### #678 — held positions keep their market data
Subscriptions now publish **watchlist ∪ held**. The OMS exit ladder is NOT watchlist-gated
(`_watchlist` = 0 refs in `oms/service.py`) but it IS quote-driven, so dropping a held symbol
silently disarmed CW_TARGET/CW_FLOOR/CW_HARD_STOP/CW_FLIP **together**. Ownership = 3-source
ADD-only union (virtual ∪ managed ∪ **account_positions**) minus protected. ⛔ **EXIT-ONLY** — see
`docs/design/held-symbol-exit-coverage.md` §2; do not "complete" it to allow entries.
⚠️ **`[V2-EXIT-COVERAGE]` has fired 0×. UNEXERCISED — needs a live held position.**

### #679 — the orphan watch asks a stronger question
`classify_unowned` (*does anything OWN this order?*) + `classify_oversell` (working sells ≤ shares
held). 6/6 mutations. Live GREEN on cron at 16:09:22 ET.

---

## 👀 WATCH TOMORROW (2026-08-12)

1. **⭐ `[V2-EXIT-COVERAGE]` on the first held position** — #678's acceptance, as a quoted line.
2. **⭐ The orphan watch's NEW lines** — a `classify_unowned` RED is a **FINDING** (a failed cancel
   happened again); a `classify_oversell` RED means stacked sells. Neither has fired yet.
3. **Webull reject storm** — ~30 rejected market sells on WXM alone today; every exit preceded by a
   burst of 3–11. `live:orb` shows **10,980 rejected sells vs 144 filled over 11 days**. #608 did
   not close this. ⛔ contaminated by the client-abort conflation, so treat the magnitude, not the
   count, as the signal.
4. **FRTT** — protected. Decide whether to unprotect.

### Tomorrow's queue
1. **🔴 CANCEL-RETRY DESIGN** — the day's real root cause. See below.
2. Commit the exec bit on `ops/health/bar_gap_watch_cron.sh` (still 100644 committed; the box has a
   hand-`chmod`, preserved across today's pull).
3. Decide deliberately whether #674's price cap stays now that #676 rests.
4. Two sweeps (dead guards · latch-timing inferences).

---

## 🔴 OPEN THREADS (detail: [`handoff-open-items.md`](handoff-open-items.md))

1. **🔴⭐⭐ A CANCEL IS FIRE-AND-FORGET — NOT FIXED.** FRTT 13:01:02: the cancel was emitted, died on
   the network (`upstream connect error … connection termination`), and the order stayed **LIVE and
   unowned for 136 minutes** until the operator killed it by hand. We treat the *attempt* as the
   *outcome*. #679 now **detects** it in ~2 min; the **cure** (verify the cancel landed, retry on
   transient failure) is unbuilt and is a real-money ENTRY-path change — design first.
   ⛔ 11-day census: exactly **2** rejected cancels, so it is rare and unbounded, not frequent.
2. **⭐⭐ WE DO NOT RECORD OCO CHILDREN AS ORDERS.** Our order table has no row type for them, so a
   live protective sell at the broker is invisible **by construction** — today the DB showed 0
   working orders while the ladder showed 2. This is why four sells stacked on two shares unseen.
3. **⛔⭐⭐ NO REPLACEMENT LINK** in the order chain — reprice / trade-pairing / entry-lot are ONE
   gap. `watchdog_replaces_client_order_id` exists but is **0/304** on entry orders.
4. **🔴 P0 — FRESHNESS GATE ON A STALENESS GUARD.** A post-boot promotion whose warmup series is
   stale keeps `just_warmed=False`, so `_cap_reconstructed_segment` never runs and BOOT-HOLD
   suppresses entries **FLEET-WIDE**. Cost **2h22m on 08-11**. ⛔ "seed-cap on promotion" already
   exists — that is a no-op.
5. **⛔⭐⭐ `broker_order_events` conflates CLIENT aborts with BROKER refusals** — every reject count
   on that table is contaminated. Needs a `source` field.
6. **⛔ We never store a BROKER FILL PRICE.** `filled_avg_price` empty on entry *and* exit; the
   ladder anchors on the *requested* price. Today's chart showed the FRTT exit at **1.535** while
   our record says `reference_price 1.53`. Every stop/target is wrong by the slippage.
7. **⭐⭐ SELECTION — we buy moves already SPENT.** ⛔ DISCUSS BEFORE BUILDING.
8. **Redis evicts the HEARTBEAT stream ⇒ false "fleet down".** `allkeys-lru`, zero `xreadgroup`.
9. **Reconciler severity INVERTED** · **Schwab API-open rejects ~3/day** · **`-close-` route
   unattributable** · **per-lot attribution gap**.
10. **Webull is OFF THE TABLE** until the Schwab reactive→resting work is proven.

✅ **CLOSED today:** #678 · #679 · **#676 is EXERCISED** (`21 reclaim placements`, `slot=reclaim`,
proven on today's tape — the FRTT order that consumed the afternoon *was* a #676 reclaim rest) ·
the resting-cancel split (5 sessions, n=655: reprice 58.2% · liquidity_floor 35.4% ·
**flip_no_fill 6.0%** ⇒ the "76% never fill" headline was per-ORDER over an opportunity-level
question) · the orphan watch proven **end-to-end incl. phone delivery**.

---

## 🔔 ALERTING — what reaches the phone
`orphan_order_cron.sh` **(PROVEN 08-11: RED classified, pushed, RECEIVED)** · `bar_gap_watch_cron.sh`
· `reconcile_alert_cron.sh` · `entry_fix_watch/watch_cron.sh` ⛔ **silence is NOT green — read
`STATUS.txt`** · `entry_fix_watch/eod_cron.sh` · OMS liveness · pre-open readiness · token expiry ·
OCO capture.
🆕 `/home/trader/slot_watch/check.sh` → `STATUS.txt` — the `slot=` watch. ⛔ **not yet in cron.**
⛔ All ROOT crontab, ET-guarded **inside** the script (`CRON_TZ` ignored on this box).
⛔ **A script committed from Windows lands mode 664 AND carries CRLF** — verify `stat -c %a` and
`bash -n` **on the box**.

---

## 🧠 MEMORY POINTERS (auto-load each session)
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project-mai-tai-restart-bar-gap-checklist]] **(READ BEFORE ANY RESTART)** ·
[[project_mai_tai_cancel_is_fire_and_forget]] ·
[[project_mai_tai_v2_post_boot_promotion_uncapped_fleet_hold]] ·
[[project_mai_tai_no_replacement_link_in_order_chain]] ·
[[feedback_check_which_parts_already_work]] · [[feedback_a_watch_that_fails_to_a_false_clean]] ·
[[feedback_a_wrong_reason_is_worse_than_a_missing_one]] · [[feedback-be-crisp-no-essays]]
