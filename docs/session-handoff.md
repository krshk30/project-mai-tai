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

# 🚨 MONDAY: THE DEPLOY DID NOT HAPPEN, AND TWO GRADES REVERSED

**1. ⛔ NOTHING DEPLOYED FRIDAY.** Two "deploy done" reports, **no workflow run either time**, box
unchanged across three readings 80 minutes apart. `main` is **8 commits ahead** of the box.
⇒ **Test the input-rejection hypothesis first:** a `workflow_dispatch` with a missing required
input is rejected outright and leaves **no run at all** — which matches the evidence exactly.
⇒ Monday post-close, **with the Actions page open. Confirm the run appears before reporting it.**

**2. ⛔⛔ `Deploy Main` IS A HAZARD — DO NOT USE IT (B30).** Its 5 runs (all June, all `failure`)
**deployed the code first** — pip installed, alembic ran — then failed a health gate. A failed
`Deploy Main` is **not a no-op**: it changes the box and reports failure, so a reader retries and
deploys twice believing they deployed zero times.
⇒ **The mechanism is `Deploy Service`, once per service.** That is what ran on 08-20 (20:14:16 oms,
20:16:26 v2). It was never written down, which is how Friday happened.

**3. ⛔ #743 IS NOT PROVEN. Signal 6 FAILED.** See the grade below.

---

## ⚡ FIRST SCREEN

**2026-08-21 EOD.** Fleet **7/7 running**, account **FLAT** (0 open of 749 · 0 non-zero of 289
`account_positions` · 0 working orders). Bar stream continuous, **no gap**.
**Box HEAD `2a43b29`** — unchanged since 08-20. Entry window closed 16:00 ET.

| service | process start (UTC) | running pulled code? |
|---|---|---|
| oms · strategy | 08-20 20:14:49 | ✅ (08-20's pull) |
| schwab-1m-v2 | 08-20 20:16:46 | ✅ (08-20's pull) |
| market-data · control · reconciler · market-capture | 07-27 / 07-14 / 08-14 / 07-08 | ⛔ on disk, not running |

⛔ **`src diff = 0` IS NOT EVIDENCE — this table is.** Friday's non-deploy would have passed a
health-only check cleanly: *7/7 running, flat, flag true, `event_source` present, alembic at head —
**all of that is true of the old code.*** That sentence is the argument for the pre/post table.

---

# 📋 THE SIX-SIGNAL GRADE — FINAL, against `2a43b29`

Graded after the **16:00 ET** entry-window close. Today's numbers are the **pre-fix baseline** and
stay valid whenever the deploy lands.

| # | signal | reading | verdict |
|---|---|---|---|
| 1 | mirror legs reaching the venue (§194 replacement) | 152 emitted · **6** became an order · 3 filled · 13 refused | result |
| 2 | entry fills/day (§186: filled BUYS) | orb 7 · schwab 5 | **thin** |
| 3 | `[WEBULL-BARE-FILL]` | **8 started bare, 3 STAYED bare**, 5 protected in seconds | pass (<20) |
| 4 | duplicate legs/segment (§185 pinned) | 0 of **2** segments | ⛔ **NON-RESULT** |
| 5 | seed-gap census denominator | `truncations=5 of 13` | ✅ PASS |
| 6 | seed-gap **fail-open** | **2** | ⛔ **FAIL** |

⛔ **Signal 4 stays NON-RESULT.** Control reproduced `119\|19\|22`, so the instrument is sound —
the denominator is simply 2, and **6 filled fan-out legs carry no segment id at all**. Do not let
Monday inherit a softened version.
⛔ **Any `live:orb` P&L for today is incomplete by FIVE exits** (9 buy fills, 4 sell fills).

---

# 🔴 THE THREE FINDINGS THAT MATTER MOST

## 1. ⛔⛔ THE ATTACH HAS BEEN SUCCEEDING ALL ALONG — `0 ATTACHED` IS AN ARTIFACT
`webull.py:261`, inside `_submit_exit_pair_blocking` (the attach's **success** path), passes
`price=None`. `ExecutionReport` has **`fill_price`**. The constructor raises *after* a successful
placement; the caller logs it as `Webull order rejected: TypeError(...)`.
**Webull returned a `combo_order_id` every time.** Four on 08-21 (SUGP, JUNS, USDE, EXYN).
⇒ Retries 2–5 then get `ORDER_NOT_SUPPORT_REVERSE_OPTION` — **fighting our own live pair.**
⇒ `THE POSITION IS HELD WITHOUT PROTECTION` is very likely FALSE.
⛔ **ONE CHANGE, TWO HALVES:** the kwarg **and** storing `_webull_protect_base[...]` — the success
branch is `if any(...)` over an **empty** list, so the only handle on a broker-created pair is also
lost. Fixing the kwarg alone leaves a recorded pair we still cannot cancel.
⛔ **SEQUENCING: grade #739 on today's numbers BEFORE this lands.** It moves signals 1 and 3 and
every `[WEBULL-PROTECT-*]` count. Nothing graded across that boundary is comparable.

## 2. ⛔ #743 NOT PROVEN — the fail-open returned the same day
`0` at 12:58 ET, **`2` by the close**; both `psycopg.errors.QueryCanceled: statement timeout` on
the **boundary** lookup #743 rewrote. Rate cut 24/day → 2/day; the timeout **survives**.
⇒ Next: *why* — same plan regression under load, a lock, or a different query on that session?
⛔ A lower refusal count is not a fix.

## 3. ⛔ THE RECONCILER CANNOT SEE THIS CLASS AT ALL (in `docs/architecture.md` now)
Every check compares the venue against **our own tables**, so an order we never recorded is
invisible **by construction**. There is no `cancel_all`, no venue-side `list_open_orders`, no
adapter enumerating broker orders — which protected us Friday, but that is **luck, not design**.
⛔ `account_positions` + `virtual_positions` + `oms_managed_positions` are **ONE source**: blind,
derived-from-it, and our bookkeeping. **`fills` is the only independent ledger; the broker SCREEN
outranks all of them.**

---

## 🗓️ DATED
| when | what |
|---|---|
| **MON 08-24** | **SCHWAB RE-AUTH** — `refresh_token_expires_at = 2026-08-25T20:46:01Z` = **Tue 08-25 16:46 ET**, mid-session. ⛔ MANUAL, cannot ride the deploy. ⛔ **TWO FIELDS**: `expires_at` is the short-lived ACCESS token the refresher rotates itself — a ready-made false alarm. Read the store, never memory. |
| **MON 08-24 post-close** | Deploy, Actions page open. **Grade #739 FIRST** on today's numbers, then OMS (#758, #760, #755), then v2 (#761). |
| **MON 08-24** | #13 weekend-outage re-check (needs a 2nd weekend retained). |

## 📌 OPEN PRs — none deployed
`#755` Q12 audit-write · `#756` preflight fences (**held — only-change window**) · `#758` origin/reason
· `#759` Q5 pager · `#760` BROKER-SYNC-OK census · `#761` reclaim live-bar + slot_consumed
· `#762` §205 audit (**decide: merge as tooling or close — do not leave drifting**) · `#763` B28+B29.

## 🧠 RULES EARNED 2026-08-21
1. **⛔⭐⭐ A MUST-BE-ZERO SIGNAL CANNOT BE GRADED INTRADAY.** Mid-window is *not yet failed*, never
   *passed*. Signal 6 read 0 at 12:58 and 2 by the close. B28's UNMEASURED verdict is the same
   insight one step earlier.
2. **⛔⭐⭐ WHICH SIDE OF THE WIRE WRITES THIS FIELD?** `webull_broker_place_time` is written by OUR
   status poll — its absence is our blindness, not the venue's silence. **The name is not the
   provenance; grep the write site.** Three wrong conclusions off that one field.
3. **⛔⭐⭐ AN ABSENCE IS ONLY AN ABSENCE WITHIN THE POPULATION YOU QUERIED.** Name the population on
   the line. Three wrong absences in one day.
4. **⛔⭐⭐ TWO METRICS THAT SHOULD DIFFER AND NEVER DO ARE THE SAME NUMBER.** A greedy regex matched
   a sibling field for 4 hours. ⛔ Renaming for clarity? Check the new name is not a **substring**
   of a sibling.
5. **⛔⭐ FLAT NOW IS A SNAPSHOT, NOT A STATE.** Generalises to every pre-flight gate — re-confirm,
   never carry forward. (Proved itself: two working orders appeared 37 min after a clean reading.)
6. **⛔⭐ A WEEKDAY IS A COMPUTATION, NOT A LABEL.** Derive it. Both parties asserted one wrongly,
   in opposite directions, in a single exchange.
7. **⛔ THE ARITHMETIC WAS RIGHT; THE GROUPING HID THE EVENT.** A 26-day total was 96% one 3-hour
   storm. ⛔ And a hypothesis must be tested in the **unit the mechanism uses** — churn→429 looked
   refuted per-DAY and holds per-MINUTE.
8. **⛔ A FIX THAT LIVES IN ONE SCRIPT IS NOT A FIX** (B29). The mutation applied-check existed in
   one harness; the next one-off did not have it, so the fix had never been made.
9. **⛔ A GUARD THAT NAMES WHAT IT HUNTS MUST EXCLUDE ITSELF — build it in, don't remember it.**
   Five self-matches in one session; fixed structurally (scan only above the selftest label).
10. **⛔ THE EXIT STATUS MUST TRAVEL IN THE OUTPUT.** `$?` after a pipe is the pipe's. Written down
    twice, broken twice in one day — so it is a tool now, not a rule. It found a real defect
    (every successful `count` had been exiting 1) on its first run.

## 🧠 MEMORY POINTERS
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_reprotect_chain_uncovered_window]] (⛔ description SUPERSEDED — attach works) ·
[[project_mai_tai_db_seed_by_count_injects_stale_bars]] (⛔ #743 NOT proven) ·
[[project_mai_tai_false_flat_naked_position]] (the one-source chain) ·
[[feedback_authoritative_for_a_is_not_for_b]] (which side of the wire) ·
[[feedback_verify_before_concluding]] (must-be-zero) ·
[[feedback_an_absence_is_evidence_only_against_a_known_denominator]] (name the population)
