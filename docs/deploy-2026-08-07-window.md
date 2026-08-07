# DEPLOY INSTRUCTION — window of 2026-08-07, after the close

> Written **before** the window, deliberately. Every near-miss today came from reasoning under time
> pressure inside one. Nothing in this document should need to be decided at the console.
>
> ⛔ **Times are ET.** ⛔ **Logs rotate at 00:00 UTC = 20:00 ET** — after 20:00, a single-file grep
> loses the day; use the rotated file too.

---

## ⭐ TIMING — read this first

**Nothing is scheduled. This runs on the operator's go.** The window opens ~16:05.

### ⚠️ Earlier is materially simpler than later
**Every verification grep in §2 needs BOTH files after 20:00 ET** (00:00 UTC log rotation). That
trap has already produced a false zero this week — a single-file grep read 7466 lines as 0. It is
handled throughout this document, but **the earlier run is the cheaper one**: before 20:00 the
verifications are single-file and the trap cannot fire at all.

### Duration: **roughly 20–30 minutes**
**Mostly verification, not execution.** The three restarts and the Redis command are a couple of
minutes; §2's six checks are the rest, and the census alone has a **5-minute** emit interval that
cannot be hurried.

### ⛔ ATTENDED THROUGHOUT — not just at the start
The operator needs to be **present for the override decision**, which lands partway in (§3), not
merely to authorise the beginning. Do not start on a "go" that is only a go for step 1.

### The one open decision
**The gate override for #666.** Everything else in this document is pre-decided.

---

## 0. WHAT IS IN SCOPE — and the merge state, verified on the BRANCH

| PR | change | service | on `origin/main`? |
|---|---|---|---|
| #660 | P0a census — `[OMS-P0A-CENSUS]`, log only, no flag | **OMS** | ✅ `_maybe_emit_p0a_census` present |
| #662 | A2 reverse-reject — back off + page, **FLAG OFF** | **OMS** | ✅ `A2_NOT_SELLABLE_REASON_SUBSTRINGS` present |
| #664 | CW_FLIP fans out to the Webull leg | **OMS** | ✅ `_cw_flip_pending.add` present |
| #663 | P2.11 — a DISARM line on every `cw_armed` clear, log only | **v2** | ✅ `[V2-CW-DISARM]` present (5 refs) |
| #666 | RCEL — cancel an RTH-placed resting order at window close | **v2** | ⏳ open, CI running |

⛔ #665 is comment-only (`events.py`, `strategy_engine_app.py`). **Nothing to deploy or verify.**

---

## 1. SEQUENCE

```
1.  OMS   pull to origin/main  → restart oms-risk        (#660, #662, #664)
2.  v2    pre-flight → gate decision → restart schwab-1m-v2   (#663 + #666, if #666 merged)
3.  Redis CONFIG SET maxmemory ~2GB  → PERSIST TO THE CONFIG FILE
```

⛔ **One pull, three separate verifications.** The three OMS changes land together; they do **not**
verify together, and a single "OMS looks fine" is not evidence for any of them.

⛔ **Before the v2 restart, run the bar-hole checklist** (`project_mai_tai_restart_bar_gap_checklist`).
A restart leaves a bar hole and ATR spans it. **Warmup repairs MEMORY, not the DB.**

---

## 2. VERIFICATION — each one separately, and what ABSENCE means

### 2a. #660 P0a census — the only one with a hard, same-night pass/fail

- **Grep:** `grep '\[OMS-P0A-CENSUS\]' /var/log/project-mai-tai/oms.log`
- **Expect:** a line **within 5 minutes** of the restart. The emit interval is
  `interval_seconds: float = 300.0` and `_maybe_emit_p0a_census()` is called from
  `sync_broker_state` **outside any condition**, so it emits even with nothing to report:
  `evaluated=0 held=0` is a **PASS**, not a null.
- ⛔ **Absence = THE DEPLOY FAILED.** This is the one line that cannot be explained away by a quiet
  market. If it is missing, the new code is not running — check the service actually restarted onto
  the new SHA before touching anything else.
- ⛔ Control: `grep -c 'OMS-V2' oms.log` should be non-zero. A zero there means the grep is wrong,
  not that OMS is silent. (This exact false zero happened this week on the v2 log.)

### 2b. #662 A2 — verified by NOTHING HAPPENING

- **Grep:** `grep -E '\[OMS-A2-(BACKOFF|EXIT-BLOCKED)\]' /var/log/project-mai-tai/oms.log`
- **Expect:** **zero lines.** The flag is OFF and `_a2_enabled_for` is scoped to `live:orb`.
- ⛔ **Absence IS the pass here** — the inverse of 2a. Any `[OMS-A2-*]` line tonight means the flag
  is not off, or the scoping is wrong. Treat one line as a **stop**.
- ⛔ **Merging authorised nothing.** The flag stays OFF through this deploy.

### 2c. #664 CW_FLIP fan-out — declare UNEXERCISED **in advance**

- **Expect on the next flip:** a CW_FLIP exit on **both** `live:schwab_1m_v2` **and** `live:orb`
  (previously only the primary). Reason string `CW_FLIP`.
- ⚠️⚠️ **THIS IS THE WEAKEST VERIFICATION OF THE NIGHT, and it is weak by construction.**
  **There is no dedicated log marker for the fan-out** — I went looking for one; `_cw_flip_pending`
  is set and discarded with no line of its own. ⇒ **The evidence is INDIRECT:** a CW_FLIP exit
  appearing on both accounts. It is inference from outcome, not observation of the mechanism.
- ⛔ **`UNEXERCISED` is the EXPECTED outcome, not a disappointment.** A flip has to fire, on a held
  position, inside the window. Most nights that does not happen. Writing UNEXERCISED is the correct
  and complete result — **not a failed verification, and not something to go hunting for.**
  Not "clean", not "0", not "passed". [[feedback_unexercised_is_not_a_result]]
- ⇒ Confidence in #664 tonight rests on **the tests and the code read**, not on the tape. Say so.

### 2d. #663 P2.11 disarm line — a count that must MOVE

- **Grep both:** `[V2-CW-ARM]` and `[V2-CW-DISARM]` in
  `/var/log/project-mai-tai/schwab-1m-v2.log` ⛔ **`journalctl -u schwab-1m-v2` is a FALSE ZERO.**
- **Expect:** ARM/DISARM pairs, and the **unpaired-ARM divergence count drops from 9**.
- ⛔ The divergence has never been zero and **is not expected to be zero tonight** — the anchor reset
  path is only one of the clears. A drop from 9 is the pass; **0 would itself be suspicious.**
- ⛔ Pair with **no filter**. The previous count came from an unfiltered pairing.

### 2e. #666 RCEL — **tomorrow's check, not tonight's**

- **Tomorrow at ~16:05:** no resting order left working after the entry window closes.
  Sweep the account for live orders; expect `0 live order(s)` on `live:schwab_1m_v2`.
- ⛔ Tonight can only show that v2 started. **The bug is only observable at a window close**, so
  claiming it tonight would be claiming a result from an unexercised path.
- Watch for `[V2-RESTING-CANCEL] reason=window_closed` where previously
  `[V2-RESTING-EH-DISARM] reason=window_closed` appeared with no cancel.

### 2f. Redis

- **Command:** `CONFIG SET maxmemory 2gb` then **write it to the config file** — `CONFIG SET` is live
  and reversible and **reverts on the next Redis restart** if not persisted.
- **Sizing, already worked:** steady state at full maxlen ≈ 1.2–1.3 GB; box has 8 GB, `available`
  3.7 Gi, Redis RSS 286 MB.
- ⛔ **`snapshot_batch_stream_maxlen` STAYS 180.** Load-bearing — scanner warmup prefill needs 120.

---

## 2g. ⛔ THE TEST-COUNT BASELINE MOVED TODAY — 1847 → 1883

Anyone comparing tonight's suite against a remembered number will read a **false delta**.

| | count |
|---|---|
| `main` **this morning**, before today's merges | **1847** |
| `main` **now** (after #660/#662/#663/#664/#665 and their tests) | **1883** |
| `claude/virtual-clear-instrumentation` (#668, +4) | **1887** |

⛔ **Quote both numbers, always.** "1854" appears in this session's earlier notes for the RCEL branch
— that was 1847 + 7, measured **before** the merges landed. It is stale, not wrong-at-the-time.
#666's own CI is the authority for that branch.

---

## 3. THE GATE DECISION — pre-made, so it is not reasoned out at the console

**Expect the armed-segment pre-flight gate to REFUSE the v2 restart.** Arming is bar-driven and runs
to 20:00, so segments arm after 16:00 routinely.

**The recorded reasoning, to be pasted verbatim into the deploy record:**

> The armed-segment gate exists to prevent a **lost entry**. #666 fixes a bug that risks an
> **unwanted entry with no protective stop, held overnight** — a resting order left working past the
> close, fillable in extended hours where Schwab refuses the STOP leg. These are not comparable
> harms, and the gate's cost model was never written against this case. Overriding is therefore a
> deliberate exception on a known asymmetry, **not** a judgement that the gate is wrong.

⛔ **Only the operator can authorise the override.** Use the documented token; record the reasoning
at the time of use.

### ⭐ STATE THIS AT THE MOMENT OF THE DECISION, NOT AFTERWARDS
> **Declining costs less than it sounds.** If the override is declined, the **three OMS changes and
> Redis proceed anyway** — including **#664, the one that matters**, which needs **no v2 restart**.
> What is deferred is #663 (log only, no behaviour) and #666 (real, but its next exposure is
> tomorrow's 16:00 close, so it fits tomorrow's window).

⇒ The decision is *"do we take #666 tonight or tomorrow"*, **not** *"do we deploy tonight"*. Say that
before he decides — afterwards it reads as consolation.

⭐ **Afterwards, put the asymmetry in the gate's own comment** — otherwise the next person re-derives
it from scratch under the same time pressure.

⛔ **A harm analysis is valid for its window, not its gate.** This reasoning is good for **tonight's
post-close window only.** Do not carry it to a pre-open window; at 06:20 the 04:00 anchor has already
fired and the cost of a capped segment is the whole session.

---

## 4. FALLBACK — if the gate blocks and the override is declined

**The three OMS changes and Redis proceed anyway.** They need only an OMS restart.

⭐ **#664 (CW_FLIP fan-out) does not need a v2 restart, and it is the one that matters** — it is a
real, priced defect (the legs exit independently; AAOG cost 2.5 points). Losing the v2 half of the
window costs the two **log-only/next-day** items, not this one.

Deferred in that case: **#663** (log only — no behaviour) and **#666** (RCEL — real, but its next
exposure is tomorrow's 16:00 close, so it can go in tomorrow's window).

---

## 5. ⛔ NOT TONIGHT — the explicit list, so it cannot drift

| item | why not |
|---|---|
| **The ledger fix** (open item 12 / `virtual_positions` false zero) | **Log line first** — `[VIRTUAL-CLEAR]` lands tomorrow, then the next occurrence names itself. ⛔ Do not fix blind between two candidate causes |
| **Ship 2 and read C** predicate switch to `oms_managed_positions` | queues **behind** the ledger fix. Would be a fourth same-day change to a script already broken three times today |
| **`snapshot_batch_stream_maxlen`** | **STAYS 180.** Not a tuning knob tonight |
| **Open item 8** (reconciler severity) | ⛔ blocked on item 12 — downgrading that severity would suppress real defects |
| **anything else** | nothing else. No gate edits, no opportunistic fixes |

⛔ **No gate edits inside a deploy window.** This rule was nearly broken twice today; both were
caught by luck.

---

## 6. ONE LINE FOR THE REPORT

> **The Redis change buys time. It does not close the loss path.** `allkeys-lru` still evicts whole
> keys, every reader still uses an in-memory cursor, and there is still **zero `xreadgroup` in the
> codebase**. A quieter pager must not be read as a fixed mechanism — the actual fix is a durable
> consumer group on `strategy-intents` and `order-events`, and it is not in this deploy.
