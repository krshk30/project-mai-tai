# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-03 21:05 ET.** Batch `2026-09-03-1601-handoff-and-oversold-park`.
Integrator for this rotation. Needs `codex-2`'s review before merge — the author never reviews.

---

# PRODUCTION — main and box IN SYNC

| | |
|---|---|
| box (deployed) | **`b308a59460bb239bfe747da007db5d2316878c68`** — verified ON THE BOX 21:02 ET, checkout clean |
| main | `b308a594` — **identical**. Three merges today: #887 (`bf493d90`), #888 (`015e0786`), #889 (`b308a594`) |
| open PRs | **none** except this handoff PR |
| exposure (21:02 ET) | virtual **0** · account **0** · managed **0** · non-terminal intents **0** · working orders **0** — **FLAT** |

| service | pid | NRestarts | | service | pid | NRestarts |
|---|---|---|---|---|---|---|
| schwab-1m-v2 | 2897273 | 0 | | strategy | 2928192 | 0 |
| oms | 2928180 | 0 | | market-data | 2202865 | 0 |
| control | 2928441 | 0 | | reconciler | 2202771 | 0 |
| market-capture | 2202817 | 0 | | | | |

# ⚠️ TWO FLAG CHANGES TODAY — one ON, one deliberately OFF

| flag | state | note |
|---|---|---|
| `..._CONFIRMATION_EXIT_ENABLED` (CONF1) | **`true`** — **CHANGED TODAY** | operator ruling. Verified present in BOTH process environments, not just the env file |
| `oms_v2_eod_cancel_reexit_enabled` (EOD1601) | **`False`** (code default; **not set in env**) | shipped OFF by design — see below |

⛔ **CONF1 was disabled mid-session and re-enabled after the close.** It is ON going into 09-04.
Its own protection reconcile first executed today and produced **221 marker lines, all CHPT**.

---

# 🆕 EOD1601 — 16:01 cancel-and-reexit is DEPLOYED AND INERT

`#889` merged and deployed. At 16:01 ET, once per position per day, it would: harvest the working
SELL leg ids from that entry's **own order tree** → `DELETE` each once → **independently re-read and
confirm zero** → place a PM limit exit through `_emit_v2_exit_on_loop`.

⛔ **IT HAS NEVER RUN. The flag is off and `UNEXERCISED` is not `PASS`.** A day with no position
open at 16:01 logs `considered=0 outcome=UNEXERCISED` — an untested day against a denominator of
zero. Do not read the absence of alarms as evidence it works.

⚠️ **THE 16:01–16:05 UNPROTECTED WINDOW IS REAL AND DELIBERATE.** Schwab's PM session does not open
until ~16:05, so between the cancel and the first fillable moment the position has **nothing
working**. It trades a bounded four-minute gap for the open-ended one it replaces. Cents on one
share; **not cents on a real position.** The operator accepted this knowingly.

⛔ **THE DELETE AGAINST AN OCO CHILD HAS NEVER EXECUTED ANYWHERE.** That is the one unknown the
whole design rests on. `scripts/schwab_oco_child_cancel_probe.py` answers it for ~$2.11 attended
(operator chose PLUG, place 15:50, cancel 16:01). It is deployed at
`/home/trader/schwab_oco_child_cancel_probe.py`, md5 `1d085fc4a31a`, identical to the copy in the
repo. **🛑 ITS WRITE SEQUENCE IS STOOD DOWN pending `codex-2` clearing it.**

⛔ **RESTORE IS IMPOSSIBLE AFTER 16:00 — this contradicts an operator ruling and is not papered
over.** He ruled "if the PM exit is refused, re-place the bracket". Schwab **rejects a STOP leg
outside RTH** ("This order type is not available for this session", measured 2026-08-04), and
`_build_exit_only_oco_payload` refuses to build one. So the path logs `RESTORE_IMPOSSIBLE`, pages,
and names the 19:55 flatten as the real backstop rather than placing an order that would certainly
be rejected and calling it `RESTORED`.

# 🅿 OVSD1 — Schwab oversold storm, PARKED DELIBERATELY UNFIXED

CHPT 2026-09-03: **205 oversold refusals in 8 minutes** (13:45:35→13:53:32) plus **14 HTTP 429s in
14 seconds**. Closed by manual broker action.
⛔ Three different windows — the **220 reconcile verdicts** span ~14 min (13:39:04→13:53:31). Do not
quote one for another.

**Mechanism (sharpest statement we have):** `oms/service.py:3600` — a false `released` runs
`_native_oco_armed_confirmed_at.pop(...)`, so **a guess deletes the broker-confirmed fact** that
would have stood the ladder down. Then `3669` waves the send through and `3671`'s stand-down is
already disarmed. Once per tick.

⛔ **THE CLASS IS OLD — CONF1 DID NOT INTRODUCE IT.** `broker_orders` spans 2026-03-30→09-03:
**639 oversold refusals, all `live:schwab_1m_v2`, across 20 ET days from 07-01.** Four storm days:
**07-13 AGEN 127 · 07-31 FCUV/KUST 126 · 08-04 AAOG 115 · 09-03 CHPT 205.**
⚠️ That shows the **refusal class** is old. It does **not** show the earlier storms share the 3600
mechanism — CONF1 did not exist then.

**Reopens on a second instance with evidence.** Start at the unexamined `>= 2` working-leg threshold
in `fetch_armed_native_oco_symbols`, not at a rewrite. Board row **OVSD1**.

# ⛔ RATE1 DROPPED BY DECISION — an accepted risk, not an unnoticed gap

The broker-reject-rate alarm was built and validated (12 retained days, 10-minute buckets, firing on
**exactly one** bucket — 09-03 `live:schwab_1m_v2` 137 — zero false positives) and then **dropped**:
the operator already sees a large volume of routine refusals daily, and *an alarm he learns to
ignore is worse than no alarm*. The objection is to the **channel**, not the detection.
⇒ **With no alarm, a recurrence is detected only by the operator noticing, as on 09-03.** Four
storm-scale instances since July make a fifth likely. Thresholds and the revisit bar are preserved
in the board row; the code was not merged.

---

# ▶ NEXT SESSION

1. **`codex-2` to clear (or refuse) the probe's write sequence.** Until then it does not run.
   If cleared: PLUG, place 15:50, cancel 16:01. **Friday 09-04 is the last window before Tuesday**
   (09-07 is Labor Day).
2. **CONF1 is ON and unproven at scale** — its reconcile has one day of history, all one symbol.
3. **EOD1601 stays OFF** until the probe answers, or the operator rules otherwise.

# ⚠️ TWO VERIFICATION FAILURES OF MINE, RECORDED SO THEY ARE NOT REPEATED

1. **I compared `-k`-FILTERED test runs and reported "identical failure sets"** while a module-global
   patch in my own test leaked and broke **deselected** modules (full suite 46 → 59). A filtered
   control cannot see a leak into what it filtered out. ⇒ **The control must cover the population
   the change can reach.**
2. **A `perl` mutation replaced the FIRST of eight identical guard lines**, not the one under test,
   so a covered guard read as **uncovered**. ⇒ **Assert the mutation landed where you think it did.**

⚠️ Across three review rounds, `codex-2` withheld the pin on **nine** findings and every one was
real. The recurring shape: **an absence read as evidence** — no working legs, no refusal, no unknown
status, no failures in the filtered set.

# ⚠️ Watch items live here, not in [`handoff-open-items.md`](handoff-open-items.md)

- **`bar_gap_watch_cron.sh` exits 0 by ET-GUARD SKIP after 16:00.** A clean exit from it in the
  evening is **not** a pass. Confirm bar continuity by direct read.
- **`broker_order_events` stores our own aborts as rejects** — every reject count is contaminated
  unless keyed on the broker's verbatim reason string.
