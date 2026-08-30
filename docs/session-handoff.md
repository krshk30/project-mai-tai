# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-08-30 16:20 ET.** Weekend close-out, batch `2026-08-30`. Integrator
for this rotation. Needs `codex-2`'s review before merge — the author never reviews.

---

# PRODUCTION — main and box IN SYNC

| | |
|---|---|
| main / box | **`0f35fadc7b4e38dde076a7eff0db3f7f97e07b14`** |
| checkout | clean |
| open PRs | **0** · undeployed commits **0** |
| ledgers | flat — `virtual_positions` qty>0 **0**, non-terminal open intents **0**, working orders **0** |
| last v2 bar | Fri 2026-08-28 **23:59:00** UTC (`23:59:30` is `polygon_30s`, fleet-wide — not v2's) |

**PIDs after the S0 rotation restart, all `NRestarts=0`:**

| service | pid | | service | pid |
|---|---|---|---|---|
| schwab-1m-v2 | 2203258 | | strategy | 2203071 |
| oms | 2203008 | | market-data | 2202865 |
| control | 2203323 | | market-capture | 2202817 |
| reconciler | 2202771 | | | |

---

# 🔴 MONDAY 06:00–06:45 ET — THE ONLY THING LEFT

**GO = an OBSERVED `restoration_complete=1` AND the explicit `[V2-BOOT-HOLD] released` INFO line.**
⛔ Absence of a hold line is **NOT** a release.

⛔⭐⭐ **GATES 2 AND 3 HAVE NEVER EXECUTED ONCE, ON ANY TAPE.** `rest_warmup_incomplete` reads **0
EVER** and `restoration_complete=1` reads **0 EVER** — both are **FALSE ZEROS: never reached, not
never-failed.** The `[V2-BOOT-HOLD] released` lines that do exist are dated 08-28 in the **old**
message format, i.e. the pre-#827 mechanism. **Monday 04:00–07:00 is their first execution in the
system's history.**

**How the hold releases:** scanner population non-empty **AND** `confirmed == evaluated` (DB seed)
**AND** `rest_warmed == evaluated`. A symbol enters `_rest_warmup_done` only on a REST bar whose
**age** is ≤300s (`REST_WARMUP_FRESH_THRESHOLD_SECS`).
⛔⭐⭐ **There is NO elapsed-time timeout — one halted or thin symbol holds the fleet indefinitely.**

**Report these as SEPARATE lines. A combined verdict destroys attribution:**

| change | expected Monday reading |
|---|---|
| **#843** scope | fan-out-only exclusions — ⛔ **ZERO IS THE EXPECTED VALUE** |
| **#848** slot consumption | `[V2-FANOUT-SLOT-CONSUMED]` attempted/suppressed |
| **#849** provenance | `refusal_origin` + code present on any refused intent |

⛔ **#843's `evaluated`-vs-Friday test was WITHDRAWN.** Measured across 08-24→08-28: only **4**
probes found any live open intent at all, and fan-out-only symbols removed were **0, 0, 1, 0**. The
instantaneous population is 0—occasionally 1, following from the **0.105s** median non-terminal
window. **Unchanged `evaluated` is the expected reading**, not a defect. #843's correctness rests on
an executed mutation, not a live observation.

**Mitigation, prepared and unexecuted:** setting
`MAI_TAI_STRATEGY_SCHWAB_1M_V2_CW_ARMED_SEGMENT_SAFETY_ENABLED=false` puts v2 in shipped
compatibility mode (`bot.py:1735`). ⭐ The pre-#827 mechanism **did** release repeatedly on 08-28, so
this is a **demonstrated** path, not a theoretical one. **Operator-authorized only.**

---

# ⭐⭐ THE WEEKEND'S MAIN RESULT — A CHAIN WITH FOUR LINKS, THREE BROKEN

| link | was | fixed |
|---|---|---|
| script correct | returned `UNKNOWN` on **every** live call (`tr` given a third operand) | #841 |
| invoked at all | **nothing scheduled it** — no timer, no unit, no crontab for any user | #842 |
| window reaches the event | cron widened to 06:00 ET but the wrapper's own guard refused before 07:00 — **inert** | #844 |
| delivered to a human | never tested | **operator confirmed on his phone** |

⛔ **Each link looked fine from the one above it.** The v2 source comment said
`"armed_segments_check will page"` — false for months.
⛔ **`SELFTEST push sent` proves NOTHING** — written unconditionally on the line after the call.
⇒ **For any alerting path, demand evidence PER LINK.**

**Now armed:** `*/5 10-21 * * 1-5` (box TZ `Etc/UTC`), wrapper guard `360 ≤ ETMIN < 990` =
**06:00–16:30 ET**, weekdays. Wrapper mode `100755` **in the git index**, not a hand-chmod.

---

# ✅ CLOSED THIS WEEKEND

| item | outcome |
|---|---|
| **S0 credential rotation** | ✅ Done. Env rewritten 19:52:33Z, 9 live connections on the new secret, 7 services restarted `NRestarts=0`, rollback copy shredded. **The credential exposed to a transcript is dead.** 36 `.bak` env files hold the now-dead old one — awaiting operator's call. |
| **Schwab re-auth** | ✅ Read back from the store: `refresh_token_expires_at` = **Sun 2026-09-06 15:44 ET**, 7.00 days. Weekday **derived**, not labelled. ⛔ `expires_at` is the short-lived access token the refresher rotates itself — a ready-made false alarm. |
| **D3 / #843** | Cross-venue scope. The union's two halves had **different scopes**; the source comment at `:1254` warned of the exact hazard the code already had, and the docstring claimed a scope the query did not implement. |
| **D20 / #848** | 11 filled buys across 5 slots on 08-27 = **6 excess fills**, each a distinct `client_order_id` — new orders on an already-filled slot. Fixed at slot consumption, scoped **per segment** (#644's cap), not per position. |
| **W2 / #849** | Provenance reached the logs and fan-out outcome but **not `trade_intents.status`** — the one surface counts derive from. Now records `refusal_origin`/`refusal_code`. |
| **Marker census / #847** | Live, with a stored Friday baseline (`a62ac547`). Collapsed 11 board rows. |
| **Item 1 · T23** | Answered · already shipped in #817. Both were **stale rows, not open work**. |

---

# OPEN ITEMS

| item | owner | state |
|---|---|---|
| **Monday watch** | codex-2 | armed, report-only |
| **W2's 3 chain errors** | codex-2 | ⛔ **codex's number, never checked by claude-1.** Not adopted. |
| 36 `.bak` env files | operator | hold a dead credential; delete or keep as config history |
| `board.sh` "open BLOCKED" | — | **FINDING, not a task** — see below |

---

# ⛔ FINDINGS (no owner, no next action — do not put these on the board)

**1. `board.sh` reports "open BLOCKED" with no concept of closure.** Line 74 greps every
` | BLOCKED | ` line ever written and prints the last 10 under that heading. A resolved BLOCKED sits
there forever. It misled `claude-1` into contradicting a correct report from `codex-2` during this
very close-out. **Same class as everything else this weekend: a label asserting more than its
mechanism supports.**

**2. `assert_fleet_flat.sh` does not source the env file** — it expects `MAI_TAI_DATABASE_URL` in the
environment and **fails closed** without it. Correct behaviour; worth knowing before invoking it
from a bare shell.

---

# OPERATIONAL RULES CONFIRMED THIS WEEKEND

1. ⭐⭐ **Ask "who invokes this?" BEFORE "is this correct?"** Eleven review rounds hardened a pager
   nothing called.
2. ⛔ **A no-op success is the dangerous kind.** A refused commit followed by `git push` printed
   "PUSHED" — pushing an unchanged ref is a valid no-op. **Verify the EFFECT.**
3. ⛔ **For any consume/release pair, mutate BOTH halves.** The guard half is obvious; the release
   half is where the silent cost lives (a broken release = one entry per symbol per segment forever).
4. ⛔ **A guard tested only on its KNOWN inputs is untested where it earns its keep.** #849's unknown
   -origin fallback survived four mutants because every one traversed a known-origin path.
5. ⛔ **A watch measuring code you intend to replace has no decision value.** Two deploy holds were
   lifted on this basis.
6. ⛔ **A Linux-targeted shell installer cannot be graded on Windows.** A local failure was a
   path-form artifact — **VOID, not negative**.
7. ⛔ **Review-pin records are WRITE-ONCE.** `review_pin_gate.py:165` requires exactly one commit per
   record path. "Unmerged" does not mean "mutable."
8. ⛔ **Check the verb.** `grep -c "env|VAR"` counts *references*, not *sourcing*; imports ≠ executes.
9. ⛔ **A wrong reason that stops being examined propagates.** A quoting diagnosis, disproven the same
   hour by the real cause, survived unretracted into an operational runbook.

## Memory pointers

`[[project-mai-tai-fleet-roster]]` · `[[project-mai-tai-architecture]]` ·
`[[feedback_a_watch_that_fails_to_a_false_clean]]` ·
`[[project_mai_tai_reconciler_detects_nobody_listens]]` ·
`[[feedback_the_tools_status_is_not_the_things_status]]` ·
`[[project_mai_tai_review_pin_gate_mechanics]]` · `[[feedback_a_wrong_reason_is_worse_than_a_missing_one]]`
