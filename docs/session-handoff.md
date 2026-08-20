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

# 🚨 TOMORROW MORNING: ONE TEST, AND FOUR SIGNALS THAT DO NOT EXIST YET

**1. The 04:00 roll is the FIRST REAL TEST of #743.** Tonight's fail-open count is **0 since boot
against 24 in the pre-restart process** — a good control, but only ~2 minutes of runtime with **no
seeding**, and the fail-opens happen *during* seeding. **Consistent with working; not proof.**
Watch `[V2-DB-SEED-GAP]` and the census through **04:00–11:00 ET**.

**2. ⛔ SIGNALS 1–4 DO NOT EXIST YET AND MUST NOT BE READ AS PASSES.** The mirror flag went live at
**16:16 ET, after the entry window shut**. Nothing has had the opportunity to reject, fill, or
duplicate. Friday's session is the first population. **A quiet Friday is a NON-RESULT.**

---

## ⚡ FIRST SCREEN

**2026-08-20 EOD.** Fleet **7/7 active**, 0 failed. Account **FLAT**, 0 open managed rows.
**Box HEAD `2a43b29`** (was `f18132e7`). Deploy ran 16:13→16:19 ET; every gate passed.

| service | process start (UTC) | running pulled code? |
|---|---|---|
| **oms** | 08-20 20:14:49 | ✅ YES — #735/#736/#737 (from disk) + **Q1 #746** |
| **strategy** | 08-20 20:14:49 | ✅ YES — restarted BY the OMS deploy (see below) |
| **schwab-1m-v2** | 08-20 20:16:46 | ✅ YES — #743 + B19/B20 #747 + the flag |
| market-data · control · reconciler · market-capture | 07-27 / 07-14 / 08-14 / 07-08 | ⛔ **NO — on disk, not running** |

**Files written `2026-08-20 20:14:28 UTC`.** ⛔ **`src diff = 0` IS NOT EVIDENCE** — this table is.

**The two opt-ins, verified FROM THE SINK, not from the commands that set them:**
- flag @ `/proc/845419/environ` → **`…WEBULL_RESTING_MIRROR_ENABLED=true`**
- `event_source` in `information_schema` → **1 row**; alembic head **`20260820_0015`**

> ### ⛔⭐⭐ THE OMS DEPLOY IS NOT OMS-ONLY
> `deploy_service.sh` does **stop strategy → restart oms → start strategy**. The strategy service
> restarts every time the OMS does, and picks up whatever is on disk. Use `hold_strategy: true` to
> prevent it. Any per-service expectation table that says "strategy: expect NO" is wrong.

---

# 📋 THE BOARD

## 🗓️ DATED — a trigger nobody wrote down never fires
| when | what |
|---|---|
| **FRI 08-21 am** | **Grade the six signals against `docs/deploy-2026-08-20-window.md` §3**, not against a clean-looking log. ⛔ Fix the collector's `--since` FIRST (below) or it grades the old process too. ⛔ A quiet Friday is a **NON-RESULT**. |
| **MON 08-24** | **#13** weekend-outage re-check — needs a 2nd weekend in the retained logs. |
| **MON 08-25, before 16:46 ET** | **SCHWAB RE-AUTH**, `https://project-mai-tai.live/auth/schwab/start`. ⛔ **MANUAL ONLY.** ⛔ **Read the expiry FROM THE STORE on the day — never from memory or from this line.** |
| **AFTER Q1 IS DEPLOYED *AND PROVEN*** | **§178** — revisit B9 cause 2's release. Ruled **STRICT** (`position_qty == 0 AND fanout_qty == 0`). ⛔ Q1 is now DEPLOYED; it is **not PROVEN** — every pre-migration row is `unknown`, so a count spanning the boundary is not a clean split. |
| **AFTER #739 IS MERGED, DEPLOYED *AND MEASURED*** | **B9 cause 3 build.** ⛔ **#739 is still `OPEN`** — not merely un-deployed, **unscheduled**. |

## ⛔ STANDING
- **`preflight_oms_restart.sh` before EVERY OMS restart.** It does not gate itself. Ran clean tonight.
  ⛔ The repo copy at `ops/health/` is **MISSING** — the box copy is the only one, so the md5 check
  has nothing to compare against.
- **Both opt-ins are verified from the SINK.** Setting a switch and confirming a switch are
  different facts.

---

## ⛔ FLAGS ON EVERYTHING — read before quoting a number

1. **Reject counts remain contaminated for any window spanning tonight.** `event_source` populates
   from 20:14 UTC forward; everything before is `unknown` **by design**.
2. **Schwab-vs-Webull comparisons are VOID STRUCTURALLY.** Signal 2 puts the two rates **side by
   side**; it never differences them.
3. **⭐ First-vs-reclaim keys on `cw_entry_n` (97%), NEVER `cw_arm_bar_ts` (53%).** The missing half
   is leg-structured, so grouping on the segment id re-weights toward reactive.
4. **`trade_reasons.py` is enforced NOWHERE** — it bans substring-matching reason strings and has no
   consumer. `event_source` is what replaces that habit.
5. **`virtual_positions` has a known FALSE-ZERO** — never read flat from it alone. Tonight's flat was
   corroborated by `oms_managed_positions` against a real denominator (40 `closed`).

---

## 📌 OPEN, NOT ON THE BOARD

- **⛔ #751 (evidence collector) needs a `--since` BEFORE Friday.** It counts signals 3/6/#736 across
  **all rotations**, so its POST run reported fail-open **30** where the restart-scoped truth is
  **0**. Left as-is it grades the old process alongside the new one. PR open, unmerged.
- **⛔ Signal 2's DEFINITION is unresolved.** The sheet's baseline is 6–7/day; the collector's query
  reads 8–10 — most likely because it counts any filled `limit`/`market` on `live:orb`, which may
  include **closes**. Not a reason to move the goalposts; a reason to grade with **one stated
  definition** and to say which.
- **⛔ Signal 4 has NO PINNED QUERY** — reports **UNMEASURED**, never 0.
- **⛔ #736's watch is UNEXERCISED** — `[OCO-TARGET-BELOW-FILL]` has never matched anything. Its zero
  is *consistent with* success and is not *evidence* of it.
- **⛔⭐⭐ §180 — THE FAN-OUT SLOT ACCOUNTING IS WRONG.** `_fetch_position_maps` is Schwab-scoped; the
  fan-out leg fills on `live:orb` ⇒ a Webull-only fill moves **neither** `position_qty` nor
  `position_qty_held`. The `update_position` comment asserts the opposite — true about
  `SymbolState`, irrelevant, because **the QUERY that feeds it is per-account**. Own item.
- **P2 replay rebuild** — needs redoing: **P21 changed what the replay reports** (unmodellable
  trades are now DROPPED and counted, never booked).
- **The unified gap check** downstream of both feeds — Q11 came back **6 of 43** ⇒ not urgent.
- **The CAST seed-cap miss is UNEXPLAINED.** The guards read the **state** field, never 0.
- **Reboot backlog** — 8 kernels + `libc6`, **~18 weeks uptime**; a reboot restarts all 12 services.

---

## 🧠 RULES EARNED 2026-08-20

1. **⛔⭐⭐ §179 MERGING IS SCHEDULING.** Merged ⇒ ships on the next deploy of whatever service it
   touches. B19/B20 and Q1 moved onto tonight by being merged. **The rule only works with no
   exceptions** — which is why cause 3 was NOT built-and-held-unmerged.
2. **⛔⭐⭐ §183 VERIFY A FAILURE MODE AT THE CATCH SITE, NEVER AT THE RAISE.** I proved the missed
   migration *raises*; every one of the six paths swallows it with `except Exception` + log. And it
   is **not observability — it drops FILLS**: `append_order_event` runs BEFORE
   `record_fill_if_needed` and `apply_fill_to_positions`. ⭐ Ask where the failing call sits in the
   **sequence**, and **would this failure disable its own detector?** (It would: the first
   swallowing path was the Webull mirror — the instrument for tonight's own signals.)
3. **⛔⭐⭐ A TIMESTAMP FILTER THAT STRING-COMPARES AGAINST MULTI-LINE RECORDS IS NOT A TIME FILTER.**
   `awk '$0 >= "<ts>"'` passed every traceback line in the whole file, manufacturing "48 tracebacks
   since boot" (truth: **0**). A case-insensitive grep for `error` manufactured "230 error-ish OMS
   lines" out of Webull `error_code` payloads (truth: **0 tracebacks**). ⭐ Both tells were the same:
   **a number that did not reconcile with the tail I could see.**
4. **⛔⭐ §181a** — a test covering the HELPER but not the WIRING cannot see a dead call site.
   **§181b** — a stub that already satisfies a fallback never exercises it. Both mutants escaped.
5. **⛔ §180** — a wrong COMMENT is a wrong reason, and code rests on it.
6. **⛔⭐ WHEN THE SUCCESS CRITERION IS ZERO, PROVE THE WATCH AGAINST A KNOWN-POSITIVE FIRST.**
   Signal 1 was a log grep returning 0 while `broker_orders` held the 720 — a broken watch and a
   passing deploy are the same number.

## 🧠 MEMORY POINTERS
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_db_seed_by_count_injects_stale_bars]] · [[project_mai_tai_armed_is_not_a_position]] ·
[[project_mai_tai_broker_order_events_conflates_client_aborts]] · [[feedback_verify_before_concluding]] ·
[[feedback_truncated_output_is_a_wrong_answer]] · [[feedback_mutate_the_code_pin_the_threshold]] ·
[[project_mai_tai_webull_mirror_born_broken]] · [[project_mai_tai_restart_bar_gap_checklist]]
