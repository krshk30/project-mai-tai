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

# 🚨 TOMORROW MORNING: TWO THINGS, BOTH FAIL SILENTLY

**1. The census WILL jump. That is the fix firing, not a problem.** #734 shipped tonight and adds
the **BOUNDARY** gap check. **181 of 194 exposed symbols are the class it newly catches.**

**2. ⛔ #734 IS DEPLOYED BUT UNEXERCISED.** Tonight's census read `truncations=0 of 0 symbols
seeded` — the operator emptied the watchlist at 16:29 before the restart, so **no seed ran**.
Tomorrow's 04:00 roll is its **first real test**. Deployed ≠ proven.

⇒ Watch `[V2-DB-SEED-GAP]` through **04:00–11:00 ET**. A boundary refusal reads
`dropped ALL n seed bars`. The new **04:00–11:00 cron** now runs the detector every 5 min and alerts
on CHANGE only (`/home/trader/project-mai-tai/ops/health/seed_exposure_cron.sh`).

---

## ⚡ FIRST SCREEN

**2026-08-19 EOD.** Fleet **7/7**. Account FLAT, **0 open managed rows**, nothing working.
**Box HEAD `f18132e7`** (was `1a26f43` — **19 commits**), `src` diff vs origin/main = **0**.

**⛔ v2 IS THE ONLY SERVICE RUNNING THE PULLED CODE.** Files written **20:36:13 UTC**, v2 process
started **20:36:31 UTC** (after files). Every other service still runs pre-pull code:

| service | process start | running pulled code? |
|---|---|---|
| **schwab-1m-v2** | 08-19 20:36:31 UTC | ✅ YES |
| **oms** | 08-17 21:50:03 UTC | ⛔ **NO — on disk, not running** (#735/#736/#737) |
| strategy · market-data · control · reconciler · market-capture | 07-08 → 08-17 | ⛔ NO |

> ### ⛔⭐⭐ NEW STANDING RULE — `src diff = 0` IS NO LONGER EVIDENCE
> From 2026-08-19 the box carries code it is not running. Every deploy report after a partial-restart
> pull must state, **per service**, the **file-write time and the process-start time side by side**.
> The table above is the evidence; the diff is not.

---

# 📋 THE BOARD

The operator maintains the live board (lamps/filters). This file records only what the board cannot:
**dated triggers, tonight's state, and the flags that make a number readable.**

## 🗓️ DATED — a trigger nobody wrote down never fires
| when | what |
|---|---|
| **THU 08-20 eve** | **OMS deploy — #735 + #736 + #737** (one bundle by FILE, separable by signal). ⛔⭐⭐ **SET `MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_RESTING_MIRROR_ENABLED=true`** — it is **false** now. Miss it and #735 ships and does NOTHING, and the acceptance fails for the wrong reason. **OMS first, then v2 with the flag.** |
| **FRI 08-21 am** | **#735 acceptance, pre-committed.** orb entry fills/day **6–7 → 12–25** with Schwab's rate side by side · mirror STOP_LIMIT rejects **→ 0** (720 since 08-14) · `[WEBULL-BARE-FILL]` per-session count, **expected ~9/day**. ⛔ **>20 bare fills ⇒ STOP and report before Monday.** ⛔ A quiet Friday is a **non-result**, not a pass. ⛔ #736's signal is the opposite: **expect ZERO `[OCO-TARGET-BELOW-FILL]` lines** — one appearing IS the finding. |
| **FRI 08-21 eve** | **Q1** — the `source` column (our aborts vs broker rejects). |
| **MON 08-24** | **#13** weekend-outage re-check — needs a 2nd weekend in the retained logs. |
| **MON 08-25, before 16:46 ET** | **SCHWAB RE-AUTH**, `https://project-mai-tai.live/auth/schwab/start`. ⛔ **MANUAL ONLY.** Miss it and Tue 08-26 pre-market opens with no token. |

## ⛔ STANDING — from the moment #735 is live
**`preflight_oms_restart.sh` runs before EVERY OMS restart.** Installed at
`/home/trader/ops_preflight/`, md5-identical to the repo copy. It is standalone tooling — **it does
not gate itself, so the discipline is to run it.** Bare Webull fills will exist; a restart without it
can leave one uncovered.

---

## ⛔ FLAGS ON EVERYTHING — read before quoting a number

1. **Schwab-vs-Webull comparisons are VOID STRUCTURALLY, not since a date.** When the Webull leg is
   present its order type is refused client-side, re-sent as a different type minutes later at a
   different price, and exits on its own OCO.
2. **STKH cannot be R1's reference** — >4d gap population, and the only symbol that ever matched.
3. **⭐ First-vs-reclaim keys on `cw_entry_n` (97%), NEVER `cw_arm_bar_ts` (53%).** The missing half
   is **leg-structured** — schwab resting 20%, reactive 100%, eh_resting **0%** — so grouping on the
   segment id **re-weights** a study toward reactive and excludes EH resting entirely.
4. **Every reject count is contaminated** until Q1 lands — `broker_order_events` stores our own
   aborts as broker rejects.
5. **`trade_reasons.py` is enforced NOWHERE.** It bans substring-matching reason strings and has no
   consumer. Reading its docstring and assuming the rule is applied is wrong.

---

## 🔬 THE DAY'S ROOT CAUSES — two, both ours

**⛔⭐⭐ THE WEBULL MIRROR WAS BORN BROKEN, AND WE REFUSED IT — NOT WEBULL.**
`rth_resting_mirror`: **720 orders, 0 fills**, first seen **08-14**. It did not *die*; it never
worked. The strategy emits that leg **BARE on purpose** (Probe W: Webull ACCEPTS a stop-limit master
standalone, 200). `_apply_v2_oco_bracket_entry` had **no broker scope**, stamped a Schwab bracket
onto it, and our own adapter guard aborted it **client-side**.
⭐ **Control group we did not have to build: the only 2 mirror fills ever are the 2 orders that
escaped the stamping.** Fixed by #735, scoped narrowly (`webull` + `STOP_LIMIT`) so the **174 live
bracketed LIMIT fan-outs** keep their protection.

**⛔⭐⭐ #721 HAD A SECOND HOLE — THE BOUNDARY GAP.** Its walk compared **adjacent loaded bars only**,
so a wholly-stale but internally-contiguous history seeded **in full, with no log line**. **178
symbols** were in that state, 600–780 bars each, **35–62 days stale**. Fixed by #734.
⛔ The fix is **not** `_missed_sessions_between(newest, now)` — that would wipe every symbol's
history every pre-open. See the memory.

---

## 🧠 STANDING RULES EARNED TODAY

1. **⛔⭐⭐ TRUNCATING MY OWN OUTPUT IS A WRONG ANSWER, NOT AN ERROR.** Three confident wrong
   conclusions in one day: reject reasons cut at 110 chars (85/79/57 → truly **92/73/58**), a
   crontab under `head -45`, and a fills query under `head -24` that made a real Webull fill look
   like a **phantom managed row**. ⭐ The tell each time was **a number that did not reconcile**.
2. **⛔⭐⭐ A BROKER-SHAPED RULE NEEDS A BROKER SCOPE — and the signature is SEMANTIC.** The syntactic
   grep returns 10 hits; **8 clear**. The test is *does this rule MEAN something different at Schwab
   than at Webull?* Adding a scope where it does **not** differ is its own defect.
3. **⛔⭐ ARMED IS NOT A POSITION.** It is bar-driven, bars flow to 20:00, so arming after the 16:00
   entry-window close is **normal**. ⛔ **Stopping a symbol FREEZES its arm** instead of clearing it —
   which is how tonight's restart gate went permanently red.
4. **⛔ A HELPER'S CONTRACT INCLUDES THE TYPE OF ITS ENDPOINTS.** Reusing one with a different
   endpoint kind is a **new function**, not a call.
5. **⛔ Piping a test run into `tail` destroys its exit status** — a green-looking tail said nothing,
   and I pushed a commit with 5 failing tests.

---

## 📌 OPEN, NOT ON THE BOARD

- **The CAST seed-cap miss is UNEXPLAINED again.** My theory died: the guards read the **state**
  field, which is never 0 (**0 of 1621 arms**), and the cap has fired **36 times** — including for
  CAST on 08-18. ⛔ **Any "delete the dead seed cap" item rests on a premise the data contradicts.**
- **§82 has THREE causes, not two.** #739 fixes the reactive latch (14 of 19). Still live: the claim
  **expiry** re-opening on `position_qty == 0` (a Webull fill does not raise it), and the
  **phantom-close** path re-arming `fanout_webull_claimed`.
- **Reboot backlog** — 8 pending kernels + `libc6`, **125 days uptime**; a reboot restarts all 12
  services at once.
- **P9 corroboration must come from a READ, not an order.** 58 symbols are excluded from every replay
  universe on **one broker sentence each**, and retrying is impossible (we cache ineligibility).
  Whether Schwab's instrument metadata carries a broker-only flag is **unverified**.
- **Box-vs-repo file copies re-diverge on every deploy.** `preopen_readiness_cron.sh` is a real file,
  not a symlink; it diverged on tonight's pull exactly as predicted and was synced by hand.

## 🧠 MEMORY POINTERS
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_webull_mirror_born_broken]] · [[project_mai_tai_broker_shaped_rule_needs_broker_scope]] ·
[[project_mai_tai_armed_is_not_a_position]] · [[feedback_truncated_output_is_a_wrong_answer]] ·
[[project_mai_tai_db_seed_by_count_injects_stale_bars]] · [[project_mai_tai_backtest_live_parity_audit]] ·
[[project_mai_tai_restart_bar_gap_checklist]]
