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

**As of 2026-08-13 EOD.** Fleet active. **Deployed HEAD `3ac4721`** (verified in the checkout AND by
content). Account **FLAT** on both live accounts at close; nothing working at either broker.

✅ **DEPLOYED 17:35 ET, SEVEN PRs, BOTH NEW FLAGS ON** (operator's explicit call, twice):
```
MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_RESTING_MIRROR_ENABLED=true
MAI_TAI_OMS_V2_EXIT_RELEASE_RESERVATION_ENABLED=true
```
Both confirmed from each process's own `/proc/<pid>/environ`, not from the env file.
⛔ **Neither key existed before tonight** ⇒ rollback = delete the two lines + restart. (An env
backup was attempted and produced **no file** — do not rely on one existing.)
⛔ **BOTH ARE UNEXERCISED.** Zero markers fired: the account was flat from the deploy to the close.

⭐ **Restart touched OMS + strategy + schwab-1m-v2. NO BAR HOLE** — zero missing minutes across the
restart, verified by a per-minute gap query, not by eyeballing the newest bar.

## 👀 WATCH TOMORROW (2026-08-14) — these separate the two changes

| marker | means |
|---|---|
| `[V2-WEBULL-RESTING-PLACE]` | the mirror put a REAL resting order at Webull |
| `[WEBULL-PROTECT-ATTACHED]` / `-FAILED` | the stop+target pair went on after a BARE fill |
| `[OMS-EXIT-RELEASE]` | resting legs cancelled before a software close |
| `[OMS-EXIT-REPROTECT]` | a close would not go through ⇒ protection restored |
| `live:orb` reject count | **58 today.** It should collapse. |

⛔ A `[WEBULL-PROTECT-FAILED]` means **we are holding with no broker-side stop.** Act, don't log it.

## 🔴 THE DAY'S SHARPEST FINDING — the close was fighting our own exit legs

**A resting exit leg RESERVES the position.** The v2 software ladder then sends its own market sell
for those same shares, Webull sees available-to-sell = 0, and refuses it as a naked short:

| n | reason (verbatim, `live:orb`, 2026-08-13) |
|---|---|
| 39 | `NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K` |
| 19 | `ORDER_NOT_SUPPORT_REVERSE_OPTION` |

56 of 58 were **one XHG share**; 48 of those inside five minutes. These are genuine broker HTTP 417
payloads, **not** the client-abort conflation.

**⭐ THE ASYMMETRY IS A MISSING CAPABILITY, NOT A BUG IN EITHER BROKER.**
`-close-` filled **4/62 at Webull vs 5/6 at Schwab**. Schwab stands the ladder down while a bracket
is armed (`_native_oco_stand_down_active`); Webull exposes no `fetch_armed_native_oco_symbols`, and
`routing.py` **fails OPEN by design** — so the ladder fires straight into its own reservation.

**⛔ NOTHING DETECTED IT.** `_V2_EXIT_ABANDON_AFTER_FAILURES = 8` resets to 0 on any positively-HELD
read — and we genuinely *do* hold the share, it is merely reserved — so **the bound is unreachable.**
The A2 backoff matches `ORDER_NOT_SUPPORT_REVERSE_OPTION` but its flag ships OFF and it does not
match the other string at all. No distinctive marker, no surviving counter, no alert.

**⭐⭐ THIS ALSO EXPLAINS 08-12 CRWU** — the "held position with nothing trying to sell it" had the
same two reject strings. It was not a mystery; it was this.

**⛔ BUT THE COST DID NOT MATCH THE ALARM.** Bid at the first blocked attempt vs the price actually
taken, n=5: better in 2, **worse in 3**, median **−0.51 pp**. Every position exited via its OCO leg.
The count screamed; the money shrugged. Count says urgent, cost says no — same as the vol-floor flap.

## ✅ MERGED + DEPLOYED TODAY (`3ac4721`)

| PR | what |
|---|---|
| #687 | release a fan-out claim that never filled — one rejection used to burn the whole flip |
| #688 | **both legs REST at their own broker**; the Webull watcher was racing the fill and losing |
| #689 | attach a REAL stop+target at Webull after a bare resting fill |
| #690 | attach leg ids could exceed Webull's 40-char cap — landed **exactly on 40** |
| #691 | the close now CANCELS the resting exit legs first |
| #692 | re-attach protection if that close will not go through |
| #693 | commit the exec bit on the two crons that run from the repo path |

**Validation:** suite **2062 pass** · ruff clean · **18 mutations, all killed.**
⛔ Two mutations initially survived and **both were my error, not weak tests** (one removed only a
sleep, one targeted code the test replaces with a double). A third survived legitimately and exposed
a real gap — the attach was not recording its base id — which is now pinned.

**⭐ WHY #691 IS EVEN POSSIBLE:** OCO children are broker-created and never land in `broker_orders`,
so they cannot be looked **up** — but they carry **deterministic** coids
(`_combo_leg_coid(base,"T"/"S")`), so they can be **addressed by name**.

**⛔ #691's HAZARD, closed by #692:** cancelling the legs removes the net that used to take the
position out at +2%/−5% when a close kept failing. Without #692 it would be strictly worse than the
storm it replaces.

## 🕐 OPEN PRs
| PR | what |
|---|---|
| #682 | entry-slippage validation — needs sudo; unrun |
| #673 · #646 · #642 · #640 | docs/design; awaiting review |

## 🔴 OPEN THREADS (detail: [`handoff-open-items.md`](handoff-open-items.md))

1. **⛔⭐⭐ THE RETRY BOUND IS STILL UNREACHABLE.** `_v2_exit_close_failures` resets on every HELD
   read, so *any* reject on a position we really hold retries forever. #691 removes the **cause** of
   these rejects, **not** the missing bound underneath. **This is the next fix.**
2. **🔴 The 16:00 bracket death** — #647 Gate 2 (`oms_v2_rth_edge_bracket_enabled`) built, still dark.
3. **⛔⭐⭐ PRE-MARKET IS 0% BRACKETED.** 14d: RTH **172/172** orb, **131/132** schwab; PRE **0/34**,
   **0/13**. Both brokers are limit-only in EH — the RTH-edge arm is the only answer.
   ⭐ The mirror is **RTH-only** (`_queue_resting_place` returns on the EH branch *before* the mirror
   block), so #689's attach never runs in EH — which is where its shape was never proven. No gap.
4. **⛔⭐ ENTRY LEVELS QUANTISED BY ~70 bps** — +2%/−5% tick-rounded off the decided price; the 0.5%
   band collapses to **ZERO** under ~$2. **Fix the unit, not the number.**
5. **⭐⭐ CHURN IS STILL THE BIGGEST NUMBER** — median 5 entries/symbol-day, max 16; ~200% of one
   position's notional in crossing over 14d. Selection is **DISCUSS BEFORE BUILDING**.
6. **⛔ The orphan watch reads SCHWAB ONLY.** It can never clear a Webull question.
7. **⛔⭐⭐ `broker_order_events` conflates CLIENT aborts with BROKER refusals** — needs a `source`
   field. (Today's 58 were verified genuine by reading the verbatim 417 payloads.)
8. **⚠ A pre-existing flaky test:** `test_scanner_cycle_history_retention_and_dedup` failed once in a
   full run, passes alone, passes with all 218 in its file, passes on re-run. Cross-file ordering.
9. **P0 boot-hold freshness gate** · **Redis evicts the heartbeat stream** · **per-lot attribution
   gap** — unchanged.

## 🔔 ALERTING
`orphan_order_cron.sh` (Schwab-only) · `bar_gap_watch_cron.sh` · `reconcile_alert_cron.sh` ·
`entry_fix_watch/watch_cron.sh` ⛔ **silence is NOT green — read `STATUS.txt`** · OMS liveness ·
pre-open readiness · token expiry · OCO capture.
⛔ All ROOT crontab, ET-guarded **inside** the script.
✅ **FIXED TODAY (#693):** `bar_gap_watch_cron.sh` + `reconcile_alert_cron.sh` were 100644 in git and
chmod'd by hand on the box, leaving the VPS tree **permanently dirty** — which **BLOCKS EVERY
DEPLOY** (`refusing deploy because repo has local changes`). ⛔ Do NOT `git checkout` them to unblock:
root's crontab invokes both **directly by path**, so reverting to 100644 silently kills the bar-hole
watch and the drift alarm. The exec bit is now committed.
⛔ `schwab_token_expiry_cron.sh` is still 100644 but runs from a **separate `/home/trader/` copy** —
its repo mode is not load-bearing. Do not "fix" it.

## 🔑 SCHWAB TOKEN
**Read from the store 2026-08-13 17:40 ET:** `refresh_token_expires_at = 2026-08-19T09:21:35Z`
⇒ **Wed 2026-08-19 05:21 ET.** Store mtime fresh ⇒ the refresher is alive.
⚠️ 05:21 is **before** the 07:00 EH open — re-auth Tue evening or Wed after the close.
⛔ **Never quote this date from memory; read `refresh_token_expires_at`.** A memory file carried a
wrong date (`Mon 08-17`) into this session — that is exactly why this rule exists.

## 🧠 MEMORY POINTERS (auto-load each session)
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_restart_bar_gap_checklist]] **(READ BEFORE ANY RESTART)** ·
[[project_mai_tai_exit_reservation_conflict]] **(today's finding)** ·
[[project_mai_tai_cancel_is_fire_and_forget]] · [[project_mai_tai_probe_w_webull_stoplimit_master]] ·
[[project_mai_tai_two_exit_routes_close_unattributable]] ·
[[project_mai_tai_premarket_exit_protection]] · [[feedback_the_brokers_book_is_shared]] ·
[[feedback_commit_before_you_mutate]] · [[feedback_a_wrong_reason_is_worse_than_a_missing_one]]
