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

**As of 2026-08-12 EOD.** Fleet: 7 services active, `NRestarts=0`. **Deployed HEAD `867dcd0`**
(verified in the checkout AND by content — both new markers present in the running tree).

✅ **DEPLOYED 18:05 ET with BOTH NEW FLAGS ON** (operator's call — deploying flags-off would have
forced a second restart in tomorrow's pre-market, the worst window to take one):
```
MAI_TAI_OMS_CANCEL_VERIFY_ENABLED=true
MAI_TAI_OMS_V2_RTH_FANOUT_LIMIT_ENABLED=true
```
Both confirmed loaded from the OMS process's own `/proc/<pid>/environ`. Env backed up as
`project-mai-tai.env.bak-deploy-20260812-220439` — the kill for either is one line + a restart.
⛔ **NEITHER HAS FIRED YET. Both are UNEXERCISED against a live broker.**

⭐ **The restart touched OMS + strategy ONLY. `schwab-1m-v2` was NOT restarted** — it is the bar
builder, so leaving it up meant **no bar hole**: every symbol advanced 18:03 → 18:04 straight through.
Reuse that scoping for any OMS-only change.

**Positions: `CRWU 2` (schwab) + `CRWU 1` (orb) — operator closing by hand.** ⛔ **CYN is CLEARED —
the operator sold all 5,000. `MAI_TAI_PROTECTED_SYMBOLS=CYN,TE` is now stale but harmless.**
FRTT unprotected and tradeable (verified per-process), but the scanner has not re-promoted it.

## 👀 WATCH TOMORROW (2026-08-13)

1. **⭐ `[OMS-CANCEL-CONFIRMED]` / `[OMS-CANCEL-RESUBMIT]` / `[OMS-CANCEL-UNCONFIRMED]`** in `oms.log`.
   This exercises from 07:00 on any cancel. An UNCONFIRMED is a **finding**, not noise.
2. **⭐ `[OMS-V2-RTH-FANOUT-LIMIT] PLACED|ABANDONED`.** ⛔ **RTH-ONLY — it cannot fire before 09:30**,
   so a quiet pre-market is NOT evidence it did nothing. ABANDONED = a fan-out entry we deliberately
   did not chase; that is the intended new behaviour, count it rather than fear it.
3. **🔴 CRWU** — see the unexited-position thread below. Confirm the operator's manual close landed.
4. The Webull reject storm (`live:orb`) is unchanged and still contaminated by the client-abort
   conflation — treat magnitude, not count.

## 🔴 THE DAY'S SHARPEST FINDING — a held position with nothing trying to sell it

**CRWU, 2026-08-12.** Held 2 (schwab) + 1 (orb), entries 5.8899 / 5.88, bid fell to **5.61**
(−4.75% / −4.59%, on the day's low) with **no broker-side stop at all** — the `session=NORMAL`+`DAY`
bracket expired at 16:00 and `MAI_TAI_OMS_V2_EOD_OCO_TRANSITION_ENABLED=false` means nothing replaces
it. The software ladder tried twice and BOTH brokers refused:
- Webull 15:30 `ORDER_NOT_SUPPORT_REVERSE_OPTION`
- Schwab 15:54 `This order may result in an oversold/overbought position`

Then **nothing was attempted for >2h** — the OMS sat polling for an OCO exit fill that can never
arrive, on managed rows **13,736s / 18,618s** old (`[OMS-OCO-EXIT-MISS] ... A miss on an OLD row is
the 07-31 AXTU/AXTX defect`). ⇒ This is the unexited-position direction the operator called
unrecoverable, and it is the live case for **#647 Gate 2** (`oms_v2_rth_edge_bracket_enabled`, still
OFF). See [[project_mai_tai_premarket_exit_protection]].

## ✅ MERGED + DEPLOYED TODAY — #684 (`867dcd0`)

**1. Cancel-verify** — the cure for the FRTT 136-minute hole. After a cancel: read the order back
until it settles → **re-submit** if it still reads working → `[OMS-CANCEL-UNCONFIRMED]` with the
coid + broker id if not. ⛔ `accepted`/`PENDING_CANCEL` is deliberately **not** proof.
⛔ A *raised* cancel is an UNKNOWN, not a failure — swallowed only when the flag is on, then resolved
by reading. Backgrounded, because inline would stall `process_trade_intent`, which carries EXITS.

**2. The fan-out leg finally has a price ceiling.** ⛔ **#674 capped only the SCHWAB PRIMARY** — its
own gate says *"the fan-out leg is deliberately untouched here"*. So the Webull leg still shipped as
`order_type: "limit" if session_is_eh else "market"` = **UNCAPPED MARKET in RTH, on BOTH sources
(`reactive` AND `rth_resting`)**. Live proof: 08-12 BAOS, the primary decided **1.1702** under its cap
while the fan-out leg paid **1.1800** and lost **5.08%**.
⚠️ NEW FAILURE MODE, NAMED: a market order always fills; a capped limit sometimes will not.

**Validation:** 24 tests · suite **1989 pass / 0 fail** · ruff clean · **9 mutations, each caught by
the test that should catch it.**

## ⭐⭐ PROBE W — RUN LIVE, SETTLED (CORE/RTH, `live:orb`, FRTT)

| shape | result |
|---|---|
| **A** LIMIT master + STOP_PROFIT + STOP_LOSS | **200** — accepted, placed live |
| **B** STOP_LIMIT master + same legs | **417** `invalid order_type, value: STOP_LOSS_LIMIT` |
| **C** bare STOP_LIMIT, no legs | **200** at preview |

⇒ **Webull refuses a stop-limit combo master; Schwab accepts the identical shape** (`previewOrder`
200 / 0 rejects). The fan-out order-type asymmetry is **the broker's**, unfixable our side.
⛔ **DO NOT remove `webull.py:949`** — it enforces the shape 174 live Webull brackets depend on.
⛔ **#681 still has the enum bug** (line 119 sends `STOP_LIMIT`, must be `STOP_LOSS_LIMIT`). Fix
before merging; the first probe run was invalid because of it, and **the failing control caught it**.

## 🕐 OPEN PRs
| PR | what | state |
|---|---|---|
| **#681** | Probe W | ⛔ needs the line-119 enum fix before merge |
| **#682** | entry-slippage validation | needs sudo; unrun |
| #673 · #646 · #642 · #640 | docs/design | awaiting review |

## 🔴 OPEN THREADS (detail: [`handoff-open-items.md`](handoff-open-items.md))

1. **🔴 The 16:00 bracket death** — see CRWU above. #647 Gate 2 is the built, dark answer.
2. **⛔⭐⭐ PRE-MARKET IS 0% BRACKETED.** Measured, bot-only, 14d: RTH **172/172** orb and **131/132**
   schwab; PRE (EH) **0/34** and **0/13**. Both brokers are limit-only in EH, so no design can put a
   stop at the broker before 09:30 — the RTH-edge arm is the only answer.
3. **⛔⭐ THE ENTRY LEVELS ARE QUANTISED BY ~70 bps.** +2%/−5% is computed off the **decided** price
   then tick-rounded; at ~$1.40 a cent is 70 bps, so 08-11 actually ran **+2.11…+2.47 / −4.38…−5.11**.
   The 0.5% entry band collapses to **ZERO** on sub-$2 names. **Fix the unit, not the number.**
4. **⭐⭐ THE CHURN IS STILL THE BIGGEST NUMBER ON THE BOARD** — median **5 entries per symbol-day**,
   max 16 (verified 324 fill rows = 324 distinct orders, not partial fills); ~**200% of one
   position's notional** in crossing over 14 days. ⛔ The entry-ordinal study is still unrun and
   selection is **DISCUSS BEFORE BUILDING**.
5. **⛔⭐⭐ `broker_orders` records an OCO child ONLY WHEN IT FILLS** (`oco_child_legs == legs_filled`
   in all 21 day-rows). It can answer *"did a leg execute"* and **cannot** answer *"is a bracket
   resting now"* — only the broker can.
6. **⛔ The orphan watch reads SCHWAB ONLY** (`SchwabBrokerAdapter`,
   `strategy_schwab_1m_v2_account_name`). It can never clear a Webull question.
7. **⛔⭐⭐ `broker_order_events` conflates CLIENT aborts with BROKER refusals** — every reject count
   on that table is contaminated. Needs a `source` field.
8. **P0 boot-hold freshness gate** · **Redis evicts the heartbeat stream** · **`-close-` route
   unattributable** · **per-lot attribution gap** — all unchanged.

## 🔔 ALERTING
`orphan_order_cron.sh` (Schwab-only, see thread 6) · `bar_gap_watch_cron.sh` ·
`reconcile_alert_cron.sh` · `entry_fix_watch/watch_cron.sh` ⛔ **silence is NOT green — read
`STATUS.txt`** · OMS liveness · pre-open readiness · token expiry · OCO capture.
⛔ All ROOT crontab, ET-guarded **inside** the script. ⛔ A script committed from Windows lands mode
664 AND carries CRLF — verify `stat -c %a` and `bash -n` **on the box**.

## 🔑 SCHWAB TOKEN
Re-authed **2026-08-12 05:21 ET** ⇒ next expiry **Wed 2026-08-19 05:21 ET**. Off the Monday slot,
but ⚠️ 05:21 is **before** the 07:00 EH open — re-auth Tue evening or Wed after the close to park it
in dead hours. ⛔ Never quote this date from memory; read `refresh_token_expires_at` in the store.

## 🧠 MEMORY POINTERS (auto-load each session)
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_restart_bar_gap_checklist]] **(READ BEFORE ANY RESTART)** ·
[[project_mai_tai_cancel_is_fire_and_forget]] · [[project_mai_tai_probe_w_webull_stoplimit_master]] ·
[[project_mai_tai_resting_does_not_avoid_the_spread]] ·
[[project_mai_tai_premarket_exit_protection]] · [[feedback_the_brokers_book_is_shared]] ·
[[feedback_check_which_parts_already_work]] · [[feedback_a_wrong_reason_is_worse_than_a_missing_one]]
