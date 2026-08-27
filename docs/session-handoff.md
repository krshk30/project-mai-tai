# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-08-26 19:15 ET**, read-only against production. Integrator for this
close-out. Needs `codex-2`'s review before merge — the author never reviews.

---

# PRODUCTION — main and box IN SYNC

| | |
|---|---|
| main / box | **`4a206181307885b8e8cb28b51a24171aaafbb20a`** |
| checkout | clean |
| open PRs | **1 — this handoff PR (#804) itself.** Zero others |
| ledgers at close | flat — `account_positions` 0, `virtual_positions` 0, open managed rows 0, working orders 0 |
| ⭐ `fills` (the independent ledger) | every symbol netted to 0 on 08-26 |

**PIDs as of 19:10 ET. `NRestarts=0` on every unit.**

| service | pid | since (UTC) | moved today? |
|---|---|---|---|
| control | 1570337 | 08-26 12:16:13 | ✅ restarted (token window) |
| oms | 1642844 | 08-26 20:10:05 | ✅ restarted (#800) |
| strategy | 1642854 | 08-26 20:10:05 | ✅ side-effect of #800 |
| schwab-1m-v2 | 1662275 | 08-26 22:25:05 | ✅ restarted (#802) |
| market-data | 1528374 | 07-27 17:35:52 | — |
| reconciler | 1514317 | 08-25 23:51:35 | — |

**Three restarts all day. The last two merges landed with ZERO** — see `sync-only` below.

---

# ⭐⭐ WHAT SHIPPED, AND WHAT IS ACTUALLY EXERCISED

| PR | merged | what | deployed | exercised? |
|---|---|---|---|---|
| #796 | 14:13Z | §82 fan-out lifecycle design | docs | — |
| #798 | 18:59Z | §82 reply-loop evidence correction | docs | — |
| **#800** | 20:08Z | fan-out identity + Webull tick spread | ✅ OMS+strategy 20:10Z | ⛔ **UNEXERCISED, zero denominators** |
| **#802** | 22:24Z | v2 post-close lifecycle exit-only | ✅ v2 22:25Z | ✅ **YES — see below** |
| #801 | 22:37Z | C3/C2 bounds, C4/C5 evidence, C4 comment fix | sync-only, **no restart** | — |
| #803 | 23:01Z | fail-closed checkout sync without restart | sync-only, **no restart** | ✅ ran, 6/6 PIDs unchanged |

⛔ **#797 and #799 are CLOSED, not merged.** Their content is inside #800. Auditing "was #799
deployed?" by its own PR state returns the wrong answer.

## ✅ #802 IS EXERCISED — and the proof is a silence

**Within 3 seconds** of the 22:25:05Z deploy:
- `[V2-ENTRY-WINDOW-EXIT-ONLY]` census ran once: `evaluated=6 released=0 arms_released=0
  cancel_requested=0 held_positions=0 armed_after_close=0`
- `[V2-POST-CLOSE-ENTRY-BLOCKED]` fired **16 times** (22:25:07)

**Cumulatively through the evening: 27 as of 23:30:02Z, and still rising** while bars flow to
20:00 ET — XPON 22:43, WSHP 22:44, YYGH 22:49 are from this later set, not the 3-second one.
⛔ A cumulative marker count is meaningless without its as-of time; the first version of this
section reported the running total as if it were the 3-second burst.
- ⭐ **`[V2-CW-STATE-PROBE]` STOPS ENTIRELY after 22:25:05.** That line is emitted only by the
  entry state machine. Pre-deploy it printed every minute post-close (WSHP 22:21, XPON 22:22/22:23,
  all `armed=True`). Its silence is the guard working.

⛔ **A `tail -1` on that probe returns a PRE-deploy line and reads as "still armed."** That
mistake was made and caught tonight. Filter by timestamp or you will report a false alarm.

⇒ **Tomorrow's 16:00 boundary is the first FULL exercise** — the first crossing with live positions
and working resting orders, where `cancel_requested` can be non-zero. Tonight's zeros are correct:
the deploy landed after the close with nothing left to cancel.

---

# 🔴 WATCH TOMORROW — in priority order

1. **DAIC at 07:00** — confirm the 04:00 roll cleared its arm. #802 now also prevents a post-close
   restart from rebuilding one, but the roll itself is still unverified in anger.
2. **`ops/health/fanout_identity_acceptance.py` is BROKEN and unfixed.** psql does **not**
   interpolate `:'var'` in a `-c` string, so its window placeholders never substitute. It fails
   **closed** to `COULD_NOT_TELL` — no wrong verdict — but **#800's identity report cannot produce
   any reading until this is fixed.** ⛔ It is in `ops/**`, so it is **NOT sync-only eligible**.
3. **#802's first full 16:00 boundary** — read `cancel_requested=N` as an **UNVERIFIED count**, not
   a cancellation; #802 requests cancels and does not consume the outcome.
4. **#800's markers stay UNEXERCISED** until a fan-out opportunity occurs *and* item 2 is fixed.
   ⛔ #799's **refusal** path has zero live population by construction — all 15 historical rejects
   were raw-VALID collapsed by rounding, so only the **widening** path can exercise.
5. **WSHP 22:25 is a permanent bar hole** (restart minute; raw ticks existed). ⛔ XPON's gaps in
   that window were never checked against ticks — `COULD_NOT_TELL`, not continuous.

---

# ✅ DISK-vs-PROCESS GAP — MEASURED, AND IT IS CLEAN

Asked at the close because `#801` merged at 22:37Z, **after** the 20:10Z OMS restart, and landed via
sync-only. *Is there running code on disk but not in a process?* Measured per service:

| process | running | disk | behavioural gap |
|---|---|---|---|
| schwab-1m-v2 | `10ca1a7d` | `4a206181` | **0 non-comment lines** |
| oms + strategy | `3b2e9656` | `4a206181` | 227 lines — **all `#802` v2 code, in files these processes never import** |

⛔ **`#801` contains no runtime code at all.** Its only source change is the C4 comment. Verified for
OMS: **zero** imports of any `schwab_1m_v2` module — the two mentions at `oms/service.py:156` and
`:319` are **comments**, and OMS does not import `entry_gate`.

`strategy_core.schwab_1m_v2` is imported by exactly two files: **`services/schwab_1m_v2_bot.py`**
(the v2 bot — which is precisely the process that restarted at 22:25:05Z to load it) and
`backtest/replay.py`. ⛔ An earlier version of this line claimed *only* `backtest/replay.py`,
because the grep that produced it **excluded `schwab_1m_v2_bot.py`** to filter self-references and
filtered out the real importer. The conclusion is unchanged — OMS and strategy-engine import
neither file — but the evidence for it was stated wrongly.

Consistent with sync-only's own census: `python_ast_equal=1 runtime_ast_changed=0`.

## ⛔⭐⭐ BUT THE SHARPER RISK IS REAL, AND IT IS THE OPPOSITE SHAPE

The danger is **not** that `#801`'s bound is unloaded. It is that **the C2 10-second page threshold
and the C3 bounds are DESIGN NUMBERS IN MARKDOWN — no such code runs anywhere.** The consumers are
explicitly not built.

⇒ **A quiet C2 reading tomorrow must never be read as "the 10-second bound is working."** There is
no bound to work. That is the same `UNEXERCISED`-reads-as-`PASS` trap, one level further out: not a
zero from an unexercised feature, but a zero from a feature that does not exist yet.

---

# OPEN ITEMS

**(a) ⭐⭐ CONSOLIDATED — "a Schwab-shaped position guess where a Webull order lifecycle should be."**
Replaces three board entries that were one gap seen three ways. `_fetch_position_maps` is scoped to
the Schwab account; a Webull-only fill moves neither counter. Costs: 22 duplicate legs (median
**4.58%** worse), the phantom re-arm, and slot blindness (**≥9** fan-out-only fills proven — 16 of
53 buy fills 08-21→08-26 carried a usable arm id, **37 are `could_not_tell`**).
⛔ **#800 did NOT close this.** Every identity stamp site is on a **Webull** draft; the Schwab
primary carries none. The cross-venue join still rests on `cw_arm_bar_ts` + symbol.
⇒ **First increment is a shared key stamped on BOTH legs at the arm — not the consumer.**

**(b) Read-only credential route — UNDECIDED, and it blocks the Webull history probe.**
⛔ Correction on record: this is a **venue** credential question, not a Postgres one. The adapter
uses a single `app_key`/`app_secret` with no scope concept in our code.
⭐ `scripts/webull_sandbox_probe.py` already exists — **the open question is whether the sandbox can
answer the history date-floor question.** If it can, no new credential is needed.

**(c) C2 cancel consumer** — contract specified in #801, not built. Page threshold **10 s**, derived
from 1,092 outcomes (median 0.167 s, max 3.256 s, **0 beyond 10 s**). ⛔ Expect **~6 pages on a bad
symbol-day, 0 on a clean one** — six is the consumer exposing a real gap, not failing.

**(d) C3 post-exit stale-held refusals** — still firing: **48 on 08-26**, 49 total that day, all 49
preceded by a confirmed Webull sell fill. Bounded change, best test of the shared fix.

**(e) C5 EH protection** — capability matrix complete (#801). No native EH stop on either venue;
the in-process software ladder is the protection.

**(f) `sync-only` scope** — closes drift for **docs + tests + comment-only `src`**. ⛔ **NOT** for
`ops/**`, even for files no service loads. "We have sync-only" does not mean "drift is solved."

**(g) Gate checksums** — `checksums.sh verify` state unchanged from this morning's re-pin.

---

# ⛔ SCHWAB TOKEN

`refresh_token_expires_at` = **Mon 2026-08-31 16:02 ET** (weekday derived, not labelled). That is
the one needing a human. `expires_at` is the short-lived access token the refresher rotates itself
and is a ready-made false alarm. Control restarted today at 12:16:13Z and the first post-PID
refresh landed at **+28m32s**, inside the +35 deadline, with the refresh token unchanged.

---

# OPERATIONAL RULES CONFIRMED TODAY

1. **A rate quoted over a window containing the fix describes neither regime.** The mirror read
   2% across the 08-20 flag flip; split, it is **0.3% → 7.1%** with rejects **720 → 29**. #735
   worked. Matched control: mirror **6.2%** vs Schwab primary **9.2%**.
2. **Name both sides of a comparison.** `HEAD:path` against a SHA that *was* `HEAD` proves nothing;
   so does a hardcoded stale base. Both happened today.
3. **Check what is already written before forming a hypothesis** — twice in two days the design doc
   held the answer being re-derived.
4. `merged`, `deployed`, and `proven healthy` are separate claims. So is `synced`.
5. A `could_not_tell` must never decay into a pass, and a zero needs its denominator on the line.

## Memory pointers

`[[project-mai-tai-context]]` · `[[project-mai-tai-fleet-roster]]` ·
`[[project-mai-tai-architecture]]` · `[[feedback_verify_before_concluding]]` ·
`[[feedback_aggregation_masked_the_event]]` · `[[project_mai_tai_entry_composition_cap]]`
