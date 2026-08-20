# EOD handoff SKELETON — 2026-08-20

**Pre-written before the 16:00 window.** Everything already known is filled in; everything the
window produces has a **`⟦FILL⟧`** slot with the exact question it answers. Assembling this after a
16:00 deploy is how details get compressed — the slots exist so tonight's result drops straight in
rather than being summarised from memory at 21:00.

⛔ **This file is scaffolding. At EOD its contents are OVERWRITTEN into `docs/session-handoff.md`
(state) and APPENDED to `docs/handoff-log.md` (narrative), and this file is deleted.** Two verbs,
never merged — see [[feedback_session_doc_and_memory_discipline]].

---

## ⚡ FIRST SCREEN → `session-handoff.md`

**2026-08-20 EOD.** Fleet ⟦FILL: n/7⟧. Account ⟦FILL: flat? open managed rows?⟧.
**Box HEAD `⟦FILL⟧`** (was `f18132e7`), `src` diff vs origin/main = ⟦FILL⟧.

⛔ **`src diff = 0` IS NOT EVIDENCE** — the per-service table below is.

| service | file write (UTC) | process start (UTC) | running pulled code? |
|---|---|---|---|
| **oms** | ⟦FILL⟧ | ⟦FILL⟧ | ⟦FILL — expect YES⟧ |
| **schwab-1m-v2** | ⟦FILL⟧ | ⟦FILL⟧ | ⟦FILL — expect YES⟧ |
| strategy | ⟦FILL⟧ | ⟦FILL⟧ | ⛔ expect NO |
| market-data · control · reconciler · market-capture | ⟦FILL⟧ | ⟦FILL⟧ | ⛔ expect NO |

**The two opt-ins, verified FROM THE SINK:**
- flag out of `/proc/<pid>/environ` → ⟦FILL: true?⟧
- `event_source` in `information_schema.columns` → ⟦FILL: 1 row?⟧

⛔ Setting a switch and confirming a switch are different facts. If either slot is empty, say
UNVERIFIED — never infer one from the command that set it.

---

## 🗓️ DATED — carried forward + new

| when | what |
|---|---|
| **FRI 08-21 am** | **Acceptance, graded against the pre-written sheet** (`docs/deploy-2026-08-20-window.md` §3), not against a clean-looking log. ⛔ A quiet Friday is a **NON-RESULT**. |
| **MON 08-24** | **#13** weekend-outage re-check — needs a 2nd weekend in the retained logs. |
| **MON 08-25, before 16:46 ET** | **SCHWAB RE-AUTH**, `https://project-mai-tai.live/auth/schwab/start`. ⛔ **MANUAL ONLY.** ⛔ **Read the expiry FROM THE STORE on the day — never quote it from memory or from this line.** |
| **AFTER Q1 IS DEPLOYED *AND PROVEN*** | **§178** — revisit B9 cause 2's release. Ruled STRICT now. ⛔ proven ≠ deployed ≠ merged. |
| **AFTER #739 IS MERGED, DEPLOYED *AND MEASURED*** | **B9 cause 3 build.** See the correction below — this is NOT gated on tonight. |

---

## ⛔⭐⭐ THE CORRECTION THAT MUST SURVIVE THE NIGHT

**#739 (§82 cause 1) is `OPEN` — NEVER MERGED.** Verified on the branch, not from the PR list
impression: `gh pr view 739 → state=OPEN, mergedAt=NEVER`, and no commit for it on `origin/main`.

⇒ **Tonight produces NO §82 residual.** Tonight's v2 payload is the flag + #743 + B19/B20; cause 1
is not in it. I had written that tonight's run produces the residual cause 3 is measured against —
**right answer (don't build cause 3 today), wrong reason.** The real chain is:

> **#739 merged → deployed → measured → THEN cause 3.**

It is not merely un-deployed; it is **un-merged**, so it is not scheduled at all.
⛔ A wrong reason is worse than a missing one: this one would have had the next session waiting on
tonight for a number tonight cannot produce.

**Why cause 3 was still not built today** (the reasons that hold):
1. It does not land sooner either way.
2. Holding a "built but deliberately unmerged" PR would weaken the rule earned today. **Merging is
   scheduling only works while it has no exceptions**, and *"remember not to merge this one"* is
   precisely the special case that gets forgotten.

---

## 🔬 §183 — THE MIGRATION DOES NOT FAIL LOUDLY ⟦keep regardless of tonight's outcome⟧

Checked at the **catch site**, not the raise. All six `append_order_event` paths are wrapped by an
`except Exception` that logs and continues; **none re-raise**. The OMS does not crash.

⛔ **And it is not observability — it drops FILLS.** `append_order_event` runs BEFORE
`record_fill_if_needed` (7276) and before `apply_fill_to_positions` (4671), so the raise costs the
fill and the position update, per order, behind a WARNING.

⛔⭐ **The first swallowing path is `_mirror_v2_fill_to_webull`** — the instrument for tonight's own
signals 1–3. A missed toggle would take out the measuring device for the thing it shipped beside.

⟦FILL: did the column check pass before the restart? Any `[OMS-V2-MIRROR] … failed` lines?⟧

---

## 📋 ACCEPTANCE — Friday, per the sheet ⟦FILL each⟧

| # | signal | expected | ⟦result⟧ |
|---|---|---|---|
| 1 | mirror STOP_LIMIT rejects | → 0 | ⟦FILL⟧ |
| 2 | orb entry fills/day (Schwab's rate beside it) | 12–25 | ⟦FILL⟧ |
| 3 | `[WEBULL-BARE-FILL]` / session | ~9 ⛔ stop if >20 | ⟦FILL⟧ |
| 4 | duplicate legs per segment | ⛔ stop above 19-of-119 | ⟦FILL⟧ |
| 5 | census denominator | readable | ⟦FILL⟧ |
| 6 | seed-gap fail-open | → 0 | ⟦FILL⟧ |
| — | **#736 — success is SILENCE** | **zero** `[OCO-TARGET-BELOW-FILL]` | ⟦FILL — a line appearing IS the finding⟧ |
| — | **B19/B20** | `[V2-ENTRY-WINDOW-ARM-RELEASE]` at 16:00; `[V2-CW-DISARM] reason=watchlist-removed` | ⟦FILL⟧ |

⛔ **Attribution is pre-assigned** (sheet §1c) so no result gets claimed by the wrong change. B20
cannot move tomorrow's entries (fires only after the window closes); **B19 can** — a re-joining
symbol now arrives disarmed. If entry counts move, B19 is the candidate, not the flag.

⛔ Reject counts stay **contaminated for tonight's grading**: `event_source` populates from the
deploy forward and the "720 since 08-14" baseline is entirely pre-column.

---

## 📌 OPEN / BOARD — carried

- **§180** — the fan-out **slot accounting** rests on a wrong comment and is therefore wrong too.
  `_fetch_position_maps` is Schwab-scoped; the fan-out leg fills on `live:orb`. Own item.
- **The CAST seed-cap miss** — still unexplained; the guards read the **state** field.
- **§82 has THREE causes.** #739 fixes the reactive latch (14 of 19) — **and is still unmerged**.
- **Reboot backlog** — 8 kernels + `libc6`, ⟦FILL: uptime days⟧.
- **P2 replay rebuild** — needs redoing: **P21 changed what the replay reports.**
- **The unified gap check** downstream of both feeds — Q11 came back **6 of 43** ⇒ not urgent.

---

## 🧠 RULES EARNED TODAY → `handoff-log.md` + memory

1. **§179 MERGING IS SCHEDULING.** Merged ⇒ ships on the next deploy of whatever service it touches.
   B19/B20 and Q1 moved onto tonight by being merged. **The rule only works with no exceptions.**
2. **§181a** — a test covering the HELPER but not the WIRING cannot see a dead call site (B19 M6).
3. **§181b** — a stub that already satisfies the fallback never exercises it (Q1 M2).
4. **§183** — verify a failure mode at the **catch site**, never at the raise.
5. **§180** — a wrong COMMENT is a wrong reason, and code rests on it.

⟦FILL: anything the window itself teaches⟧
