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

# 🚨 MONDAY: THE CODE IS ON THE BOX. GRADE #739 FIRST, THEN MERGE #766.

**1. ✅ THE DEPLOY BLOCKER IS SOLVED, AND IT WAS THE SERVICE NAME.** `Deploy Service`'s `service`
input is the only `required: true` with **no default**, and it is a `choice`. A missing *or
misspelled* value is rejected **422 with no run created** — no trace anywhere but the caller's
terminal. ⛔⭐⭐ **The dispatch takes `schwab-1m-v2` (HYPHENS). The code slug is `schwab_1m_v2`
(UNDERSCORES) and is refused outright.** Probed A–F with a passing control; see the day log.

```
gh workflow run deploy-service.yml --ref main -f service=schwab-1m-v2 -f run_migrations=false
```
**Landed** = exit 0 / raw API **204 empty**, then `gh run list --workflow=deploy-service.yml
--limit 1` shows `branch=main` within ~5 s. Empty listing = **no run**; re-read the error, do not wait.

**2. ⛔ GRADE #739 ON FRIDAY'S NUMBERS BEFORE MERGING #766.** #766 moves signals 1 and 3 and every
`[WEBULL-PROTECT-*]` count. Nothing graded across that boundary is comparable.

**3. ⚠ SIGNAL 4's DENOMINATOR WILL SHRINK, AND THAT IS #765 WORKING.** More truncations ⇒ fewer
symbols arm. It was already only 2. Do not read a smaller denominator as signal 4 degrading.

---

## ⚡ FIRST SCREEN

**2026-08-23 (Sun) 09:45 ET.** Fleet **7/7 running**, account **FLAT** — 0 non-terminal orders
across **all** broker accounts (auditable: the entire status vocabulary in 7d is
`filled`/`rejected`/`cancelled`, all terminal), 0 non-zero of 1033 `account_positions`, 0 non-zero
of 842 `virtual_positions`, 0 open `oms_managed_positions`. **Box HEAD `253752a` — 0 behind main.**

| service | process start (UTC) | running pulled code? |
|---|---|---|
| **schwab-1m-v2** | **08-23 13:35:18** | ✅ **has #739 AND #765** |
| oms · strategy | 08-20 20:14:49 | ⛔ on disk, not running (#758/#760/#755/#766 all unrun) |
| market-data · control · reconciler · market-capture | 07-27 / 07-14 / 08-14 / 07-08 | ⛔ on disk, not running |

⛔ **`src diff = 0` IS NOT EVIDENCE — this table is.**

---

# 📋 THE SIX-SIGNAL GRADE — Friday's numbers are the PRE-FIX BASELINE, still valid

Graded 08-21 after the 16:00 ET close against `2a43b29`. **v2 now runs newer code; OMS does not.**

| # | signal | 08-21 reading | verdict |
|---|---|---|---|
| 1 | mirror legs reaching the venue (§194) | 152 emitted · **6** became an order · 3 filled · 13 refused | result |
| 2 | entry fills/day (§186 filled BUYS) | orb 7 · schwab 5 | **thin** |
| 3 | `[WEBULL-BARE-FILL]` | 8 started bare, **3 STAYED bare**, 5 protected in seconds | pass (<20) |
| 4 | duplicate legs/segment (§185 pinned) | 0 of **2** segments | ⛔ **NON-RESULT** |
| 5 | seed-gap census denominator | `truncations=5 of 13` | ✅ PASS |
| 6 | seed-gap **fail-open** | **2** | ⛔ **FAIL — addressed by #765** |

⛔ Signal 4 stays NON-RESULT: the control reproduced `119|19|22`, so the instrument is sound — the
denominator is simply 2, and **6 filled fan-out legs carry no segment id at all.** Do not let
Monday inherit a softened version.

---

# 🔴 THE THREE THAT MATTER MOST

## 1. ⛔⛔ THE ATTACH SUCCEEDS AND WE RECORD IT AS A REFUSAL — #766, HELD
`price=` vs **`fill_price=`**: the `ExecutionReport` constructor raises **after** `place_order`
returns a `combo_order_id`, so `submit_order` returns a **`rejected`** report (⛔ *not* an empty
list — a non-empty list of one reject) and the OMS success branch never runs. That branch is what
stores `_webull_protect_base[…] = coid`, **the only handle on legs the broker creates and never
lists.** Retries 2–5 then fight our own live pair (`ORDER_NOT_SUPPORT_REVERSE_OPTION` ×56).

⛔⛔ **NOT "succeeding all along" — it began on 08-21.** `[WEBULL-EXIT-PAIR-PLACED]` is 0 across
08-16→08-20 and **5 on 08-21** (SUGP, JUNS, USDE, EXYN, **USDE again**) — ⛔ **five, not four.**
"0-for-EVER" was true for its own window. *A correction is a claim too, and needs its own denominator.*

⚠ **Five broker-created pairs had their handle discarded.** `broker_orders` never held them by
construction ⇒ **no query of ours can confirm they are gone. The screen outranks our logs.**
⛔ **§220 dependency:** when #766 lands, re-put the ladder-only decision with the honest number —
**3 of 8**, extended hours only, stop priced below market.

## 2. ✅ #743's SUCCESSOR SHIPPED — the guard failed in the case it exists to catch
`lo` is the day after the newest bar, so **the window width IS the staleness**. 83-day window =
3580 ms (72% of the 5 s timeout, **idle**) → fail-open → LSTA seeded **May** bars on **Aug 21** and
armed off them. `EXISTS` answers the same question in **0.182 ms**. Live since 09:35 ET.
⛔ `SELECT DISTINCT … LIMIT 1` was **measured and rejected** — HashAggregate cannot emit early.
**Monday is the first real exercise: signal 6 must read 0, and it CANNOT be graded intraday.**

## 3. ⛔ THE RECONCILER CANNOT SEE THIS CLASS AT ALL
Every check compares the venue against **our own tables**, so an order we never recorded is
invisible **by construction**. No `cancel_all`, no venue-side `list_open_orders`. That is exactly
why the five orphaned pairs above are unanswerable from here.
⛔ `account_positions` + `virtual_positions` + `oms_managed_positions` are **ONE source**: blind,
derived-from-it, and our bookkeeping. **`fills` is the only independent ledger.**

---

## 🗓️ DATED
| when | what |
|---|---|
| **MON 08-24** | **SCHWAB RE-AUTH.** Read from the store 08-23: `refresh_token_expires_at = 2026-08-25T20:46:01Z` = **Tue 08-25 16:46 ET**, mid-session. ⛔ MANUAL, cannot ride a deploy. ⛔ **TWO FIELDS** — `expires_at` is the short-lived ACCESS token the refresher rotates itself, a ready-made false alarm. Read the store, never memory. |
| **MON 08-24 post-close** | **Grade #739 FIRST**, then merge+deploy **#766**, then OMS (#758, #760, #755), then v2 (#761). Actions page open; confirm the run before reporting it. |
| **MON 08-24** | #13 weekend-outage re-check (needs a 2nd weekend retained). |

## 📌 OPEN PRs
`#766` **attach fix — HELD until #739 is graded** (validate green) · `#755` Q12 audit-write ·
`#756` preflight fences (**held — only-change window**) · `#758` origin/reason · `#759` Q5 pager ·
`#760` BROKER-SYNC-OK census · `#761` reclaim live-bar + slot_consumed · `#763` B28+B29.
✅ Merged today: **#765** (§256). ⛔ **#762 is already CLOSED unmerged (08-21)** — `cf64e6b5` is not
an ancestor of `main`. Not drifting; do not re-triage it.

## 🧠 RULES EARNED 2026-08-23
1. **⛔⭐⭐ A CORRECTION NEEDS ITS OWN DENOMINATOR.** "Succeeding all along" overshot the evidence in
   the opposite direction from "0-for-EVER" — both were absences read past their population.
2. **⛔⭐ MEASURE THE ALTERNATIVE BEFORE RECOMMENDING IT.** The obvious `LIMIT 1` rewrite is not a fix.
3. **⛔⭐ A MUTATION HARNESS MUST RESTORE IN A `finally`** — one crashed mid-run and left a mutant in
   the source. Restore is now re-verified **by content**.
4. **⛔ TEST THE SEAM, NOT JUST BOTH SIDES.** Two green files, seven days, one broken joint — each
   fed a fixture standing in for the other.
5. **⛔ A PERMISSION-DENIED READ IS NOT A CLEAN ONE.** `tail` on a `root:root 640` log returned an
   empty error census. Confirm by CONTENT.

## 🧠 MEMORY POINTERS
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_reprotect_chain_uncovered_window]] (⛔ re-censused 08-23 — 5 placed, 0 recorded) ·
[[project_mai_tai_db_seed_by_count_injects_stale_bars]] (⛔ #765 now live, unproven until Monday) ·
[[project_mai_tai_false_flat_naked_position]] (the one-source chain) ·
[[feedback_an_absence_is_evidence_only_against_a_known_denominator]] (name the population) ·
[[feedback_verify_before_concluding]] (must-be-zero cannot be graded intraday) ·
[[feedback_fixture_must_match_production_config]] (the seam) ·
[[feedback_mutate_the_code_pin_the_threshold]].
