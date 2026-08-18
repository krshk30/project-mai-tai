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

# 🚨 TOMORROW STARTS WITH THE DETECTOR, BEFORE 07:00. NOTHING ELSE MOVES UNTIL THE CENSUS WINDOW.

**#721 deployed 16:31 ET 08-18 (`1a26f430`). Its census read `truncations=0 of 5` tonight — that is
MECHANICAL, NOT EVIDENCE.** CAST stopped being exposed because it grew past 250 bars during the
session, not because of the fix.

```bash
/tmp/seed_exposure_detector.sh          # on the box; read-only, no restart
```
Run it **pre-open AND at every watchlist add** — a symbol joining mid-session has almost no bars at
that moment, which is the CAST case exactly. Then watch `[V2-DB-SEED-GAP-CENSUS]` through
**04:00–11:00 ET**.

⛔ **THE PROOF IS `truncations > 0` ON A THIN SYMBOL WITH AN OLD SERIES BEHIND IT.** A second quiet
morning is a **NON-RESULT** — say so; do not bank it. The criterion is **bar count vs the 250 seed
limit, not clock time**: a thin name may never reach 250 in a session, and thin names are the whole
universe. Exposed = *thin today* **AND** *has an old series*. A history that STARTS today (AIXC 74,
NTWOW 160) is **SHORT, NOT HOLED** — the regression direction, and it must keep seeding.

---

## ⚡ FIRST SCREEN

**2026-08-18 EOD.** Fleet **7/7 running**. **Account FLAT** — broker book empty, 0 managed rows,
0 working orders. Tree clean, **no open PRs, nothing in flight**.

**Box HEAD `1a26f430`, `src` diff vs `origin/main` = 0.** v2 restarted **16:31:39 ET** (files written
16:31:26 — process after files); outage **1 second** per systemd's own record ⇒ **no bar hole**.
OMS still carries `70ca930`, untouched since 08-17 17:50 ET.
Verified BY CONTENT: `DB_SEED_MAX_MISSED_SESSIONS` ×3 · `_missed_sessions_between` ×2 ·
`[V2-DB-SEED-GAP-CENSUS]` ×1 · `[V2-BOOT-HOLD] released — 0 reconstructed-uncapped segments`.

---

# 📋 THE QUEUE — BY EXECUTION, NOT BY TIER (operator-set 08-18)

## 🗓️ DATED — a trigger nobody wrote down never fires
| when | what |
|---|---|
| **Wed 08-19, 04:00–11:00 ET** | **#721 acceptance.** Detector + census. `truncations>0` on a holed thin symbol is the only proof. |
| **Mon 08-24** | **#13** — the weekend-outage re-check, answerable only once a SECOND weekend is in the retained logs. |
| **Mon 08-25, before 16:46 ET** | **SCHWAB RE-AUTH** at `https://project-mai-tai.live/auth/schwab/start`. `refresh_token_expires_at = 2026-08-25T20:46:01Z`, read from the STORE 08-18 16:48 ET (GREEN ~7.0d). ⛔ **MANUAL ONLY.** Miss it and **Tue 08-26 pre-market opens with no token**. |

## 🚀 DEPLOYS, IN ORDER
**§81.4 → §3 → #16 → §81.1+2 → #7 → §54/#12**

1. **§81.4 — THE RESTART FENCE. ✅ CONFIRMED WINDOW-FREE, SHIPS IMMEDIATELY.**
   `deploy_preflight` is imported by **no service code**, and `/home/trader/ops_preflight/
   preflight_v2_restart.sh` is standalone blocking tooling ⇒ **no deploy window needed.**
   ⛔ It is a **SIBLING, not an edit**: the existing script gates **v2** restarts; this fence is for an
   **OMS** restart while a pre-market position is open, because the software ladder is **in-process in
   the OMS**. Today that fence was **a human remembering, for 26 minutes**.
   ⛔ Commit the exec bit; a hand-chmod on the box blocks every deploy.
2. **§3 — THE `source` COLUMN (abort vs refusal). IT IS THE INSTRUMENT, SO IT GOES FIRST.**
   #16's acceptance IS a reject count, and the conflation is what makes reject counts unreadable —
   fixing the leg before the gauge means measuring the fix through the lens that hid it. **Changes
   zero order flow**, so it is the safest thing in the first window. Measured cost: three sessions.
3. **#16 — THE DEAD WEBULL MIRROR LEG. THURSDAY, NOT FRIDAY.** It is the only queued change that
   **adds live orders**, so it must be watched the next morning; a Friday deploy means three days
   before anyone sees it work. Build with the full mutation round, deploy after the close.
4. **§81.1+2** — stop the pre-market combo attach · **one COUNTED line per unprotected fill**.
   ⛔ Trap at the top of that PR: after part 1 the refusals stop **because we stopped asking**.
   Part 2's count is the only honest signal.
5. **#7** retry bound `_v2_exit_close_failures` unreachable · 6. **§54/#12** sustained-unreadability
   pager (trip on N consecutive failures AND holding; 273 reads ≈68 min vs 6 ≈1 min sizes it).

## 🔀 PARALLEL — no deploy, no restart
**R4 wiring → R1-as-fidelity · §121 · §113 · §124 · §99 · the segment-identity audit**
⛔ **R1 is the FIRST trade-level parity the engine has ever had** — the 89/90 is *config* parity and
only STKH has ever matched. The divergence report must classify **(a) engine defect / (b) production
defect / (c) genuine tolerance from run one**; retrofitting after a run that called everything an
engine miss is worse than useless. Signature of (b): *an arm whose bar predates the loaded window*.
Replay loads by TIME WINDOW and already caps such arms, so **it will correctly refuse entries
production actually took — those are NOT misses.** R1-as-evaluation waits on the seed work.
Golden set: 08-17, 20 broker-confirmed trades, +$2.26 gross (IPST +2.52, IVF −0.26, WFF +0.10, SLE −0.10).

## ⬇️ BEHIND
#4 · #11 · **§63 stays LAST — the refusals are still a detector** · group B (BOXL/GXAI/RMCF, 1–2¢) ·
SCKT (stop above ask on our feed, refused anyway, n=1) · #8 · #9 · #6 · §82 (fan-out once-per-flip
latch) · retire the Wednesday cron + fix the duplicated crontab lines · Redis MAXLEN **once
`required_cycles` pins the floor**.

## 🧪 RESIDUALS
- The **4d–7d REST band #721 cannot see** (73 gaps, 59 symbols) — they arrive via REST, not the DB.
  ⭐ Design note: **the gap check belongs downstream of BOTH feeds**, not inside the DB seed.
- `_cap_reconstructed_segment` is **decoration** — marked NOT LOAD-BEARING in code with both
  failures named (50 ms early on REST-warmup #619; never ran at all for CAST).
- The **1.80 G Redis peak is still unexplained** against a 1.11 G structurally-constant steady state.
- `source='live'` is a **PROVENANCE** guarantee, never a **FRESHNESS** one.

## ⛔ FLAGS ON EVERYTHING
1. **EVERY Schwab-vs-Webull comparison spanning 08-14 → now is VOID.** The Webull leg was absent.
2. **STKH CANNOT BE R1's REFERENCE** — it sits in the >4d gap population (60.1 d, 2 wide gaps) and it
   is the only symbol that has ever matched.

---

## ⭐⭐ THE DAY'S ROOT CAUSE — one defect that ate five board sections
`_seed_strategy_bars_from_db` hydrated **250 bars BY ROW COUNT**, so on a thin name it reached back as
far as the rows did. CAST 08-18: 38 bars that day, a **61-day hole**, then June ⇒ armed at
**flip_level 7.99 while CAST traded 1.04–1.28**. Five arms through five June bars in **26 ms**.

**Fixed by the right variable — MISSED TRADING SESSIONS, not wall-clock.** Over 256k gaps:

| | gaps | median price move |
|---|---|---|
| same session | 255,243 | **0.7%** |
| **0 missed — a CLOSURE** | **345** | **10.2%** ← legitimate, seed across it |
| **1 missed** | 75 | **26.2%** ← 2.6× jump |
| 2–10 missed | ~190 | 16–32%, flat |

⛔ Two candidate variables **died to that histogram**: a 4-day threshold let 110 gaps through at 18%
median, and a PRICE cut truncates every Monday (weekend gaps genuinely run 10–18%). Only *missed
sessions* separates a **CLOSURE** from an **ABSENCE**. Calendar derived from the data ⇒ holidays
cannot drift; a failed calendar read returns **0**, so a DB blip never silently truncates history.

⛔⭐⭐ **THE SCHWAB STOP-PRICE REJECTS WERE A PROTECTIVE ACCIDENT, NOT A GUARD.** 33 of 454 entries
carried a prior-session arm bar; **32 refused because the level was absurd, and one (BQ 08-12, arm bar
06-11) FILLED for +1.75%** — indistinguishable from seven clean BQ trades the same day. **That is why
this is P0 on MECHANISM, not damage**, and it answers anyone pointing at a winning trade as evidence
the data was fine.
⛔ **CAST is the symptom.** The fingerprint is on **eight** symbols — CRWU, SCKT, INHD, AEHL, STFS, BQ,
WCT, CAST. Removing one destroys evidence and changes nothing.

## 🔴 #16 IN ONE PARAGRAPH (the other thing found today)
The Webull `rth_resting_mirror` leg has been **100% dead since 08-14** — it sends a `STOP_LIMIT`
master and **our own code aborts it client-side**: `RuntimeError('Webull combo MASTER must be LIMIT or
MARKET ...; got STOP_LIMIT')`. **542 attempts, 1 fill** (08-14 175/1 · 08-17 202/0 · 08-18 165/0);
other legs healthy. ⛔ Invisible because a RuntimeError from OUR code is stored as *"Webull order
rejected"*. It explains **both** fills/day halving (61–68 → 20–28) **and** `ATTACHED=0`. Acceptance
*with its denominator*: fills/day back toward 61–68, and the RuntimeError count to **zero**.

## 📌 STANDING RULES EARNED 08-17/08-18
1. **⭐⭐ "UNEXERCISED" DOES NOT MEAN "WAIT."** If the untested path is reachable by injecting a fault
   into the **real object**, inject it — #714 was proven in minutes. Stub the persistence, never the
   object under test.
2. **⛔⭐⭐ COUNT WITH THE PREDICATE THE CONSUMER USES.** Family of four, the last being bar counts
   summed across strategy codes (591) when the seed reads only its own (215).
3. **⛔⭐⭐ A QUERY AGAINST A WRONG NAME RETURNS A CONFIDENT WRONG ANSWER, NOT AN ERROR.** Four in 24 h;
   one became §49, a three-part plan to build a log line that already existed. **Enumerate, then
   filter. For log markers, grep the marker out of the SOURCE.**
4. **⛔⭐⭐ READ THE CONTENT, NEVER THE STATUS.** 8+ in 48 h. Every filtered query echoes its predicate
   **and** the unfiltered count; every write is read back.
5. **⛔⭐⭐ A TEST MAY NOT REIMPLEMENT WHAT IT TESTS.** Twice in one evening on two functions; one
   escape would have truncated **every weekend**. Mutants were the only thing that caught it.
6. **⛔ THE CLOCK AND THE CODE COME FROM THE BOX.** My elapsed-time sense and my memory of the entry
   window (7–18 ET; the code says **7–16**) were both wrong today.

## 🧠 MEMORY POINTERS
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_db_seed_by_count_injects_stale_bars]] · [[project_mai_tai_restart_bar_gap_checklist]] ·
[[project_mai_tai_webull_core_session_root_cause]] · [[project_mai_tai_broker_order_events_conflates_client_aborts]] ·
[[feedback_a_bare_where_clause_lies]] · [[feedback_the_tools_status_is_not_the_things_status]] ·
[[feedback_unexercised_is_not_a_result]] · [[feedback_check_which_parts_already_work]]
