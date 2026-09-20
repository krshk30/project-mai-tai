# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-19 (Sat) 20:30 ET.** Batch `2026-09-19-webull-exit-seam-fixes-two-deploys-pa1-live`
(covers Fri 09-18 and Sat 09-19). Integrator for this rotation; `codex-2` authored every code PR, executed every merge and
both deploys, and reviews this PR. The author never reviews.

> ⛔⭐⭐⭐ **OPERATOR'S LINE IN THE SAND (09-18).** *"We are just putting a band-aid on each hole."* A bug is not fixed when
> its test passes. Every bug ⇒ sweep the REAL history for every member of its class, fix the class, and review what the PR
> did **not** write. Every exit decision must END in a named safe state — **SOLD, or PROTECTED-AGAIN + PAGED** — including for a
> broker answer we have never seen. No freezes: the design is right, the build was wrong — fix forward. `claude-1` leads the
> exit-seam effort (authors the unowned items, `codex-2` reviews those; `codex-2` keeps what it had in flight, `claude-1` reviews).

---

# ✅ PRODUCTION — main and box IN SYNC at `d6a6f59a` (this handoff PR is docs-only: no sync, no restart)

| | |
|---|---|
| box (deployed) | **`d6a6f59a88dfe9d5cdf9f1746a18b8dcf76b732b`** — read FROM THE BOX by `claude-1` 2026-09-19 19:57:48 ET, clean |
| GitHub main | **`d6a6f59a`** at write time. Open PR besides this one: **#1013** (offline Momentum baseline control; pinned @ `4e4f1d0e` on an older base — needs a rebase + re-pin) |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-21` · `EXPECTED_SHA=d6a6f59a…` · `EXPECTED_PID=2549985` · `EXPECTED_START='Thu 2026-09-17 20:14:30 UTC'` · `SNAPSHOT=…/v2-before-corrective-20260917T201400Z.json` · `--restarted schwab-1m-v2 --restarted oms --restarted strategy` · `--expected-alembic-head 20260916_0021` · `--no-schema-change` (read 19:57 ET) |
| restart evidence | The gate's OWN command run read-only by `claude-1` 19:58 ET (output to `/tmp`): **rc 0, PASS 9/9**; bar-continuity = gaps spanning restart 1/162 → **`NO_TRADES_IN_GAP`** (MNOV 09-17 16:15–16:18 ET = 0,0,0,0). ⚠ `…/v2-restart-evidence-20260921.md` on disk is the 09-19 **morning** file (8/9 FAIL, pre-#1016) — Monday's gate overwrites it; do not quote it |
| exposure | 19:57 ET: **open managed rows 0 · non-zero account-position rows 0 · both live accounts flat** |
| flags | From `/proc/<oms pid>/environ`: **`MAI_TAI_OMS_V2_WEBULL_MIRROR_DEFERRED_RESUBMIT_ENABLED=true`** (operator 09-19: *"yes turn it on for monday.. i will go with your recommendation"*; env file line 222, the only new line) · `…WEBULL_LATE_CLOSE_GUARD_ENABLED` **ABSENT** (default false) · **#1014 has NO flag — live since 09-19 08:10:23 ET** · quantity unchanged |
| merges 09-18/19 (**11**) | **#1005** LC1 late-close guard (flag off) · **#1006**+**#1009** PA1 deferred mirror resubmit · **#1007** Momentum 1008 cool-off · **#1008** Step 2 protocol · **#1010** deploy gate (cannot run yet — see open items) · **#1011** Step 2 tooling (unwired) · **#1012** PA1b pre-check · **#1014** confirmation-exit invariant · **#1015** preflight: paper observer = warning · **#1016** restart-evidence gap check |
| migration | `alembic_version = 20260916_0021`, unchanged |

## Restarts (all `NRestarts=0`, 0 tracebacks after start — verified on the box)

| service | pid | started (ET) | why |
|---|---|---|---|
| **oms** | **3107580** | Sat 09-19 19:51:59 | deploy `d6a6f59a` (#1015 #1016 #1012) + PA1 flag. Earlier same day 08:10:23 → pid 3014661 for `c6e259a0` (#1005 #1006 #1009 #1014) |
| **strategy** | **3107596** | Sat 09-19 19:51:59 | OMS deploy restarts strategy (both times) |
| **momentum-paper** | **2704889** | Fri 09-18 07:27:53 | #1007, intraday, paper unit only, on operator yes |
| schwab-1m-v2 2549985 · control 2273848 · reconciler 1626620 · market-data 2202865 · market-capture 2202817 · orb 2051823 | — | unchanged | — |

## What is LIVE and what to READ Monday 09-21 (owner · first read) — report the numbers to the operator UNPROMPTED

| change | first evidence to read | owner |
|---|---|---|
| **#1014 Webull confirmation exit ends SOLD / resolved_by_fill / REPROTECTED+PAGED** (claim before first await; cancel-AND-read release with 0.5 s + 1.0 s retries; class-D one corrected retry) | `EXITDONE1` fired vs sold/reprotected **per broker** (09-18 tape: live:orb fired 4, sold 1, gap 3; schwab 2/2) · every `inline_seconds=` (retries still run INLINE on the serial tick consumer) · any `[OMS-V2-CONFIRMATION-EXIT-PAGE]` / `…-CORRECTED-RETRY]` | claude-1 |
| **PA1 + PA1b (flag ON): refused Webull resting mirror is re-sent inside 8%; a mirror > 8% from the market is deferred before submit; cap 3, 5 s re-arm, serial lane only** | from 07:00 ET: `[OMS-WEBULL-MIRROR-DEFERRED]` by decision · `ORDER_RISK_RULE_PRICE_AGGRESSIVE` rejects (baseline 41 in 4 sessions; 11 on IMCC alone 09-18) · segments where the primary filled with no Webull order (baseline 2: MEDS 09-16, IMCC 09-18) · Webull orders per `fanout_slot_id`. **KILL SWITCH:** a duplicate leg on one slot, or a resubmit after a v2 cancel ⇒ remove env line 222, flat-check, restart oms, tell the operator at once. Zero deferrals = UNEXERCISED | claude-1 |
| **#1007 Momentum cool-off** (5×1008 ⇒ 15 min ⇒ ONE probe) | 04:00 ET: 5 kicks then cool-off; gateway `1008 (policy violation)` count per probe (09-18: 290 → 292 over 8 probes). Bot receives **no prints** until Step 3 — a quiet Momentum page is expected, not a pass | codex-2 |
| **#1015 / #1016** | first weekday-evening deploy: momentum-paper printed as `NON-BLOCKING paper observer`; any restart-spanning gap graded against Massive 1-min aggregates (refused 09:30–16:00 ET) | codex-2 |
| **#1005 LC1** | merged, deployed, **flag OFF** — nothing to read until the operator enables it (not the same day as PA1) | operator → codex-2 |

## What happened (one paragraph each — the narrative is in `handoff-log.md`)

- **Fri 09-18, three Webull false-flip exits failed the same way:** pair cancelled (`confirmed=2`), a second task cancelled it
  again, `ORDER_CAN_NOT_BE_CANCEL` was scored "unconfirmed", the close was REFUSED and forgotten. GIPR 1.20→1.1001 (**−8.3%**,
  Schwab −1.3%), GIPR 1.14→0.93 (**−18.4%**, Schwab −2.2%; the software hard stop filled 11% below its level), IMCC 5.53→5.0201
  (**−9.2%**). The next real flip was also missed because the slot is not reset while the Webull close is pending.
- **IMCC 12:04 ET flip not traded:** Schwab restricted (13 refusals), Webull refused the 5.78 rest at 11:45 as PRICE_AGGRESSIVE
  (+9.9% over 5.26), the level stayed flat 19 min so nothing re-mirrored, v2 logged a "LIVE mirror" cross with nothing resting.
- **Momentum saw zero prints:** its own `T.*` socket on the shared Massive key was kicked 1,325× and kicked the live gateway 222×.
  Limit is one websocket per ACCOUNT per cluster. Operator ruling: **Option A — one connection, through the gateway, tick by tick.**
- **Two deploys Sat 09-19** by the standard watched procedure; the new #1010 gate could not run (box predated it), and the first
  attempt Fri evening was refused twice by the health preflight because momentum-paper's truthful `degraded` heartbeat blocked it.

## Open items — each with an OWNER and a NEXT ACTION (nothing boards without both)

| item | owner | next action |
|---|---|---|
| **Every other Webull software exit still uses the old one-try release** (CW_HARD_STOP 58 rejected / 6 filled, CW_FLOOR 17 rejected + 17 cancelled / 10 filled, 09-04→09-18) and the confirmation retries run inline on the tick consumer | **claude-1** (claim on `oms/service.py`) | one repaired take-back-and-sell routine for hard stop / floor / flip / overnight flatten, retries off the tick path; codex-2 reviews; not deployed without a live-tape fixture per class A–E |
| **Hard stop is a market sell AFTER the level trades** (GIPR −18.4%) | claude-1 | `docs/review-artifacts/webull-exit-seam/HARD_STOP_DESIGN.md` — how −8% is held at the BROKER for a released / never-protected leg |
| **WEBULL-PROTECT-FAILED 3 of 75** (IMRN 09-04, QCLS 09-16, DLXY 09-16 — held with no broker stop) | claude-1 | `PROTECT_FAILED_FORENSIC.md`: the 5 attach answers each, then the same terminal rule |
| **Webull API pressure** (~1,261 `TOO_MANY_REQUESTS`, 416 status-fetch failures in 10 sessions) | claude-1 | `WEBULL_API_BUDGET.md`: calls/min by endpoint vs where the 429s land; no fix before the table |
| Re-attach of a FULL pair while an old leg may still work is **ASSUMED** to be refused by Webull's share reservation | codex-2 | capture the real response the first time the tape shows it; add the fixture |
| #1010 deploy gate cannot run on a box that predates it | codex-2 | bootstrap PR: tools dir outside the repo, sha256-verified, exec bits committed, first-install path stated |
| LC1 flag (late-close pacing) | operator → codex-2 | operator decides a day AFTER PA1 has been read; never both the same day |
| Momentum Step 2 measurement (tooling #1011 pinned; flat files reachable, 09-17 file 2.97 GB) | codex-2 | population count may run detached on a weekend; the replay needs a **FLAT 16:05–20:00 ET weekday window** (no quote ticks after 20:00 ⇒ criterion 3 UNMEASURED). Step 3 stays blocked |
| #1003 baseline report | codex-2 | #1013 (order-aware control, 60/60 exact, 1,296 same-ms extras explained) needs rebase + re-pin; the report stays unread until then |
| Small test pins | codex-2 | LATECLOSE1 `<= 3` threshold · Momentum one-probe + connected-during-streak heartbeat · gateway needle `1008 (policy violation)` · behavioural v2 claim-expiry test · gate-level main-moved refusal · #1016 zero-transaction row |
| Schwab-ineligible cache no longer written after #992 (KXIN 5/5, TURB 25/25, IMCC 13) | none — **parked by the operator 09-18** | costs rejects only; reopen on request |
| Schwab refuses to open **37 of 39** Webull-only opportunities (09-04→09-18) | operator | design question raised 09-18: keep those names at 1 share on Webull until `EXITDONE1` is clean for 5 sessions? Not yet answered |
| Carried unchanged from 09-17: late software close after a broker fill (now LC1) · `abandoned_no_fresh_quote` (14/86 on 09-17, 0 trades lost, age not logged) · seed-cap fires on every symbol ADD (26 since #993, 2 in-window, n=1 saved a loser) · G5 parity · ZTG ASK_PAST_BAND · 07:00–07:08 blind window · hand-cancel of one leg | as on 09-17 | none moved |

## Rulings and approvals (operator, 09-18 / 09-19)

1. Fix **Webull "can't sell short"** and **PRICE_AGGRESSIVE**; every other 09-10..09-17 census row accepted as-is.
2. **Do not stop, disable or remove momentum-paper** — it is the proving ground; **Option A** architecture; no extra Massive connection.
3. **No freeze — fix forward.** Unknown broker answer ⇒ retry → re-protect → page (standing rule for every exit).
4. **`claude-1` is the lead agent** on the exit seam; `codex-2` keeps in-flight PRs.
5. **Preflight policy:** a service declaring `execution_mode=paper` AND `broker_route=none` is a warning, not a block (#1015).
6. **GO `c6e259a0`** (Sat 08:10 ET) and **"GO d6a6f59a"** (Sat ~19:50 ET). **PA1 flag ON for Monday**; LC1 stays off.

## Pre-open 09-21 (`preopen.sh` pins set by `codex-2` 09-19 ~19:55 ET, read by `claude-1` 19:57 ET)

Run the gate and the grades as usual. Expect `EXPECTED_SHA=d6a6f59a`, v2 pid `2549985`, the 09-17 corrective snapshot, restarted
list v2 + oms + strategy, evidence **9/9** with the MNOV `NO_TRADES_IN_GAP` row. ⛔ All seven pins move on every restart —
an unattended-upgrade restart over the weekend would stale them; the gate will say so. 03:55–04:00 ET: momentum-paper takes five
1008s and cools off — the fix working. From 07:00 ET `claude-1` watches PA1 live.
