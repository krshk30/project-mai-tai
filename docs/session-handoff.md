# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-08-29 16:35 ET.** Weekend session, market closed. Needs `codex-2`'s
review before merge — the author never reviews.

---

# PRODUCTION — main and box IN SYNC

| | |
|---|---|
| main / box | **`8715a0996b6c9ad32bc8e2b6e6d3d2050a920b0a`** |
| checkout | clean |
| open PRs | **0** |
| ledgers | flat — `virtual_positions` qty>0 **0**, non-terminal open intents **0**, working broker orders **0** |
| last bar | Fri 2026-08-28 23:59:30 UTC (19:59:30 ET). No stream since. |

| service | pid | moved today? |
|---|---|---|
| schwab-1m-v2 | 2096551 | ✅ twice — 17:38Z (#827), 20:27Z (#843) |
| oms | 1982227 | — |
| market-data | 1974639 | — |
| reconciler | 1811867 | — |
| control | 1974293 | — |

`NRestarts=0` on every unit.

---

# ⭐⭐ THE PAGER CHAIN — FOUR LINKS, THREE WERE BROKEN, NOW PROVEN END TO END

This is the weekend's main result and the most transferable lesson.

| link | was | fixed by |
|---|---|---|
| script correct | returned `UNKNOWN` on **every** live call (`tr` given a third operand) | **#841** |
| invoked at all | **nothing scheduled it.** No timer, no unit, no crontab for any user, no workflow | **#842** |
| window reaches the event | cron widened to 06:00 ET but the wrapper's own guard refused anything before 07:00 — **inert** | **#844** |
| **delivered to a human** | never tested | **operator confirmed on his phone** |

⛔ **Each link looked fine from the one above it.** The v2 source comment said
`"armed_segments_check will page"` — false for months.

⛔ **`SELFTEST push sent` in `cron.log` proves NOTHING.** It is written unconditionally on the line
after the call. Only a recipient can close a delivery question.

⇒ **For any alerting path, demand evidence PER LINK**: code correct · invoked · window covers the
event · received by a human.

**Now armed:** `*/5 10-21 * * 1-5` (box TZ is `Etc/UTC`), wrapper guard `360 <= ETMIN < 990` =
**06:00–16:30 ET**, weekdays. Wrapper is `100755` **in the git index** — not a hand-chmod, so it
will not block a future deploy.

---

# 🔴 MONDAY 06:00–06:45 ET — THE CRITICAL PATH

v2 has been held since boot with `restoration_complete=0`,
`reason=empty_evaluated_population_after_exclusions`. **That is CORRECT** — a weekend has no bar
population, and the guard refuses to read an empty set as restored.

**How the hold releases** (`_try_complete_boot_state_restoration`): requires scanner population
non-empty **AND** `confirmed == evaluated` (DB seed) **AND** `rest_warmed == evaluated`. A symbol
enters `_rest_warmup_done` only on a REST bar whose **age** is `<= 300s`
(`REST_WARMUP_FRESH_THRESHOLD_SECS`).

⛔⭐⭐ **THERE IS NO ELAPSED-TIME TIMEOUT. One halted or thin symbol holds the entire fleet
indefinitely.** That is the real risk — not the 300s bound.

⛔ **A prior claim in the #827 pin is WRONG and cannot be amended (pins are immutable):** it argued
300s was violated because `[V2-REST-WARMED]` lines were 14–18 minutes apart. **Those are different
quantities** — 300s bounds the accepted bar's *age*, not the interval between warmups. `codex-2`
caught it.

**GO = an OBSERVED `restoration_complete=1`.** ⛔ Absence of a hold line is **NOT** a release; the
release is its own INFO line and must be seen.

⭐ **Also report `evaluated` and compare to Friday's.** #843 should make it **smaller** by exactly
the fan-out-only symbols. If unchanged, #843 is not reaching the path — and that must be known
before the open.

**Mitigation, prepared and unexecuted:** setting
`MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_ARMED_SEGMENT_SAFETY_ENABLED=false` puts v2 in shipped
compatibility mode (`bot.py:1735` — no boot latch, no hold). It costs the armed-segment safety
feature, so it is **operator-authorized only**.

---

# ✅ D3 IS LIVE — AND IT WAS NEVER A STRATEGY DECISION

`_fetch_position_maps` unioned two halves with **different scopes**: `virtual_positions` was
broker-scoped, in-flight `TradeIntent` was **not**. So a fan-out leg's working order made v2 believe
it was in position on Schwab.

⭐ **Three written sources already required the fix**, none cited when it was framed as a policy
call: operator reading A; the 08-26 fleet note *"duplicate-exposure alarms must be scoped PER
VENUE"*; and **the source itself at `:1254`**, warning that widening this to a second broker account
"would make v2 believe it is in position on Schwab when only the Webull fan-out leg is open" — a
hazard the code already had.

⛔ **The old docstring claimed a scope the query did not implement.** That false contract is the
most likely reason it survived review.

**Magnitude (14d):** 1,583 fan-out intents over 60 symbol-days polluted the entry union — *more than
the strategy's own 1,438*. Non-terminal window **median 0.105s**, max 3,332s.
⛔ **This is REACHABILITY, not harm.** The suppressed-entry count is **COULD_NOT_TELL**: a
suppressed entry writes no `trade_intent` row, and the gates are bare returns with no marker.
⛔ **Booked under `live:orb`, NOT `live:webull_30s`** — a `webull`-keyed query returns 0 and reads as
"no problem."

---

# 📌 ITEM 1 — THE 19 DUPLICATES, PRESERVED HERE BECAUSE IT LIVED ONLY IN CHAT FOR 4 DAYS

**Established:** the 7 duplicate symbol-days **net exactly 0** — every sell is held, **no naked
positions**.

**The original 19 (08-21 → 08-26) are permanently `COULD_NOT_TELL`.** `fanout_slot_id` coverage in
that window is **0 of 1,838 buy orders**. #812's key first populates **08-27, not 08-26**, covers
**~50%**, and **does not backfill**. ⛔ Do not revisit them; no query will change this.

**08-27 → 08-28, keyed population only, distinct filled BUY orders (not fill rows):**

| classification | count |
|---|---|
| intended cross-broker fan-out pair | 0 |
| **genuine duplicate** | **2** |
| could not tell | 2 |
| **total** | **4** |

⭐ **MIMI and PPCB are the first duplicates ever PROVEN rather than inferred** — same-broker,
same-slot repeats. Opened as **D20**, owner `codex-2`, **not started**. Separate from D3 and **not**
fixed by #843.

⛔ **Unit warning:** buy *fills* are the wrong denominator. DAIC 08-25 shows 39 buy fills — partial
fills of one entry. At order level, symbols with >1 buy order run 5–12/day, not 19.

---

# D6 — RAN, ARMED, AND ITS BASELINE IS UNTRUSTWORTHY

Manual run recorded: `session=2026-08-28 verdict=FAIL denominators=present`.
Scheduler armed `17 4,5,6 * * 2-6` = **Tue–Sat**.
⛔ **MONDAY PRODUCES NO D6 READING** — Monday's session reports Tuesday early AM. Absence is not
failure.

⛔ **Friday's FAIL is FAIL-BY-CONSTRUCTION on n=1, not a fill-rate finding:**
`mirror keyed 0/19` at a 7.1% base rate has **P(zero) = 24.7%**; `schwab keyed 0/6` at 9.2% has
**56.0%**. Paired legs `0/1` is a single event. **A quarter of healthy Fridays produce 0/19.**

⛔ The historic `22/22 median 4.58%` comparator came from a **zero-key-coverage** population. It is
not an identity-keyed baseline. Every D6 line must carry its key-population rate inline.

---

# OPEN ITEMS

| item | owner | state |
|---|---|---|
| **Monday 06:00–06:45 watch** | codex-2 | scheduled — report only |
| **D20** — MIMI/PPCB confirmed duplicates | codex-2 | **not started** |
| **Item 1 remainder** — 08-27+ only, ~50% coverage | codex-2 | **not started** |
| **S0 — rotate the exposed DB credential** | **operator** | **not started, ~5 days.** The URL was rendered into a task transcript. Nothing is recorded against it. |
| **Schwab re-auth** | **operator** | `refresh_token_expires_at` = **Mon 2026-08-31 16:02 ET**, *inside* Monday's session. ⛔ Read the store back afterward; do not trust the flow's success message. |

---

# ⛔⭐⭐ REVIEW-PIN GATE — RECORDS ARE WRITE-ONCE

`review_pin_gate.py:165` requires **exactly one commit per record path**, and `record` refuses an
existing destination. **There is no amend or supersede path.**

I amended a record in place after new information arrived and the gate refused
`must be immutable; found 2 commits touching it`. **"Unmerged" does not mean "mutable" —
immutability starts when the record is WRITTEN.** Recovery is a content-identical new head plus a
fresh record, with the invalid one dropped in the same commit. ⛔ Not a force-push; that rewrites a
shared audit trail.

---

# OPERATIONAL RULES CONFIRMED THIS WEEKEND

1. ⛔⭐⭐ **A no-op success is the dangerous kind.** A refused commit followed by `git push` printed
   "PUSHED" — pushing an unchanged ref is a valid no-op. Second false positive this week, and **not
   PowerShell**, which kills the "environment quirk" reading. **Verify the EFFECT.**
2. ⛔ **A Linux-targeted shell installer cannot be graded on Windows.** A local run showed
   `1 failed`; the failure was Git Bash path-form mangling. That run was **VOID, not negative** —
   and I nearly reported it as contradicting codex.
3. ⭐ **Ask "who invokes this?" BEFORE "is this correct?"** Eleven rounds hardened a pager nothing
   called.
4. ⛔ **A watch measuring code you intend to replace has no decision value.** The #843 hold was
   dropped on that basis: preserving a pre-fix observation that feeds no decision is sunk effort.
5. ⛔ **Two of my stated reasons collapsed under operator questioning in one session** — the #842
   schedule (inert) and the #843 hold. **Both times I asserted a downstream effect without reading
   the thing downstream.** The measurements held; the reasoning did not.

## Memory pointers

`[[project-mai-tai-fleet-roster]]` · `[[project-mai-tai-architecture]]` ·
`[[feedback_a_watch_that_fails_to_a_false_clean]]` ·
`[[project_mai_tai_reconciler_detects_nobody_listens]]` ·
`[[feedback_the_tools_status_is_not_the_things_status]]` ·
`[[project_mai_tai_review_pin_gate_mechanics]]` · `[[feedback_query_unit_must_match_hypothesis_unit]]`
