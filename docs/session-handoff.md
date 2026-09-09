# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

> ⛔⭐⭐⭐ **THE 2026-09-06 PAPER-ONLY RULE IS REVERSED. Reversed by the operator, 2026-09-08.**
> **Live `schwab_1m_v2` now runs `+5%` target · `−8%` hard stop · reclaim OFF · one entry per ATR
> segment.** These are **no longer paper-bot settings** — they are installed in
> `/etc/project-mai-tai/project-mai-tai.env` and **verified present in both running processes**
> (see PRODUCTION below). The banner that stood here until today said the opposite; it was correct
> when written and is now superseded. The 09-06 correction at
> `corrections/pr-905-paper-not-live-settings.md` on `review-pins` remains valid **as history** —
> it records that nothing was ever gated on CONF3, which is still true.
>
> ⚠ **Reclaim OFF is two changes, not one.** `schwab_1m_v2.py:591` reads
> `_cw_v2_max_entries_per_flip = 2 if reclaim_enabled else 1`. Turning reclaim off **also halves the
> per-flip entry cap**. Do not describe it as "the reclaim path is inert".
>
> ⚠ **The hard stop moved −5% → −8%: maximum loss per trade is 60% larger than it was yesterday.**

**Written by `claude-1`, 2026-09-08 20:05 ET; PRODUCTION block refreshed 20:50 ET after #923.** Batch `2026-09-08-rej1-fixed-and-reclaim-retired`.
Integrator for this rotation. Needs `codex-2`'s review before merge — the author never reviews.

---

# ✅ PRODUCTION — main and box IN SYNC at `7eca22a7`

| | |
|---|---|
| box (deployed) | **`7eca22a7a1ff55372b401ef45ff38f728c4edbb9`** — read FROM THE BOX 2026-09-08 20:50 ET, branch `main`, checkout clean |
| GitHub main | **`7eca22a7`** — **identical**. Everything merged today is deployed |
| open PRs | **none** (this handoff PR excepted) |
| exposure | **20:50 ET:** live positions **0** · live working orders **0** · **0 tracebacks** in oms since the 00:46 UTC restart |
| merges 09-08 | #913 handoff · #914 unexercised watcher · #915 ORB resting · #916 SLOT2/DB2/RECOV1 harness · **#917 REJ1 P1** · #918 + #919 dashboard truth · #920 ATR flip via native OCO · **#921 replay parity** · **#923 CW-flip position binding** |
| deploys 09-08 | **(1)** merged PRs at `90f4860` — oms+strategy 21:26:30 UTC, v2 21:35:28 UTC. **(2)** #921 **plus the three runtime settings** at `c47d3a76` — oms+strategy **22:55:46 UTC**, v2 **22:58:09 UTC**. **(3)** #923 at `7eca22a7` — oms+strategy **00:46:33 UTC**; v2 deliberately NOT restarted (its only change was a docstring) |
| ⚠ restart scope | The `oms` target restarts **oms AND strategy** together. Do not plan around "oms only" |
| ⛔ ORB | **untouched since 2026-09-04 21:52 UTC.** `#915` is merged and **DARK** — see below |

| service | pid | started (UTC) | | service | pid |
|---|---|---|---|---|---|
| oms | **4090993** | **00:46:33 (09-09)** | | market-data | 2202865 |
| strategy | **4091004** | **00:46:33 (09-09)** | | market-capture | 2202817 |
| schwab-1m-v2 | **4069310** | 22:58:09 | | reconciler | 2202771 |
| control | 4001261 | 18:57:00 | | orb | 3110306 (09-04) |

⛔ Unit names are `project-mai-tai-<svc>.service`, **not** `mai-tai-<svc>`. A `systemctl show
mai-tai-oms` returns `MainPID=0` and reads exactly like a dead service. It is not one.

---

# FLAGS — read from the RUNNING processes, not the env file

| flag | state | note |
|---|---|---|
| `..._CW_V2_RECLAIM_ENABLED` | **`false`** ⭐ NEW | also caps the segment at ONE entry |
| `OMS_V2_CW_TARGET_PCT` | **`5.0`** ⭐ NEW | first time this has ever been set explicitly |
| `OMS_V2_CW_HARD_STOP_PCT` | **`8.0`** ⭐ NEW | ⚠ 60% wider than yesterday |
| `..._CONFIRMATION_EXIT_ENABLED` (CONF1) | `true` | ON since 09-03 |
| `..._DUAL_BROKER_FANOUT_ENABLED` | `true` | `..._WEBULL_FANOUT_QUANTITY=1` |
| `..._WEBULL_MIRROR_ENABLED` / `_EH_` / `_RESTING_` | `true` | |
| `OMS_V2_EH_ENTRY_ENABLED` | `true` | |
| `..._ATR_FLIP_PROBE_SYMBOLS` | `*` | present in v2 pid 4069310 — re-confirmed after tonight's restart |

⛔ **`settings.py` code defaults are still `2.0` / `5.0`** (guarded by `test_exit_logic_cw.py:66-67`).
⇒ **The env file alone holds live at 5/8.** An env rebuild silently reverts live to +2%/−5% while
every backtest keeps modelling 5/8 — and `audit_live_locked_drift.py` files that in its `unset`
bucket, which is its quietest, **not** as drift. Treat those two lines as load-bearing.

---

# ⭐⭐ TODAY IN ONE LINE

**A P1 that blocked live closes was found from the operator's own P&L screens — because the guard
never logs and the happy-path fixture had never modelled a real wrapper — fixed and deployed the
same day; the Completed Positions table was made truthful; and the paper-only settings rule was
reversed and deployed to live.**

---

# ⛔ REJ1 (#917) — the class is fixed, the SILENCE is not

`release_native_oco_for_close` refused to release a native OCO whose wrapper carried no
`orderLegCollection`. **A wrapper whose children are visible is not opaque.** Fixed and deployed.

⛔ **Four distinct causes set `unsafe`, and NONE of them logs.** The opaque-node guard; a SELL child
`ACCEPTED` with no `orderId`; a SELL child in `PARTIAL_FILL`; a SELL child with an unrecognised
status. ⇒ **The next refusal in this class will also have to be found from a P&L screen.** Adding a
reason line at the four sites is small, unclaimed, and the highest-value follow-up on the board.

---

# ⛔ #915 IS MERGED AND DARK — no operator decision exists

It flips ORB to the fixed opening-high resting model **the moment anyone rebuilds the ORB env and
restarts ORB**. ORB has not been restarted since **2026-09-04 21:52 UTC**, so the change is inert
today and will activate silently on the next unrelated ORB restart.
⇒ **Decide it deliberately or leave ORB alone.** Do not let a routine restart make the decision.

---

# ⭐⭐ STATUS SPLIT — ANSWERED vs UNEXERCISED. Do not collapse these.

| ✅ ANSWERED **and CLOSED** | ⛔ DEPLOYED and **UNEXERCISED** |
|---|---|
| **REJ1** — cause found, fixed, deployed, live impact same day | **CONF3** — fan-out running; the live two-broker close has still never fired |
| **Dashboard truth** — `PHANTOM rows: 0` verified on the rendered page | **SIL1** — alarm running; no episode has reached 8. `live:orb` deliberately UNSET/uncovered |
| **MOBX** — not a broker decline; our own malformed buy-stop | **Released-leg recovery** — still never fired |
| **Reclaim P&L** — indistinguishable from first entries (see below) | **HDL1** — bounded retry + incident; neither has fired |
| **Replay parity** — LIVE_LOCKED now mirrors live on all three keys | **#914's four conditions** — the watcher exists so these stop being invisible |
| **Post-deploy drift** — pre-registered 26/0/10, measured 26/0/10 | **#920's CW-flip release** — merged today, never exercised |
| **NUR 12:34→13:29** — a flip decision that outlived its position, confirmed on the box | **#923's position binding** — deployed 00:46 UTC; the live path is **UNEXERCISED** |

⭐ **13 of 15 deployed-but-unexercised items were exercised with controls today.** The two that
remain need a live session or a rare event and are covered by the `#914` watcher, which pages the
operator directly. Its cron is sha-pinned at `*/15 11-21 * * 1-5`.

---

# ⭐ RECLAIM — retired for simplicity, NOT because it lost money

Re-derived by attributable coid-prefix pairing, **live accounts only, no FIFO inference**:

| Era | Slot | Cycles | Win% | Median |
|---|---|---:|---:|---:|
| 08-27…09-01 | first | 27 | 78% | +2.00% |
| 08-27…09-01 | **reclaim** | 16 | 75% | +1.94% |
| 09-02…now | first | 32 | 69% | +1.89% |
| 09-02…now | **reclaim** | 11 | 73% | +1.92% |

⛔ **Pre-08-27 yields ZERO attributable cycles.** The older *"38% win / −4.98%"* figure — which I
repeated in the CONF3 material and in a #921 review — **has no attributable denominator in its own
era**. Do not cite it. It is withdrawn.

---

# ⛔ ONE PR MERGED WITHOUT AN INDEPENDENT PIN — #919

**`#919` merged at 18:56 UTC with `independent-review-pin` RED and ZERO records in the ledger.**
Swept every PR from #910 to #923: **12 of 13 are pinned at their exact head; #919 is the only hole.**

⛔ **It was not a silent bypass — it was an explicit operator waiver:** *"it's just a dashboard,,
can you not review and pin it then merge and deploy"*. The code (a dashboard de-duplication fix in
`trade_episodes.py`) reached production **without a second agent ever reading it**, which is the
thing the gate exists to prevent. It is recorded at `corrections/pr-919-merged-without-a-pin.md` on
`review-pins` so a later audit reads a decision rather than a gap.

⇒ **The residual risk is unreviewed dashboard code in production.** If anyone wants that closed, the
action is a post-hoc review of `5862b89c`, not a pin — the gate cannot pin a merged PR whose base
has moved.

---

# ▶ CARRIED — open, read-only, nobody is blocked on them

| item | question | owner |
|---|---|---|
| **BNC 11:03** | why did the Webull leg cancel at 11:03:11? | unassigned |
| **claim overhang** | is a claim outliving its position by ~3 minutes intended? | unassigned |
| **fan-out size** | the 2-vs-1 asymmetry between the legs | unassigned |
| **ELIG** | Schwab 128/129 rows written, Webull **1 row ever**, three unmatched strings | unassigned |
| **AMEND1** | D6 harness: `paired_legs` PASS, `duplicate_legs` PASS, `fill_rate` **FAIL** 7.4pp on 2 symbols, `refused_exits` **FAIL** 3/8 | unassigned |
| **REJ1 silence** | add a reason line at the four `unsafe` sites | unassigned |
| **drift audit** | `audit_live_locked_drift.py` is scheduled in **neither crontab** | unassigned |

---

# ⚠️ THREE VERIFICATION FAILURES OF MINE, RECORDED SO THEY ARE NOT REPEATED

1. **I verified #918 against a payload I reconstructed, not the live one.** It passed while the
   phantoms were still on the operator's screen. ⇒ *Verify against the artefact the user is looking
   at, not a model of it.*
2. **I claimed a 13-second settle-lag near-miss as the cause of the duplicates.** Wrong — the two
   sources were never comparable (ISO vs display-ET), so **both dedupe paths had always been dead**.
   ⇒ *A plausible false reason stops the hunt.*
3. **I chose #921's test population by grepping for the symbol name.** `test_daily_sheet.py` reaches
   `build_replay_settings` **transitively** and never names it, so my "complete" run missed the one
   test the change broke — and I pinned green over a red `validate`. ⇒ ⭐ **A grep for a symbol is
   not a call graph.**

Also, twice today `#914` — my own watcher — paged the operator's phone with duplicates before its
state-write ordering and its sticky `announced` flag were fixed.

---

# ▶ NEXT SESSION — Wednesday 2026-09-09

1. ⛔ **First live session on `+5%` / `−8%` / one-entry-per-segment.** Nothing about the exit
   profile from before today is a baseline for it. Judge it on its own tape.
2. ⛔ **Decide `#915`** before anything touches ORB.
3. The REJ1 reason lines — small, high value, unclaimed.
4. Watch `#914`'s four conditions. Silence is not green: read `STATUS.txt`, do not infer from the
   absence of a page.

⛔ Watch items live here, not in [`handoff-open-items.md`](handoff-open-items.md).
