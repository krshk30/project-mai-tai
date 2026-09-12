# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-12 16:55 ET.** Weekend batch `2026-09-12-known-defect-watch`.
`codex-2` integrated and merged; the author never reviews or merges their own work.

> ⛔⭐⭐⭐ **RECLAIM1 IS LIVE, NOT DARK.** The previous version of this file said
> `flip_owned_first_entry_enabled` was *absent and defaulted `False`*. **That is wrong as of today.**
> Read from the running v2 process (pid `1089323`) on 2026-09-12 20:55 UTC:
> `MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true`.
> This implements the operator's standing ruling — *one trade per ATR segment, the first resting
> entry only, no reclaim anywhere in the system.* Do not describe it as dark again.

---

# ✅ PRODUCTION — main and box IN SYNC at `5dae7c9a`

| | |
|---|---|
| box (deployed) | **`5dae7c9a`** — read FROM THE BOX 2026-09-12 16:42 ET, branch `main`, checkout clean (0 files) |
| GitHub main | **`5dae7c9a`** at the time of writing |
| ⛔ **box pin** | **The box must STAY at `5dae7c9a` until Monday's pre-open gate passes.** The gate pins `EXPECTED_SHA=5dae7c9a`; a docs-only merge (this PR included) moves *main* ahead, which is fine, but **do not `git pull` on the box before the gate is green.** Sync the box after Monday pre-open, not before |
| open PRs | **none** (this handoff PR excepted) |
| exposure | **16:42 ET:** both live accounts flat — open managed rows **0**, nonzero `account_positions` rows **0**, both accounts resolved |
| merges 09-12 | **#951** known-defect regression watch · **#964** SLOTCLEAR1/LIQPULL1 rows + DB-evidence hardening · **#965** liquidity threshold parity. All three independently reviewed and pinned by `claude-1` |
| deploy 09-12 | **one window, 16:34:27 ET** (20:34:27 UTC). **v2 only** was restarted; oms, strategy, ORB, reconciler, market-data, market-capture and control were **not** touched |
| ⚠ why a restart was needed | #964 adds `resting_below_floor_bars=` to the two resting-cancel log lines. Without it the **LIQPULL1** watch row cannot read the streak and pages `COULD_NOT_TELL` on every liquidity cancel |

| service | pid | | service | pid |
|---|---|---|---|---|
| **schwab-1m-v2** | **1089323** (restarted 20:34:27 UTC, `NRestarts=0`) | | oms | 555216 |
| strategy | 555227 | | market-data | 2202865 |
| market-capture | 2202817 | | reconciler | 2202771 |
| orb (paper) | 609358 | | control | 311547 |

Zero post-restart tracebacks (0/249 timestamped records). No migrations ran; alembic head
`20260910_0020` unchanged.

---

# FLAGS — read from the RUNNING processes, not the env file (2026-09-12 20:55 UTC)

| flag | value | note |
|---|---|---|
| `FLIP_OWNED_FIRST_ENTRY_ENABLED` | **`true`** | ⛔ **RECLAIM1 LIVE.** Was `absent/False` on 09-09 |
| `CW_V2_RECLAIM_ENABLED` | `false` | no reclaim in the system, per the operator's ruling |
| `CONFIRMATION_ACCOUNT_NEUTRAL_DISCOVERY_ENABLED` | `true` | #945 |
| `ATR_FLIP_PROBE_SYMBOLS` | `*` | ⭐ **fleet-wide, and load-bearing** — SLOTCLEAR1 has no denominator without it |
| `MACD_PROBE_SYMBOLS` | `*` | ⭐ same; five flags are required at pre-open, not four |
| `ATR_FLIP_VOL_FLOOR` | `10000` | |
| `CW_V2_RECLAIM_GAP_BARS` | `1` | |
| `OMS_V2_CW_TARGET_PCT` / `HARD_STOP_PCT` | `5.0` / `8.0` | read from the **oms** process |
| `OMS_V2_RTH_EDGE_BRACKET_ENABLED` | `false` | #647 Gate 1 still not waived |

---

# 🔔 KNOWN-DEFECT REGRESSION WATCH — INSTALLED 2026-09-12 20:35 UTC

One dedicated `/etc/cron.d/project-mai-tai-known-defect-regression-watch`, `root:root 0644`,
sha256-identical to the reviewed source, **shared crontab untouched**. Self-test delivered and
**confirmed received on the operator's phone**.

```
*/5 * * * * root /home/trader/project-mai-tai/ops/health/known_defect_regression_watch_cron.sh
```

⛔ **The cron runs every 5 minutes but the wrapper only evaluates 03:50–20:15 ET on weekdays**
(`CRON_TZ` is ignored on this box, so the guard lives inside the wrapper). `STATUS.txt` will sit
frozen all weekend at the 20:36 UTC dry run. **That is correct, not a fault.**

**Dry run: 19/19 rows** — 1 `OBSERVED_CLEAN`, 9 `UNEXERCISED`, 5 `UNARMED`, 4 `DELEGATED`; zero
`RECURRENCE`, zero `COULD_NOT_TELL`. The only row with a real denominator is `W4291`
(3,020 Webull position reads, 0 backoff markers). ⛔ **`UNARMED` and `DELEGATED` are inventory
states, never passes.**

---

# ⭐⭐ MONDAY 2026-09-14 PRE-OPEN — the corrected list

⛔ `codex-2`'s fail-closed gate **supersedes** `claude-1`'s original helper, which is preserved at
`/home/trader/preopen.sh.claude-20260912`. Four defects in the original, all verified against the
code before acceptance:

1. **Four flags, not five** — `MACD_PROBE_SYMBOLS` was omitted.
2. **Managed rows only** — it never checked `account_positions`, so a broker position with no
   managed row read as flat.
3. ⛔ **Restore-before-release ordering unproven** — `tail -1 | grep released` would have PASSED a
   release that *preceded* restoration, which is precisely the `BOOT1` defect. The replacement
   requires `row[0] >= completion_stamp`.
4. **Fail-open error suppression** — a failed query printed nothing and was read as flat. The
   replacement also uses `2>/dev/null`, but value-tests every result, so an error becomes an empty
   string that **fails** the check. Same construct, opposite polarity.

## The steps

1. **~06:30 ET:** `ssh mai-tai-vps /home/trader/preopen.sh`
2. Require the literal final line **`PASS: Monday pre-open gate is green.`** Anything else is
   `BLOCKED`. **Exit 1 is the gate working.**
3. It verifies in one pass: run window before 07:00 · SHA `5dae7c9a` and clean tree · v2 identity
   pid `1089323`, `NRestarts=0`, exact start timestamp · installed watch hash/mode/schedule ·
   restarted vs untouched service pids · **both accounts flat, managed rows AND broker positions** ·
   alembic head with no schema change · **five** running-process flags · REST warmup population ·
   **ordered** BOOT-HOLD release · bar continuity across the restart · tracebacks · fresh 19-row
   watch status under 420s with zero recurrence and zero blind readings.
   ⭐ **Only `gaps spanning restart = 0` is required.** ⛔ The bracketing check will STILL read
   `N/A_OFF_SESSION` on Monday and that is correct — it grades the **Saturday** restart, which
   happened outside a trading session, so no adjacent bar pair can ever bracket it. Do not
   wait for it to turn green and do not treat `N/A_OFF_SESSION` as a failure.
4. If blocked, **rerun after the scanner watchlist arrives.** ⛔ Do not manually bypass the hold.
   If still blocked at 07:00, **v2 is not entry-ready** — that is the honest outcome, not a
   formality to clear.

⛔ **The Saturday restart did NOT pre-drain the boot hold.** The hold releases only on restoration
completing against a **non-empty evaluated population**; a weekend has no scanner watchlist
(`scanner_evaluated=0 reason=empty_evaluated_population_after_exclusions`). It re-HELDs every ~60s
all weekend and releases Monday exactly as it would have after a Monday restart. **09-11's took
18.8 minutes.** [[a gate whose release depends on DATA cannot be pre-satisfied by running earlier]]

⚠ **Two properties of the gate.** It pins `EXPECTED_PID` and the exact start timestamp, so a weekend
reboot (unattended upgrades have restarted this fleet before) fails it loudly rather than passing on
a different process — re-verify, never relax the pin. And `EXPECTED_DATE=2026-09-14` makes it a
**one-day** gate; it refuses to run Tuesday and is not a daily tool.

---

# ⛔⭐⭐ WHAT MONDAY CAN AND CANNOT PROVE — event-specific, no day-level verdict

| fix | what it needs to be exercised | status |
|---|---|---|
| **#955** fresh-SELL latch clear | a seed-capped symbol takes a fresh SELL **and then places** | replay-proven only; first live money |
| **#960** 3-bar volume pull | the thin-volume sequence | replay-proven only |
| **#945** account-neutral confirmation discovery | **a Webull-only fill** — has never once occurred | deployed, unexercised |
| **#946 / #947** | their **own** OCO and false-flip conditions | ⛔ unproven; a Webull-only fill does **not** validate them |
| **SLOTCLEAR1 / LIQPULL1** | a fresh SELL; a liquidity cancel | never seen live tape — Saturday proved they *execute*, not that they *catch* |

⛔ **Grade the first qualifying entry as a single event, not the day.**

---

# 🔔 ALERT INTERPRETATION

The watch pages **once per episode**; failed delivery retries next run and a cleared episode
re-arms. ⇒ **Repeated pages on the same row _without an intervening clear_ mean delivery or state
trouble** — check `state.json` before treating it as a defect. ⭐ But if the condition clears,
re-arms, and then genuinely recurs, **a second page is correct** and is a real defect, not noise. Any `COULD_NOT_TELL` is a **lost evidence
source**: not a pass, not noise, wants a look the same day.

⭐ Separately, the operator's standing tolerance is **~10 alerts/day across ALL fleet alerting**
(stated 09-11 against a 34-alert day). ⛔ **This watch's own clean-day target is ZERO**, not a share
of the ten. Any page from it is an event; if it starts contributing volume, that **is** the signal.

---

# 🔔 GATE 1 WATCHER IS LIVE — installed 2026-09-09, still armed and unexercised

`#931`, installed 19:07 ET. **Exactly one** cron entry, verified:

```
*/5 13-21 * * 1-5 /home/trader/project-mai-tai/ops/health/gate1_shape_watch_cron.sh   # GATE1 qualifying-shape watch
```

Wrapper committed `100755`. Selftest logged `ALERT[NONE] DELIVERED` and — correctly — wrote **no
state file**, which is the behaviour `test_a_selftest_does_not_write_state` pins.

⛔ **THE ACTIONABLE WINDOW STARTS 09:30 ET, NOT 09:00.** The cron fires from 13:00 UTC (09:00 ET),
but the wrapper's own guard is `ETMIN -lt 570` — it **exits immediately** until 09:30 and after
16:00. That is correct (the qualifying shape needs an RTH session), but do not sit waiting for a
page at 09:00: the first run that can page is 09:30 ET.

⛔ **It pages the moment a qualifying shape exists: a v2-held SCHWAB long, entered pre-market, still
held in regular hours, with NO shares reserved by an open exit.** An ordinary RTH entry can never
qualify (it is bracketed at the fill), which is why SUNE could not be used. **The window is open only
while that position is.** Take the `previewOrder` by hand while it is live — ⛔ preview, never a live
probe. That is the only thing standing between us and enabling `oms_v2_rth_edge_bracket_enabled`.

⚠ The ET window, weekday and holiday guards live **inside the wrapper**; `CRON_TZ` is ignored on this
box, so the `13-21` UTC hour range only trims idle runs and is not the authority.

---

---

# ✅ F1 — FIXED BY #930 AND DEPLOYED, BUT NEVER EXERCISED

⛔ **The previous version of this file reopened F1 as a live defect. That was wrong, and `codex-2`
caught it in review of #966.** #930 (*F1/F2: make ATR SELL observations account-neutral*, merged
2026-09-09 16:02 UTC) replaced the strategy-owned, Schwab-sized `v2_cw_flip` close draft with a
single account-neutral `v2_atr_sell_observation`. OMS evaluates **every** configured v2 account
independently and binds only the managed row open when the stamped bar closed.

Verified on the box 2026-09-12:

| check | result |
|---|---|
| `v2_atr_sell_observation` handled in `oms/service.py` | present (line 1113) |
| old `emit_cw_flip` / `_maybe_cw_flip_close` in the strategy | **0 occurrences** — removed, not left dormant |
| times it has fired on live tape | **0** |

⇒ **Status is DEPLOYED and UNEXERCISED, not open.** The YMAT 09-09 proof (`pos_qty=0`, flip SELL,
no exit) describes the **pre-fix** behaviour and must not be quoted as current.

⚠ Note the distinction that made this easy to get wrong: `_fetch_position_maps` is **still**
scoped to the v2 account, and that is deliberate — it is the ENTRY-decision signal, and its own
docstring says cross-venue fan-out intents must not enter it. The EXIT path is what #930 made
account-neutral. Entry-scoped and exit-neutral are both correct at the same time.

# ⛔ CONFIRMATION-EXIT RACE — open, live, unowned

SUNE 2026-09-09, one protection base `schwab_1m_v2-SUNE-protect-1b46c61c5453`:

```
12:52:13.094  WEBULL-RELEASED  requested=2 confirmed=2
12:52:13.355  WEBULL-REFUSED   reason=pair_cancel_unconfirmed reports=2 confirmed=0
              accounts=live:schwab_1m_v2:closed,live:orb:refused
```

Schwab closed; Webull did not; the share sat until the 13:25 ATR exit. The OMS releases
`_confirmation_exit_inflight` **immediately after the protection-cancel call**, before the close
reaches a terminal result, so a second quote evaluation re-enters during the
released-but-not-yet-closed interval. **A control should replay those two overlapping evaluations
and prove one protection cancellation and one Webull close attempt.**

---

---

# ⭐⭐ STATUS SPLIT — ANSWERED vs UNEXERCISED. Do not collapse these.

| ✅ ANSWERED **and CLOSED** | ⛔ DEPLOYED and **UNEXERCISED** |
|---|---|
| **Webull one-sided entries** — measured; zero venue rejections; cause is our slot | **#955 / #960** — replay-proven only; **never run on live money** (see the Monday table above) |
| **DRIFT1** — false-clean fixed and now scheduled | **GATE1 watcher** — armed; no qualifying shape has ever existed |
| **HALT-FILTER / HALT-DETECTOR** — session-boundary artefact fixed at source | **INC1** — installed; synthetic incident opened and closed, no real one yet |
| **F1 mechanism** — root cause proven on YMAT **and FIXED by #930** | **#930 account-neutral ATR exit** — deployed; has fired **0** times on live tape |
| **#933 controls** — all five admission branches + the reactive guard mutation-red | **CONF3 / SIL1 / released-leg recovery / HDL1** — still never fired |

⛔ **UNKNOWN is not PASS.** Read `STATUS.txt`; do not infer health from the absence of a page.

---

---

# ▶ CARRIED — open, read-only, nobody is blocked on them

| item | question | owner |
|---|---|---|
| ~~**F1/F2 one-sided lifecycle**~~ | ✅ **CLOSED by #930**, deployed, never exercised. Was wrongly reopened in the 09-12 draft of this file | — |
| **confirmation-exit race** | per-account in-flight ownership held to terminal close | unassigned |
| **STOPMKT probe** | operator deferred: does a stop-market entry change the fill? | deferred by operator |
| **PROV1** | arming on flips reconstructed from db-seed (649 occurrences) | unassigned |
| **liquidity floor** | ⭐ the *cancel* half is addressed by **#960** (pull only after 3 consecutive sub-floor bars, one good bar re-places) and watched by **LIQPULL1** — replay-proven, unexercised live. The floor VALUE (10,000) and the arm-time staleness remain open | unassigned |
| **0.5% stop-limit band** | strands the entry on fast moves (YMAT flip bar) | unassigned |
| **F6 / F8 / F9 / F10** | stale `current_profit_pct`; Webull manual-order ingestion | unassigned |
| **BNC 11:03 · claim overhang · fan-out size · ELIG · AMEND1 · REJ1 silence** | carried from 09-08, unchanged | unassigned |

⭐ **`drift audit` is CLOSED** — #924 scheduled it; it is no longer unscheduled.

---

# ⚠️ VERIFICATION FAILURES OF MINE, RECORDED SO THEY ARE NOT REPEATED

1. **I claimed a Saturday restart removes the Monday boot-hold risk.** Wrong — `codex-2` caught it.
   The hold is gated on data, not uptime. I read the clock instead of the release condition.
2. **I wrote a pre-open helper with four fail-open defects** (above), including a boot-hold check
   that could not come out false in the one direction that matters.
3. **I bundled #945/#946/#947 under one trigger.** Absence of a Webull-only fill is not evidence
   about #946 or #947.
4. **A controlled pair reported `new_failures=0` from worktrees whose Python had no pytest.** A void
   harness, not a clean result. The venv `.pth` also hardcodes the *main* checkout's `src`, so a
   worktree run without `PYTHONPATH` silently compares main against main. **Assert the imported
   path before trusting any count.**
5. **`no tests ran in 0.00s` read as five clean mutation results.** zsh does not word-split an
   unquoted variable, so two test paths became one bogus argument.
