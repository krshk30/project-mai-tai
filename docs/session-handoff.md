# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

> ⛔⭐⭐⭐ **RECLAIM1 (#933) IS DEPLOYED AND DELIBERATELY DARK.** It implements the operator's
> 2026-09-09 ruling — *one trade per ATR segment, the first resting entry only, reclaim no way* —
> behind `strategy_schwab_1m_v2_flip_owned_first_entry_enabled`, which **defaults `False` and is
> ABSENT from the env**, so nothing overrides the default. The running v2 emits **zero**
> `V2-FLIP-OWNER-*` markers. Merging and deploying it changed no trading behaviour.
> **Enabling it is an operator decision that has not been made.**
>
> ⚠ Live `schwab_1m_v2` therefore still runs the 09-08 profile: `+5%` target · `−8%` hard stop ·
> reclaim OFF · one entry per ATR segment (via the old `_cw_v2_max_entries_per_flip` path, **not**
> RECLAIM1). Maximum loss per trade remains 60% larger than before 09-08.

**Written by `claude-1`, 2026-09-09 19:20 ET.** Batch `2026-09-09-reclaim1-dark-gate1-live`.
Integrator for this rotation. Needs `codex-2`'s review before merge — the author never reviews.

---

# ✅ PRODUCTION — main and box IN SYNC at `7a879a22`

| | |
|---|---|
| box (deployed) | **`7a879a22d75662c8ca4b7fd7488d27b9a0c1cbc1`** — read FROM THE BOX 2026-09-09 19:10 ET, branch `main`, checkout clean |
| GitHub main | **`7a879a22`** — **identical**. Everything merged today is deployed |
| open PRs | **none** (this handoff PR excepted) |
| exposure | **19:12 ET:** virtual positions **0** · open broker orders **0** · open/pending intents **0** · **0 tracebacks** in oms since the 23:03 UTC restart |
| merges 09-09 (**10**) | #924 DRIFT1 · #925 ORB-LEFT · #926 HALT-FILTER · #927 HALT-DETECTOR · #928 INC1 · #929 STOPASK · #930 F1/F2 account-neutral · **#931 GATE1 watch** · **#933 RECLAIM1 (dark)** · #934 Webull investigation. #932 CLOSED as superseded by #933 |
| deploys 09-09 | **THREE windows, not one** — from the box's own reflog (all ET; the box clock is `Etc/UTC`). **(1) 06:54** → `fd22eb4` (#924/#925); ORB restarted 9s later at 10:54:27 UTC for `orb_app.py`. **(2) 07:27** → `72b13393` (#926); **pull only, no restart** — it is an `ops/health` script. **(3) 19:03** → `7a879a22` (the remaining six); oms+strategy **23:03:42 UTC**, v2 **23:05:54 UTC** |
| ⚠ restart scope | The `oms` target restarts **oms AND strategy** together. Do not plan around "oms only" |
| ⛔ ORB | **running, and it is the PAPER path** — `[ORB-PAPER-ENTRY] … RECORDED_NOT_A_FILL`. Restarted **06:54 ET** (10:54 UTC) for the ORB-LEFT work (#925), and again earlier at 05:22 ET (09:22 UTC) by the operator. Not live money. Any memory saying the service is disabled is stale |

| service | pid | started (UTC) | | service | pid |
|---|---|---|---|---|---|
| oms | **180952** | **23:03:42** | | market-data | 2202865 |
| strategy | **180963** | **23:03:42** | | market-capture | 2202817 |
| schwab-1m-v2 | **181816** | **23:05:54** | | reconciler | 2202771 |
| control | 4001261 | 18:57:00 (09-08) | | orb | (paper) 10:54:27 UTC = 06:54 ET |

All three restarted services: `active`, `NRestarts=0`. v2 warmed **3/3**, streamer connected, one
REST gap-fill recorded during restart. No migrations ran.

---

# FLAGS — read from the RUNNING v2 process (pid 181816), not the env file

| flag | value | note |
|---|---|---|
| `CW_V2_RECLAIM_ENABLED` | **false** | also halves `_cw_v2_max_entries_per_flip` to 1 — two changes, not one |
| `flip_owned_first_entry_enabled` | **absent → default `False`** | RECLAIM1 dark |
| `OMS_V2_CW_TARGET_PCT` | **5.0** | |
| `OMS_V2_CW_HARD_STOP_PCT` | **8.0** | |
| `OMS_V2_RTH_EDGE_BRACKET_ENABLED` | **false** | #647 Gate 1 NOT waived — see below |
| `ATR_FLIP_VOL_FLOOR` | **10000** | |
| `CW_V2_RECLAIM_GAP_BARS` | 1 | |

---

# ⭐⭐ TODAY IN ONE LINE

Ten PRs merged and deployed across three windows; RECLAIM1 built to the operator's one-entry-per-flip ruling and
shipped **dark** with a fail-safe; the Gate 1 watcher armed; and the Webull one-sided-entry question
answered — **zero venue rejections in 54 orders**, the two missing legs were our own consumed
fan-out slot.

---

# 🔔 GATE 1 WATCHER IS LIVE — first page possible 09:30 ET tomorrow

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

# ⛔ RECLAIM1 — what it does, and what it does NOT do

**Does:** admits only the first ATR-trail resting entry per confirmed ATR BUY segment; refuses the
rested reclaim (`slot != "first"`) **and** the reactive reclaim (an unconditional guard in
`_cw_v2_quote`). FTFT 12:10 and SUNE 13:07 would have been refused on **both** brokers rather than
filling Schwab alone. It intentionally reduces trades.

**Does NOT:** make Webull chase a missing reclaim; change exits; change sizing; do anything at all
while the flag is off.

**Fail-safe:** one flag, default off, absent from the env. Reverting is a flag flip, not a redeploy.

⛔ **Turning it on is a live-money behaviour change and needs a deliberate decision.** It has never
run against a live tape.

---

# ⛔ F1 — THE v2 POSITION COUNT IS SCHWAB-ONLY, AND THE TREND EXIT IS BLIND TO A WEBULL-ONLY FILL

`_fetch_position_maps` (`schwab_1m_v2_bot.py:1814`) filters **both** halves to
`live:schwab_1m_v2`. A Webull-only fill therefore reads `position_qty=0`, and
`schwab_1m_v2.py:2319` returns `None` before it can emit an exit.

**Proven live 2026-09-09 on YMAT:** ATR flipped SELL, `pos_qty=0`, **no exit fired**.

⛔ **The hard stop, target and ladder DO work** — 57 filled pre-market Webull sells. **Only the flip
is blind.** The flat check, coverage and reconciler are whole. Owner: `codex-2` (one-sided
lifecycle). ⚠ This was never explained to the operator until today; do not let it go quiet again.

---

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

# ⭐⭐ THE WEBULL ENTRY INVESTIGATION — settled numbers (#934)

24 logical producer episodes (67 raw intents → 22 slot identities + 2 suppressed), FTFT/SUNE/YMAT:

| | episodes | Webull fills | blocked while held | consumed slot | rested, unfilled |
|---|---:|---:|---:|---:|---:|
| FTFT | 7 | 5 | 0 | 1 | 1 |
| SUNE | 5 | 4 | 0 | 1 | 0 |
| YMAT | 12 | 4 | 7 | 0 | 1 |

⛔ **ZERO Webull venue rejections among 54 buy orders.** Schwab filled 11 entry episodes, Webull
matched **9** — the only two gaps are FTFT `12:10:08` and SUNE `13:07:16`, both our own consumed
fan-out slot. The 7 YMAT blocks were the managed-position collision guard working correctly; **do
not weaken it to raise the fill count.**

⭐ **The subtlety that makes this make sense:** `_SLOT_BY_SOURCE` maps `rth_resting → "resting"` and
`reactive → "reclaim"`. The **fan-out** slot vocabulary is not the **CW entry** slot vocabulary
(`first`/`reclaim`), so a rested *reclaim* collides with the *first* entry in fan-out identity space.

---

# ⭐⭐ STATUS SPLIT — ANSWERED vs UNEXERCISED. Do not collapse these.

| ✅ ANSWERED **and CLOSED** | ⛔ DEPLOYED and **UNEXERCISED** |
|---|---|
| **Webull one-sided entries** — measured; zero venue rejections; cause is our slot | **RECLAIM1** — deployed **dark**; never run on a live tape |
| **DRIFT1** — false-clean fixed and now scheduled | **GATE1 watcher** — armed; no qualifying shape has ever existed |
| **HALT-FILTER / HALT-DETECTOR** — session-boundary artefact fixed at source | **INC1** — installed; synthetic incident opened and closed, no real one yet |
| **F1 mechanism** — root cause proven live on YMAT | **STOPASK (#929)** — observability only, unexercised |
| **#933 controls** — all five admission branches + the reactive guard mutation-red | **CONF3 / SIL1 / released-leg recovery / HDL1** — still never fired |

⛔ **UNKNOWN is not PASS.** Read `STATUS.txt`; do not infer health from the absence of a page.

---

# ▶ CARRIED — open, read-only, nobody is blocked on them

| item | question | owner |
|---|---|---|
| **F1/F2 one-sided lifecycle** | the fix for the blind trend exit | `codex-2` |
| **confirmation-exit race** | per-account in-flight ownership held to terminal close | unassigned |
| **STOPMKT probe** | operator deferred: does a stop-market entry change the fill? | deferred by operator |
| **PROV1** | arming on flips reconstructed from db-seed (649 occurrences) | unassigned |
| **liquidity floor** | the cancel-on-floor-fail pattern; floor 10,000 | unassigned |
| **0.5% stop-limit band** | strands the entry on fast moves (YMAT flip bar) | unassigned |
| **F6 / F8 / F9 / F10** | stale `current_profit_pct`; Webull manual-order ingestion | unassigned |
| **BNC 11:03 · claim overhang · fan-out size · ELIG · AMEND1 · REJ1 silence** | carried from 09-08, unchanged | unassigned |

⭐ **`drift audit` is CLOSED** — #924 scheduled it; it is no longer unscheduled.

---

# ⚠️ VERIFICATION FAILURES OF MINE TODAY, RECORDED SO THEY ARE NOT REPEATED

1. **I verified a proxy for a rule and stated the conclusion anyway.** I told codex no rebase was
   needed on #931 because `git log base..head` resolved to my three commits. The gate requires the
   base to be an **ancestor** of the head (`review_pin_gate.py:218`); a resolving range does not
   imply it. ⇒ *Run the gate's own check: `git merge-base --is-ancestor $BASE $HEAD`.*
2. **I committed a mutated wrapper** — `exit 0` in place of the market-hours guard, which would have
   made the Gate 1 watch do nothing on every run, silently. I had piped my mutation script through
   `head -4`; SIGPIPE killed it before the restore. My own tests caught it. ⇒ *A mutation harness
   needs a `trap`, and never a pipe that can die early.*
3. **A permissions false-zero.** My first grep for `fanout_webull_collision_managed` and
   `pair_cancel_unconfirmed` returned zero because the logs are root-only and I ran as `trader`.
   ⇒ *A zero from a tool that cannot read the source is UNKNOWN, not absence.*
4. **Two false readings during deploy verification**, both re-derived before reporting: `oms` and
   `strategy` read "inactive" because I guessed unit names (they are `project-mai-tai-oms` /
   `-strategy`), and "38 tracebacks" was a whole-file count — the post-restart window is **0**.
5. **I shipped a test suite that only ran during market hours.** #931's wrapper controls drove the
   real clock, so after the close the four transition tests silently stopped running; CI had passed
   only because it ran in-window. ⇒ *A control whose result depends on WHEN it runs is not a
   control.* Found by codex, not by me.
6. **I missed that reclaim was already off** when the operator asked me to investigate entries.
   ⇒ *"You need to think about all the direction, not just one direction."*
7. **I read the box's UTC clock and labelled it ET.** `systemctl`'s ORB timestamp is `10:54:27 UTC`
   = **06:54 ET**; I reported "10:54 ET" in the handoff, the log and a memory. I had converted
   correctly for the SUNE log lines an hour earlier, so this was inconsistency, not ignorance.
   ⇒ *The box clock is `Etc/UTC`. Convert EVERY timestamp read from it, including systemd's.*
8. **I said "one deploy window" without checking.** There were **three** (06:54, 07:27, 19:03 ET),
   provable from `git reflog` on the box in one command. I generalised from the window I had
   watched. ⇒ *Ask what the ARTEFACT says, not what I remember doing.*
9. **I announced Gate 1 as arming at 09:00 ET** — the cron hour, not the wrapper's guard, which
   refuses until 09:30. I wrote that guard. ⇒ *A schedule is not a behaviour; read the code that
   runs, not the line that launches it.* All four caught by codex-2 on #935, none by me.

---

# ▶ NEXT SESSION — Thursday 2026-09-10

1. 🔔 **Gate 1 can first page at 09:30 ET** (the cron starts 09:00; the watcher refuses until 09:30). If it pages, the qualifying window is open **only while that
   position is** — take the `previewOrder` by hand, immediately. Preview, never a live probe.
2. ⛔ **Decide RECLAIM1.** It is deployed, dark, controlled and reversible by one flag. It has never
   run live. This is an operator decision, not an agent one.
3. **First full session on the deployed six-PR stack.** Nothing from before 23:03 UTC tonight is a
   baseline for it.
4. **F1 one-sided lifecycle** (codex) and the **confirmation-exit race** are the open live defects.
5. Watch `INC1` and `#914`'s four conditions. Read `STATUS.txt`; silence is not green.

⛔ Watch items live here, not in [`handoff-open-items.md`](handoff-open-items.md).
