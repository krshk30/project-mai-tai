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

# ✅ PRODUCTION — RUNTIME SHA is `2d54d29c` (main may sit ahead by docs-only commits)

**Written by `claude-1`, 2026-09-13 (Sunday). Monday 2026-09-14 is the first live session.**

| | |
|---|---|
| box (deployed) | **`2d54d29c`** — read FROM THE BOX 2026-09-13 22:54 UTC, branch `main`, checkout clean (0 files) |
| GitHub main | **`2d54d29c`** at the time of writing. ⭐ **Merging this handoff PR moves main ahead of the box BY DESIGN — see the DOCS-ONLY DIVERGENCE rule below. It is not a deploy gap and needs no sync and no restart** |
| gate pin | `preopen.sh` pins `EXPECTED_SHA=2d54d29cd58caa20a70cf3cd61f5ba1aa11c8a7f` matching the box, **and** carries `--expected-quiet-service reconciler`. Updated atomically with the sync |
| open PRs | **none** — this handoff PR excepted |
| exposure | both live accounts flat — 0 non-closed managed rows, 0 nonzero `account_positions`, 0 working broker orders |
| merges 09-12 → 09-13 | **#951** watch · **#964** SLOTCLEAR1/LIQPULL1 rows · **#965** threshold parity · **#967** log scoping · **#968** zero-record labelling · **#969** expected-quiet services. All independently reviewed and pinned by `claude-1` |

## Restarts — THREE services, in two windows, both on 09-12

⛔ The earlier draft of this file said "v2 only". That was true of the 16:34 window only.

| window (ET) | services | why |
|---|---|---|
| **16:34:27** | `schwab-1m-v2` | #964 adds `resting_below_floor_bars=` to the resting-cancel lines; without it **LIQPULL1** cannot read the streak and pages `COULD_NOT_TELL` on every liquidity cancel |
| **17:17:54** | `oms`, `reconciler` | **#957** had been on disk but unloaded — oms started 18.4h before it merged. **#961** (page only actionable alert transitions) had never loaded at all: the reconciler had been up since **08-30**, 13 days |

⚠ `project-mai-tai-oms.service` does **NOT** restart `strategy` with it — that is the oms *target*.
Only two services restarted in that window. `strategy` was verified not to load any changed module
(`schwab_1m_v2`, `v2_flip_entry_ownership`, `reconciliation`), so nothing stale runs there.

| service | pid | started (UTC) | | service | pid | started (UTC) |
|---|---|---|---|---|---|---|
| **schwab-1m-v2** | **1089323** | 09-12 20:34:27 | | orb (paper) | 609358 | 09-11 06:30:47 |
| **oms** | **1105862** | 09-12 21:17:54 | | market-data | 2202865 | 08-30 19:54:39 |
| **reconciler** | **1105870** | 09-12 21:17:55 | | market-capture | 2202817 | 08-30 19:54:38 |
| strategy | 555227 | 09-11 00:14:59 | | control | 311547 | 09-10 12:16:19 |

## Post-sync evidence (gate run 2026-09-13 22:54 UTC, re-derived by `claude-1`, not carried over)

| row | result |
|---|---|
| Restarted services | **PASS 3/3** |
| Services not restarted | **PASS 6/6** |
| Both accounts flat before AND after | **PASS** — 0 managed rows, 0 broker positions, 0 working orders |
| Migration | **PASS** — `20260910_0020`, no schema change |
| Running-process flags | **PASS 5/5** |
| Bar continuity | `N/A_OFF_SESSION` — grades the **Saturday** restart, which was off-session. ⛔ It stays N/A on Monday; only `gaps spanning restart = 0` is required |
| Tracebacks | **`PARTIAL_N/A`** — v2 and oms both **0 tracebacks** against real denominators; `reconciler=N/A_EXPECTED_QUIET(0/0)` |
| REST warmup · BOOT-HOLD released | **FAIL** — the only two, both Monday-dependent |

⚠ **Record counts are point-in-time and grow constantly**, so exact denominators are stale the
moment they are written — I measured different totals from `codex-2` an hour apart for this reason.
What is durable is the SHAPE: zero tracebacks, both trading services measured against real
denominators, reconciler expected-quiet.

⛔ The **reconciler writes no log at all** — 0 bytes since 08-28 — so its row is *unmeasured*, not
*verified clean*. #969 makes that distinction explicit and makes an **undeclared** silence FAIL, so
if oms or v2 ever goes silent the gate reds instead of quietly passing.

# 🔴 2026-09-14 MONDAY — SESSION IN PROGRESS, READ THIS FIRST

**Written by `claude-1` 10:44 ET, mid-session, because the operator is restarting the session.**

## Live state at 10:44 ET
| | |
|---|---|
| box / runtime | `2d54d29`, v2 pid `1089323`, no restarts |
| main | `f582808` — ahead by docs-only + merged fixes NOT yet deployed |
| exposure | **ALL FLAT**, no working orders |
| watchlist | `BMGL, FTFT` |
| open PRs | none |

## ⛔ TODAY'S LOSS — one setup, lost to a broker rejection
```
09:40  BMGL entry placed @ 8.2855 — REJECTED by BOTH brokers
09:51  BMGL FLIPS BUY at 8.56, straight through our level   <- the trade
10:13  a replacement is finally ACCEPTED
10:19  reprice CANCELS it (trail moved >0.5%)
10:20  replacement REJECTED — bare again
```
Schwab refused on eligibility (`must be placed with a broker`). Webull refused with
`ORDER_RISK_RULE_PRICE_AGGRESSIVE` — **5 occurrences all-time: RUBI once, BMGL 4× today.**

⇒ **The rejection did not cost a delay. It cost the trade.**

## Root cause status
⛔ **Webull's rule is NOT understood and must not be guessed.** Distance from market does NOT
separate accepted from rejected: BMGL was rejected at +9.67% and accepted at +10.24%, and a +31%
order was accepted on 09-09. Webull's own *preview* accepted all five combinations that live
placement refused, so preview cannot screen it. Direct Webull quotes are `403
MARKET_DATA_NOT_SUBSCRIBED` — a separate OpenAPI quote subscription is a **purchase decision**.

## Assigned to `codex-2` by the operator
1. **Diagnostics** — preserve Webull's `request_id`, exact wire prices, instrument_id, timestamp and
   full broker error on the reject record. The SDK supplies the request id; our formatter discards it.
2. ⭐ **The cancel-before-confirm reprice window** — we surrender a working resting order before
   knowing the replacement is accepted. This is OURS, not Webull's, and it is what left BMGL bare.
3. Escalate the four BMGL `request_id`s to Webull support.
4. Schwab quote recorded only as a clearly labelled proxy.

## ⚠ MERGED BUT NOT DEPLOYED — needs the after-close window
| PR | What | Needs |
|---|---|---|
| `#972` | BOOT1 same-millisecond log ordering | watch cron picks it up, no restart |
| `#973` | reconciliation scope + fill-balance checkpoint | **reconciler restart** |
| `#974` | Webull recycled-ticker instrument pick (`f5828088`) | **OMS restart** |

⛔ **DEPLOY ORDER:** sync, then **restart the reconciler FIRST** — `deploy_preflight.py:156` fails on
`critical_findings > 0` and the 221 stale findings only clear once #973's code is running.
Then `EXPECTED_SHA` and `--expected-quiet-service reconciler` move with the sync, in ONE action.

## 🔎 MONITORS ARE SESSION-LOCAL AND DIE ON RESTART
Nothing persistent is watching the tape. To re-arm after a restart, watch the v2 log for:
`V2-FLIP-OWNER-OPPORTUNITY|V2-RESTING-PLACE|V2-RESTING-EH-ARM|V2-FLIP-OWNER-FILL|`
`V2-RESTING-SLOT-CONSUMED|reason=liquidity_floor|PRICE_AGGRESSIVE|Traceback|CRITICAL`
⭐ The installed **known-defect watch cron is NOT session-local** and keeps running regardless.

## ⛔ Two of my own errors today, so they are not repeated
1. I reported **no live BUY flip** — `head -4` truncated the listing and hid the BMGL line behind six
   FTFT warmup lines. **Count or list in full; never sample a question of existence.**
2. I said a resting order was **live** when it had been cancelled six minutes earlier.
   **A live-state claim decays — re-read before repeating it.**

---

# FLAGS — read from the RUNNING processes, not the env file (2026-09-13 22:54 UTC)

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
3. It verifies in one pass: run window before 07:00 · SHA `2d54d29c` and clean tree · v2 identity
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

⭐⭐ **DOCS-ONLY DIVERGENCE IS EXPECTED — do NOT sync or restart for it.** The RUNTIME SHA is
`2d54d29c`. Merging a docs-only PR (this handoff included) moves **main** ahead of it immediately,
and that is the normal, correct state. It is **not** a deploy gap.

- **Monday's gate is unaffected.** `EXPECTED_SHA` is checked against the **box checkout**, not
  against GitHub main. Main moving ahead cannot turn the gate red.
- **Test it the right way:** `git diff --name-only <box-sha> origin/main`. Treat the box as behind
  **only if something outside `docs/` appears**.
- ⛔ **Do not `git pull` on the box to "fix" a docs-only divergence.** A sync drags in the
  `EXPECTED_SHA` and `--expected-quiet-service` pairing below, and a needless sync before a live
  session is risk taken for nothing.

⛔ **SYNC RULE — the gate's config moves WITH the checkout, in one action.** Applied twice now
(`5dae7c9a` → `d87d8d1b` → `2d54d29c`), each time without restarting a service. Sync the checkout
and leave `EXPECTED_SHA` behind and the gate fails on **checkout identity** rather than on anything
real — an alarming red that means nothing.

⭐ #969 added a second thing that travels with it: `--expected-quiet-service reconciler`. This is
not optional decoration. I ran the tool on the box **both ways** before it merged: without the flag
the reconciler reads `UNMEASURED(0/0)` and **FAILS** the Tracebacks row; with it, it reads
`N/A_EXPECTED_QUIET(0/0)` and the run is left with only the Monday-dependent failures. **Code, flag
and `EXPECTED_SHA` land together or not at all.**

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

# ✅ F1 — FIXED BY #930, DEPLOYED, AND **EXERCISED** ON LIVE TAPE

⛔ **This section has now been wrong twice.** The 09-12 draft reopened F1 as a live defect; my first
correction then said it had "fired 0 times". Both wrong, both caught by `codex-2`. The zero came
from grepping `atr_sell_observation` — the **wire event type, which is never logged** — and from
plain `grep` against **gzipped** rotated logs, which reads nothing. The emitted markers are
`[OMS-V2-CW-FLIP-EVALUATED]` and `[OMS-V2-CW-FLIP-LEG]`, and rotated files need `zgrep`.

Measured on the box 2026-09-12, all `oms.log*` since #930 deployed (2026-09-09 23:03 UTC):

| | |
|---|---|
| ATR-SELL decisions evaluated | **43** (09-09 ×1 · 09-10 ×12 · 09-11 ×29 · 09-12 ×1) |
| leg evaluations | **86** — ⭐ **43 `live:schwab_1m_v2` and 43 `live:orb`, exactly balanced** |
| outcomes | 83 `not_owned`, **3 `armed`** |
| the 3 armed | FTFT 09-11 **18:13 — BOTH legs, schwab AND orb** · FTFT 09-11 19:20 schwab |
| ⛔ Webull-only-held decisions | **0** |

⇒ **The account-neutrality itself is PROVEN live, not merely deployed.** Every decision reaches both
accounts independently (43/43), and a Webull leg has actually been armed for close. ⛔ `30` is the
count in the current uncompressed file only; the full figure since deploy is **43**.

⇒ **What remains unexercised is only the exact original F1 shape:** a decision where `live:orb` is
armed while `live:schwab_1m_v2` reads `not_owned`. That has occurred **zero** times. The YMAT 09-09
evidence is the PRE-FIX proof of that shape and must not be quoted as current.

## ⭐⭐ THE DISTINCTION THAT MADE THIS EASY TO GET WRONG — still true, do not "fix" it
`_fetch_position_maps` **is still scoped** to the v2 account, **deliberately**: its own docstring
says cross-venue fan-out intents must not enter the ENTRY-decision signal. #930 made the **EXIT**
path account-neutral. **Entry-scoped and exit-neutral are both correct at once** — seeing that
filter still present is NOT evidence the defect returned.

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
| **F1 / #930 account-neutral ATR exit** — fixed, deployed, and **exercised**: 43 decisions, 43/43 legs per account, 3 armed incl. a Webull leg | **the Webull-only-held shape** — `live:orb` armed while schwab reads `not_owned`: **0** occurrences |
| **#933 controls** — all five admission branches + the reactive guard mutation-red | **CONF3 / SIL1 / released-leg recovery / HDL1** — still never fired |

⛔ **UNKNOWN is not PASS.** Read `STATUS.txt`; do not infer health from the absence of a page.

---

---

# ▶ CARRIED — open, read-only, nobody is blocked on them

| item | question | owner |
|---|---|---|
| ~~**F1/F2 one-sided lifecycle**~~ | ✅ **CLOSED by #930** — deployed **and exercised** (43 decisions, both accounts, 3 armed). Only the Webull-only-held shape is still unseen | — |
| **confirmation-exit race** | per-account in-flight ownership held to terminal close | unassigned |
| **STOPMKT probe** | operator deferred: does a stop-market entry change the fill? | deferred by operator |
| **PROV1** | arming on flips reconstructed from db-seed (649 occurrences) | unassigned |
| **liquidity floor** | ⭐ the *cancel* half is addressed by **#960** (pull only after 3 consecutive sub-floor bars, one good bar re-places) and watched by **LIQPULL1** — replay-proven, unexercised live. The floor VALUE (10,000) and the arm-time staleness remain open | unassigned |
| **0.5% stop-limit band** | strands the entry on fast moves (YMAT flip bar) | unassigned |
| **F6 / F8 / F9 / F10** | stale `current_profit_pct`; Webull manual-order ingestion | unassigned |
| 🔧 **`board.sh overlap` broken on macOS** | `overlap.sh:70` uses `mapfile` (bash 4); this Mac runs bash 3.2.57. ⛔ Fails CLOSED (exit 1), and the **claim-time** guard is UNAFFECTED — `--candidate` returns at :67 with a `while read` loop, so two agents still cannot claim the same path. What is lost is only the pairwise sweep of EXISTING held claims, which is the one thing that catches a `FORCE_CLAIM=1` overlap and globs that grow into each other as files are added. **NEXT ACTION: replace `mapfile -t L < <(held)` with a `while read` loop.** Operator asked for it THIS WEEK (09-13). | `claude-1` |
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
