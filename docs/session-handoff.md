# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-01 19:30 ET.** Batch `2026-09-01-batch-day`. Integrator for this
rotation. Needs `codex-2`'s review before merge — the author never reviews.

---

# PRODUCTION — main and box IN SYNC AT THE CODE LEVEL

| | |
|---|---|
| box (deployed) | **`e24913b85db7c5234038f0453019a7c0f27a37df`** — verified ON THE BOX 19:29 ET, checkout clean |
| main | `e24913b` — identical. **Two deploy windows today**: 16:26/16:29 ET (the 11-PR batch onto `1f5da81`) and ~17:45 ET (#870+#871, v2-only, operator-ordered no-hold) |
| open PRs | **1** — #865 (D20 edge-control re-derivation, docs-only, pinned by claude-1 at `def705cc`, undeployed-by-nature) |
| exposure (19:29 ET) | managed **0** · virtual **0** · account **0** · non-terminal intents **0** · working orders **0** — FLAT |

| service | pid | NRestarts | | service | pid | NRestarts |
|---|---|---|---|---|---|---|
| schwab-1m-v2 | 2531255 | 0 | | strategy | 2519776 | 0 |
| oms | 2519765 | 0 | | market-data | 2202865 | 0 |
| control | 2203323 | 0 | | reconciler | 2202771 | 0 |
| market-capture | 2202817 | 0 | | | | |

The OMS restart at 16:26 was **the #869 fence's first live invocation — it ran and said GO**
("flat on every real-money account, zero managed rows, all sources fresh"). The #860 A7 cron is
active at exactly one repository-owned target and its 17:00 tick **ran the new provenance
classifier** (STATUS.txt header shows `refusal_origin`).

# ⭐ TOMORROW'S ONE JOB: RUN THE READING

The 09-02 session is the **first honest grade session** — one regime, everything deployed
(#858/#859/#860/#861 census/#862/#863/#864/#866/#867 C42/#868 S7/#869 Q21-1/2/4/#870 arm-guard/#871).
The reading is scripted (`grade_session.sh <date>` in the session scratchpad; queries reproducible
from the pre-stated denominators in the journal, 2026-09-01T19:15Z + the #870 refinement 21:52Z).

**The governing splits, pre-stated BEFORE the session:**
- **Event-dependent** (zero = UNEXERCISED, a valid session): #858 duplicate grade + #863
  denominator (both need ≥1 live mirror up-cross); #859 cap grade (needs a capped segment +
  breaking quote); the BLOCK-ruling cost line (needs a filled claim + reclaim attempt).
- **Expected-every-session** (zero = **A FINDING**, not a quiet day): S7 first-fill stamps ·
  D23 population read · zero-hold VETO lines on any mirror day · Q21's
  `[WEBULL-PREMARKET-UNPROTECTED]` count == pre-market `live:orb` fills · C42 joiner caps.
- **#870's zero DECOMPOSES or reads as nothing**: BUY-arm count (denominator) + refusal count +
  per-armed-bar DB-adjacency falsifier query, ⚠ with the TWO-POPULATION caveat named: the guard
  reads the strategy's IN-MEMORY previous bar; the gap census reads the DB series;
  divergent-by-construction — a mismatch is evidence to investigate, never auto-interpreted.
- ⛔ **No reading spans a fix date.** Split at `1f5da81`-deploy (16:29 ET) and `e24913b`-deploy
  (~17:45 ET); pre-fix data wears the label. Two post-fix days conclude NOTHING either direction.

# STANDING RULES SET TODAY (full text in memory + handoff-log)

1. **Merged ≠ deployed bit hard**: three grade lines nearly read UNEXERCISED when the truth was
   NOT-DEPLOYED; LIDR traded through a known-fixed-undeployed defect. Step 0 of any grade: pin
   the SHA that was RUNNING.
2. **A reviewed number is a claim the reviewer co-signs** — and verifying inputs is NOT deriving
   the reduction (the 27→16 withdrawal; three signers, none had derived it).
3. **A baseline named by a moving ref is not a baseline — pin by SHA** (the void-control
   retraction; #858's replay was immune because it pinned `327f843`).
4. **One-occurrence rule, symmetric**: single-instance findings close (resurface with evidence);
   two post-fix days prove nothing; rates NEVER span a change — split at the fix date; a
   too-small window becomes an ACCUMULATING WATCH.
5. **Do not split a deploy for attribution** — pre-stated denominators already attribute.
6. **BUILT-BUT-NEVER-INVOKED is a class** (3rd instance: preflight uninvoked until #869). Ask
   WHO INVOKES THIS of every artifact — a call-site or a schedule, never the file existing.

# OPEN / OWED

| item | owner | state |
|---|---|---|
| Run the 09-02 grade reading | claude-1 | **the priority**; script staged |
| #865 merge (docs-only) | codex-2 | pinned at `def705cc`, mergeable |
| S8 — SSM/WETO/GYGY stale-anchor harm-linkage | claude-1 | small reading, owed |
| Webull mirror refusal ACCUMULATING WATCH | unowned — needs operator owner | spec in journal 21:15Z: per-session counts by `refusal_origin`+code, per regime, graded only when the post-fix window is representative |
| Webull eligibility pre-check (M4: `TICKER_ID_CAN_NOT_TRADE` dominates mirror rejects) | candidate row — operator to board | criterion-based spec ready |
| #870 within-ATR-window residual | dormant | new-row-with-evidence if `[V2-ATR-ARM-GAP]` reads zero while gap-days continue |
| JZ 08-20 wrong flip stamp | single-occurrence — CLOSED per rule | resurfaces with a second instance |

# ⛔ WATCH ITEMS

- The `[V2-ATR-ARM-GAP]` zero-decomposition is IN the grade reading (above), not a separate watch.
- A7 reject alarm: GREEN = no new real-money class and no ≥2-day streak — **NOT zero refusals.**
- `[BROKER-SYNC-CENSUS]` every 300s at zero failures is the broker-read heartbeat (B25 closed).
