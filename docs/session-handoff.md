# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-08-31 17:15 ET.** Batch `2026-08-31`. Integrator for this rotation.
Needs `codex-2`'s review before merge — the author never reviews.

---

# PRODUCTION — main and box IN SYNC

| | |
|---|---|
| main / box | **`77ae556f73da3b6eb0079acf43610faa8affea8e`** |
| checkout | clean |
| open PRs | **0** |
| exposure | managed **0** · virtual **0** · account_positions **0** · non-terminal intents **0** · working orders **0** |

| service | pid | | service | pid |
|---|---|---|---|---|
| schwab-1m-v2 | 2364618 | | strategy | 2203071 |
| oms | 2365948 | | market-data | 2202865 |
| control | 2203323 | | market-capture | 2202817 |
| reconciler | 2202771 | | | |

`NRestarts=0` on every unit. Only v2 and OMS moved tonight, each in its own restart.

---

# ⭐⭐ TODAY IN ONE LINE

**Two live-money defects were found by 1-share positions worth $8.50, fixed, mutation-verified,
deployed the same day — and one of them was proven working in production twelve hours after it was
found.**

## #852 — v2 warmup latch · **EXERCISED, NOT MERELY DEPLOYED**

**The defect:** `_rest_warmup_done` could only be entered by a REST bar younger than 300s, on the
docstring's stated assumption *"REST is the only live feed."* **Not reliably true** — the streamer is the live feed and
REST backfills history, so during the observed early-pre-market interval REST returned days-old
bars and the latch had **no elapsed-time bound**.

⛔ **SCOPE, corrected by `codex-2`:** this is NOT "structurally unreachable". At **07:01 ET today the
PRE-FIX code warmed AEHL and YDDL from REST and released the hold naturally.** REST freshness is
unavailable during part of the early pre-market, not absent from it. The defect is the **missing
streamer route plus the absent timeout**, not an impossible REST path. At 05:40 ET the boot hold had been stuck
since 04:06 with `rest_warmed=0`, `warmup_pending=AEHL,YDDL`, while both symbols had bars **56
seconds old** in the DB.

**Tonight's deploy proves the fix:**
```
REST 1/4 · STREAMER 3/4 · timeout 0/4
20:40:48.894Z [V2-BOOT-HOLD] released — 12.9s after start
"fresh-source warmup complete for AEHL (bar_age_seconds=283.7)"
```
⭐ **Three of four symbols warmed via the path #852 added** — a route that did not exist this
morning. The 369s timeout stayed **UNEXERCISED**, as intended.

## #853 — CW profit floor · **UNEXERCISED**

**The defect:** CW mode maintained and *persisted* a high-water floor and **never consumed it**,
substituting a fixed entry+2% floor. AEHL entered 6.07, bid peaked **7.58**, the ratchet stood at
**~7.48895**, bid fell to 7.46 — and CW returned a frozen **6.1914**. **0 of 376 quote ticks**
breached it. Operator closed by hand at **operator-reported ~+18.6%**; ⛔ internal execution price and P&L are **`COULD_NOT_TELL`** — the manual close was never recorded.

⭐ **The control was in the same log:** YDDL breached its −5% stop and exited correctly via
`CW_HARD_STOP`. **Downside path executed, upside path did not.**

⛔⛔ **Why it hid for months — measured, 07-14→08-31: 339 of 406 profit exits (83.5%) were taken by
the BROKER'S OCO BRACKET**, only 67 (16.5%) by the software ladder. In RTH the bracket silently did
the job. **Extended hours has no bracket, which is the only reason this surfaced.**

⛔ **The fixed floor was DELIBERATE** — chosen for restart determinism, *"no durable state needed."*
The fix is safe only because a lost ratchet **degrades to the fixed floor**:
`max(fixed_floor, ratcheted)`, one comparison, tested both directions.

**Verdict tonight: `0` CW-floor opportunities over `0` managed positions ⇒ UNEXERCISED, not PASS.**

## #854 — scanner alert · S0 fallout

The alert held the **pre-rotation credential**; auth failed, stderr was discarded, the return code
ignored, and the failure was **rendered as `ROWS=0`** — then it asserted a cause it never measured.
Capture was writing: 37 rows, newest 5 minutes before the page. Now compares against five matching
weekdays, alerts below 20% of median, prints both populations, and a DB failure yields
`COULD_NOT_TELL / row_count=UNMEASURED` with a non-zero exit.

## #851 — fleet toolkit macOS · ⭐ **and it fixed a false pass in `promote.sh`**

⛔⛔ **The gate that authorises close-outs had a false PASS.** The prior code normalised both sides
through `$(...)` to cure a false *mismatch* — and that created a false *match*: **a manifest
differing by ONE TERMINAL NEWLINE compared equal and AUTHORISED A CLEAR.** Both sides now hash from
files. The selftest pins both directions.

**Two-platform verification, both independent:** Windows **126/0** (run by `claude-1`, counted two
ways) · macOS **130/0**, `SELFTEST_RC=0` (run by the **operator** in Terminal — third-party, not
codex self-report). The 130-vs-126 gap is four Darwin-only BSD controls and is **expected**.

✅ **The Windows-only close-out restriction is LIFTED.**

---

# 🔴 TOMORROW — THE MAC CUTOVER

**Working from the Mac starts tomorrow.** The migration is complete and verified: repo at
`~/Projects/project-mai-tai`, memory loaded, SSH to the box, `gh` authed, toolkit green.

⛔⛔ **THE CUTOVER MUST CARRY MEMORY, NOT JUST THE JOURNALS.** Files written on Windows on 08-31
that the Mac does not have:
```
feedback_something_else_was_covering_for_it.md   (new — explains BOTH of today's defects)
project_mai_tai_v2_three_exit_rules.md           (the CW floor defect)
MEMORY.md                                        (index)
project_mai_tai_v2_trading_window_and_exit_churn.md   (08-30)
```

**Procedure — only AFTER this batch is promoted** (the archive and journal rotation happen *during*
promote):

```bash
# WINDOWS
S=~/Desktop/mac-cutover && mkdir -p "$S/bundle"
cp -r ~/.claude/projects/C--Users-kkvkr/memory "$S/bundle/memory"
cp -r ~/.claude/mai-tai-fleet "$S/bundle/mai-tai-fleet"
cd "$S/bundle" && find . -type f -print0 | sort -z | xargs -0 sha256sum > "$S/MANIFEST.sha256"
cd "$S" && tar -czf mac-cutover.tar.gz bundle MANIFEST.sha256 && sha256sum mac-cutover.tar.gz

# MAC — ⛔ FAILS CLOSED. Verification GATES the replacement; nothing is deleted.
set -euo pipefail
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
MEM=~/.claude/projects/-Users-velkris/memory
FLEET=~/.claude/mai-tai-fleet

tar -xzf ~/mac-cutover.tar.gz -C ~
cd ~/bundle
shasum -a 256 -c ../MANIFEST.sha256          # ⛔ non-zero exit here ABORTS under set -e

mv "$MEM"   "$MEM.pre-cutover-$STAMP"        # rename, never rm -rf
mv "$FLEET" "$FLEET.pre-cutover-$STAMP"
cp -R ~/bundle/memory "$MEM"
cp -R ~/bundle/mai-tai-fleet "$FLEET"
chmod +x "$FLEET"/*.sh
"$FLEET"/checksums.sh verify
```

⛔⭐⭐ **Why this shape:** the earlier version piped verification into `grep -c "OK$"`, which merely
**prints a count** — it gated nothing, and the `rm -rf` ran regardless. Without `pipefail` a
`shasum` failure was not propagated either, so **a corrupt archive would have deleted the live
memory and fleet board anyway.** Here `shasum -c` exits non-zero under `set -e` and aborts before
anything is touched, and the originals are **renamed, not removed**, so rollback is `mv` back.
⛔ Delete the `.pre-cutover-*` copies only after the behavioural check below passes.

⛔⭐⭐ **AFTER THE CUTOVER, WINDOWS MUST STOP WRITING FLEET JOURNALS ENTIRELY.** One writer per file
is the whole guarantee; two machines appending in one batch diverge irreconcilably and
`manifest.sh` cannot reconcile it. Keep Windows as a **read-only** fallback.

⚠️ **Verify behaviourally, not by file count** — ask a fresh Mac session *"what does 'something else
was covering for it' mean?"* A file count proves files moved; only an answer proves memory loaded.

---

# OPEN ITEMS

| item | owner | state |
|---|---|---|
| **Manual-close lifecycle defect** | codex-2 | boarded. A manual broker close leaves **no execution record** and orphans the managed row. Today one sat 0.14% from firing a sell into a flat account |
| **Three non-cron research scripts** | codex-2 | hold dead embedded credentials; cleanup only |
| **CI rule** | codex-2 | scripts load the DSN from the managed env; DB failure ⇒ `COULD_NOT_TELL` + non-zero exit, never zero. **CI scans RESOLVED CRON TARGETS, not filename patterns** |
| **AEHL exit price / P&L** | — | permanently `COULD_NOT_TELL`. The manual close was never recorded |

---

# OPERATIONAL RULES CONFIRMED TODAY

1. ⭐⭐ **Something else was covering for it.** Ask of any guard: *if I removed everything else,
   would this still work?* The cover is a broker order, a second data source, or a branch that runs
   first — **none appear in the file you are reading.** ⭐ Scoped: the broker OCO covered #853; REST
   *becoming* fresh around 07:01 masked #852's missing streamer route.
2. ⭐ **Trade small size in the degraded environment on purpose.** Extended hours removes the broker
   bracket outright, and makes same-day REST freshness intermittent rather than absent. **$8.50 exposed two months-old bugs.**
3. ⭐ **Look for the case in the same window where the mechanism DID work.** YDDL vs AEHL settled
   the CW diagnosis in minutes.
4. ⛔ **Require the rationale before the patch.** The fixed floor was deliberate; "just make it
   trail" would have shipped a regression dressed as a fix.
5. ⛔⛔ **A PATTERN MATCH IS A LEAD, NEVER A FINDING.** `claude-1` made **four** grep errors today,
   each a confident wrong number, one causing a **false live-money escalation**. Every one was
   caught by a *second method*. Confirm twice before reporting a number.
6. ⛔ **A guard tested only on its known inputs is untested where it earns its keep.** Four PRs
   today had a surviving mutant in round 1; every one was on the *safety* half, not the feature.
7. ⛔ **A deploy guard bound to a stale SHA refuses — that is correct.** It cost 25 minutes and
   prevented shipping an unreviewed delta.

## Memory pointers

`[[feedback_something_else_was_covering_for_it]]` · `[[project_mai_tai_v2_three_exit_rules]]` ·
`[[project_mai_tai_v2_post_boot_promotion_uncapped_fleet_hold]]` ·
`[[feedback_a_watch_that_fails_to_a_false_clean]]` · `[[project_mai_tai_review_pin_gate_mechanics]]`
