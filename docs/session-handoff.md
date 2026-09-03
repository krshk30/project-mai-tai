# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-02 19:25 ET.** Batch `2026-09-02-blind-watch-day`. Integrator for
this rotation. Needs `codex-2`'s review before merge — the author never reviews.

---

# PRODUCTION — main and box IN SYNC

| | |
|---|---|
| box (deployed) | **`f437100ba4ec22cbed5c41281f8a5fbb987fa57a`** — verified ON THE BOX 19:25 ET, checkout clean |
| main | `f437100` — identical. Three merges today: #873 (`50f8c055`), #874 (`028817d`), #875 (`f437100`) |
| open PRs | today's handoff PR only |
| exposure (19:25 ET) | virtual **0** · account **0** · managed **0** · non-terminal intents **0** · working orders **0** — FLAT |

| service | pid | NRestarts | | service | pid | NRestarts |
|---|---|---|---|---|---|---|
| schwab-1m-v2 | **2531255** | 0 | | strategy | 2732881 | 0 |
| oms | 2710560 | 0 | | market-data | 2202865 | 0 |
| control | 2733285 | 0 | | reconciler | 2202771 | 0 |
| market-capture | 2202817 | 0 | | | | |

# ✅ MIXED CODE — RULED DELIBERATE, DO NOT "FIX" IT

**`schwab-1m-v2` holds PID `2531255`, its pre-deploy PID.** It was not restarted across today's
three deploys, so the real-money v2 bot runs **pre-#874 code** while oms/strategy/control run
`f437100`.

⭐ **`codex-2` confirmed 2026-09-03: this was DELIBERATE, to avoid an unrelated bar hole.** The
question "decision or omission?" is answered — it was a decision.

⛔ **Do NOT restart v2 to bring the fleet onto one version.** A restart >2 min leaves a hole in
`strategy_bar_history`, and the ATR then computes True Range across it — inflating ATR and pushing
every resting order too high. That hazard is the whole reason v2 was left alone. Restart it only
when there is an independent reason to, and then only with the full checklist: outside 07:00–16:00
ET, account-flat from broker truth, working orders zero or known, then `[V2-BOOT-HOLD] released`
+ warmup spanning the outage + a clean bar-continuity check.

The split is believed benign: #874's `events.py` change is an additive field with a default, and v2
touches none of the new tables. It resolves on its own at the next restart v2 needs for its own
reasons.

# ⭐ TOMORROW'S FIRST READ: DOES THE SEED-EXPOSURE WATCH ACTUALLY SPEAK?

`#873` is merged and deployed but **UNPROVEN**. The watch was blind for **ten consecutive sessions**
(2026-08-20 → 09-02), refusing `⛔ CANNOT SEE — REFUSING: no DSN` on every 5-minute tick. Its window
is **04:00–11:00 ET**, so the first live verdict lands tomorrow morning.

- **PASS** = a real `swept N of N` / `VERDICT` line in `/home/trader/seed_exposure_out/latest.txt`.
- **FAIL** = another `CANNOT SEE`, or a `09:12` readiness verdict that is still AMBER.
- ⛔ Do not mark `#873` verified on the strength of the merge. Merged ≠ deployed ≠ proven.

# ⛔ STILL BROKEN AFTER THE DEPLOY — the 09:12 readiness path

`git pull` does **not** fix it. Root's crontab runs `/home/trader/preopen_readiness_cron.sh`, a
**separate unversioned copy** (inode `524437`) of `ops/health/preopen_readiness_cron.sh`
(inode `1861068`). Contents were identical, so it *reads* as versioned and is not.

⇒ Fix by **repointing root's crontab at the repo path**, never by re-copying (a re-copy recreates
the drift). Operator-visible change to root's crontab; needs explicit OK. Until then the 09:12 slot
runs the old code and the daily AMBER continues.

# STANDING RULES SET TODAY

1. **A REPEATING alert is a DEAD alert.** Same level + same string N days running is not N warnings,
   it is one unfixed defect plus N−1 desensitising events. `⛔ CANNOT SEE` ten days running meant the
   watch was OFF. A refusal is only half the contract; the other half is that somebody acts on it.
2. **An alarm must name itself.** The readiness line said `SEED-EXPOSURE: CANNOT SEE` with no cause
   for ten days while the cause (`no DSN`) sat unused in `$SEED_LINE`.
3. **When one wrapper in a family misbehaves, diff it against the family, not against its own spec.**
   The defect was invisible in the check's logic and obvious in one grep across siblings.
4. **Check the ACCOUNT before reading any v2 timeline.** Fills that look like direct hits were
   `paper:polygon_30s`, a different bot. Resolving `broker_account_id` dissolved a false hit *and*
   confirmed a prior trace.
5. **A count of attempts is not a count of events.** Suppression markers fire once per reprice cycle,
   not once per lost entry.

# OPEN / OWED

| item | owner | state |
|---|---|---|
| ~~Confirm the v2 non-restart was deliberate~~ | codex-2 | ✅ **CLOSED** — confirmed deliberate 2026-09-03; do not restart v2 to "fix" it |
| Prove `#873` — read the seed-exposure verdict after 04:00 ET | claude-1 | scripted; PASS/FAIL stated above |
| Repoint root's crontab at `ops/health/preopen_readiness_cron.sh` | operator + codex-2 | needs explicit OK; blocks the 09:12 fix |
| Run the 82-event exit-rule measurement | claude-1 | ⛔ **hard deadline 2026-09-07** — `market_capture_quotes` prunes at 14 days and the 08-24 session leaves the tape then. 80/82 gradable provisionally |
| `[PAPER-EXIT-REFUSED]` marker has no consumer | unowned — needs an owner | detects-but-nobody-listens class; a bare zero cannot separate "never fired" from "nobody looked" |
| Webull reclaim-slot asymmetry | unowned — needs an owner | Schwab may take 2 entries per segment, Webull 1; the Webull slot is not released when its leg exits. Denominator not yet derived |
| PAPER1 — successor exit question above +5% | codex-2 | blocked on exercised paper-harness evidence |

# ⛔ WATCH ITEMS

- **C42 stale-anchor residual.** WETO satisfied C42's falsifier on 09-01: a joiner armed on a
  prior-session anchor with no cap/roll line, killed at 07:01:02 by `session_anchor_reset` — not by
  the 04:00 roll and not by the seed cap. Exposure is mostly structural (04:00–07:00 sits before
  entries open) with a **~62-second residual inside the tradable window**. Accumulating watch.
- **SEG1 is falsifiable but THIN.** True identity returned 13 on the 09-02 tape and a wrong identity
  (dropping `entry_slot`) returns 12 — but the discriminator is exercised by **exactly one slot**
  (BIAF `58f2bc1e`, first + reclaim). Report as n=1, never as "SEG1 verified".
- **Hold-until-proven 0/82 rests on 4/82.** Only four rows are "still open at 16:00" (CELU −0.94,
  AEHL −0.58, NCRA −0.35, FLYE −2.26). Correct as measured; a small denominator, never evidence that
  holding is free.
- **A7 reject alarm:** GREEN = no new real-money class and no ≥2-day streak — **NOT zero refusals.**
