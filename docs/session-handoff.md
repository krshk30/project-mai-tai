# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-14 18:15 ET.** Batch `2026-09-14-webull-latch-two-missed-flips-deployed`.
Integrator for this rotation; `codex-2` executed the deploy and reviews this PR. The author never reviews.

> ⛔⭐⭐⭐ **RECLAIM1 IS LIVE.** `MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true` read from
> the running v2 (pid `1628896`) at 21:58 UTC. One trade per ATR segment, first resting entry only, no reclaim.

---

# ✅ PRODUCTION — main and box IN SYNC at `d99552c8` (everything merged today is DEPLOYED)

| | |
|---|---|
| box (deployed) | **`d99552c8ec8b36d459e39bdce84138d8f1a88ac3`** — read FROM THE BOX 2026-09-14 21:58 UTC, branch `main`, clean |
| GitHub main | **`d99552c8`** — identical at the time of writing. Merging this handoff PR moves main ahead by docs only — the DOCS-ONLY DIVERGENCE rule holds; no sync, no restart |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-15` · `EXPECTED_SHA=d99552c8…` · `EXPECTED_PID=1628896` · `EXPECTED_START='Mon 2026-09-14 21:40:48 UTC'` · snapshot `v2-restart-before-20260914.json` · report `v2-restart-evidence-20260915.md` · still carries `--expected-quiet-service reconciler` (reconciler.log is still 0 bytes after #973) |
| open PRs | **none** — this handoff PR excepted. #975 (my 10:44 mid-session handoff) is CLOSED, superseded by this file |
| exposure | **21:58 UTC: broker positions 0 · open managed rows 0 · working orders 0 · reconciliation critical findings 0** (three consecutive 30-s runs) |
| merges 09-14 (**11**) | #966 · #970 · #971 · **#972** BOOT1 sort · **#973** reconciliation scope + fill checkpoint · **#974** Webull recycled ticker · **#976** Webull reject evidence · **#977** LATCH1 · **#978** ORB times in ET · **#979** LATCH2 · **#980** BOOT2 |
| deploys 09-14 | **Window A 16:57–17:00 ET:** snapshot → sync-only → reconciler (`1615355`). **Reconciler again 17:34 ET** (`1626620`) after the checkpoint move. **Window B 17:37–17:41 ET:** oms+strategy, v2, control. Executed by `codex-2` on operator GO |

## Restarts — five services, all on 09-14, all `NRestarts=0`, 0 tracebacks after start

| service | pid | started (UTC) | why |
|---|---|---|---|
| **reconciler** | **1626620** | 21:34:21 | #973 (first restarted 20:59:31 as 1615355; restarted again for the checkpoint env) |
| **oms** | **1627713** | 21:37:08 | #974 #976 |
| **strategy** | **1627724** | 21:37:08 | ⛔ **the tracked OMS deploy script stops and restarts strategy.** The 09-12 handoff said the `.service` unit does not — that was wrong about the DEPLOY path. Restarted although no PR touched it; healthy |
| **schwab-1m-v2** | **1628896** | 21:40:48 | #977 #979 |
| **control** | **1629380** | 21:41:28 | #978 |
| market-data 2202865 · market-capture 2202817 · orb (paper) 609358 | — | unchanged | orb shows `NRestarts=1` from before today |

## Post-deploy evidence (`v2_restart_evidence.py report`, run by `codex-2`, re-derived by `claude-1` at 21:58 UTC)

| row | result |
|---|---|
| Restarted services | **PASS 5/5** |
| Services not restarted | **PASS 4/4** |
| **Both accounts flat before AND after** | ⛔ **FAIL — the ONE known red.** The immutable pre-restart snapshot (20:57 UTC) holds `open managed rows=1`: the BMGL phantom row from the operator's hand sale, closed by hand at ~17:20 ET *after* the snapshot. After-state is 0/0/0 and the OMS preflight immediately before Window B printed `Live deploy preflight passed`. **Codex refused to rewrite historical evidence; correct.** |
| Migration | PASS — `20260910_0020`, no schema change |
| Running-process flags | PASS 5/5 |
| REST warmup | PASS 3/3 |
| BOOT-HOLD released | PASS at 21:40:59 UTC (11 s after start; the watchlist was non-empty after close) |
| Bar continuity | PASS — 3 pairs bracket the restart, **gaps spanning restart = 0** |
| Tracebacks | 0 in control/oms/v2/strategy; reconciler `N/A_EXPECTED_QUIET(0/0)` |

⛔ **`preopen.sh` re-runs that same report against that same snapshot, so Tuesday's gate WILL print this one FAIL again.**
**Ruling for Tuesday:** the gate is green if and only if the ONLY failed row is *"Both live accounts flat before/after"* with
`before … open managed rows=1` and `after … 0`. Any other FAIL is real. Do not edit the snapshot.

---

# FLAGS — read from the RUNNING processes (21:58 UTC)

| process | flag | value |
|---|---|---|
| v2 | `FLIP_OWNED_FIRST_ENTRY_ENABLED` / `CONFIRMATION_ACCOUNT_NEUTRAL_DISCOVERY_ENABLED` | `true` / `true` |
| v2 | `CW_V2_RECLAIM_ENABLED` · `ATR_FLIP_PROBE_SYMBOLS` · `MACD_PROBE_SYMBOLS` | `false` · `*` · `*` |
| oms | `OMS_V2_EXIT_MANAGEMENT_ENABLED` · `OMS_V2_OVERNIGHT_FLATTEN_ENABLED` · `OMS_V2_CW_FLOOR_EXIT_ENABLED` | `true` · `true` · `true` |
| oms | `OMS_V2_CW_TARGET_PCT` / `HARD_STOP_PCT` · `RTH_EDGE_BRACKET_ENABLED` · `EOD_OCO_TRANSITION_ENABLED` | `5.0` / `8.0` · `false` · `false` |
| oms | `OMS_V2_EOD_CANCEL_REEXIT_ENABLED` (EOD1601) | **absent → `False`. Operator 09-14: NOT wanted** (see AFTER-HOURS below) |
| **reconciler** | **`MAI_TAI_RECONCILIATION_FILL_BALANCE_SINCE`** | **`2026-09-14T21:20:41+00:00`** — `/etc/project-mai-tai/project-mai-tai.env` line 219, proven in pid 1626620. ⛔ `settings.py` default still says `2026-09-12 21:17:49`; the env overrides it **deliberately** — carry the divergence, do not "fix" it |

Why the checkpoint moved: the operator sold the last BMGL share by hand at ~16:24 ET (see TRADE TRUTH). #973's fill-ledger check
then read the 15:59:03 buy as net +1 because the hand sale is, correctly, not booked as ours. Both accounts were positively flat with
zero working orders at 21:20:41 UTC (last fill 19:59:03 UTC), so that instant is a legitimate fixed checkpoint on the same terms as
the 09-12 one. **No manual fill was fabricated.**

---

# ⭐⭐ TUESDAY 2026-09-15 PRE-OPEN

1. **~06:30 ET:** `ssh mai-tai-vps /home/trader/preopen.sh`. Green = only the ONE known FAIL above. `EXPECTED_DATE` is already `2026-09-15`.
2. **The boot hold re-HELDs every ~60 s overnight** (empty evaluated population after the 04:00 roll) and releases when Tuesday's
   watchlist arrives, ~04:1x. Expected. Do not touch it. The REST-warmup and BOOT-HOLD rows are Tuesday-dependent until then.
3. ⭐ **BOOT1 proof of #980:** the first watch run at **03:50 ET** must read BOOT1 `GUARD_WORKING` or `UNEXERCISED`, never `RECURRENCE`,
   and `state.json`'s BOOT1 episode (`delivered=true` since 09:50 UTC today) must clear. That is the live grade; do not claim it earlier.
4. ⛔ **Restored owners — #979's restored-owner path is UNDER TEST.** After the restart v2 logged
   `[V2-FLIP-OWNER-RESTORED] FTFT opportunity_id=1789410362403 phase=bound positions=2 entry_allowed=0 pending_fresh_position_read` and
   `… BMGL opportunity_id=1789415762432 phase=consumed positions=1 entry_allowed=0 pending_fresh_position_read`. Both accounts are flat.
   **Before 07:00 ET confirm both reach idle/released** (`grep -E 'V2-FLIP-OWNER-(RELEASED|RECOVERY|RETIRED)' | grep -E 'FTFT|BMGL'`).
   If either still refuses admission (`[V2-FLIP-OWNER-ADMISSION] … reason=owner_phase_unknown|owner_phase_bound`) at the first short
   segment, that is a #979 finding — record it, do not hand-edit state.
5. Session-local tape monitors DIED with this session. The known-defect cron (every 5 min, 03:50–20:15 ET weekdays) and the GATE1 cron are
   the only persistent watches.

---

# ⛔⭐⭐ TODAY'S TRADE TRUTH — BMGL was Webull-only ALL DAY; two flips were MISSED by the same defect

**Schwab dropped every BMGL leg from 10:08 ET** (`[OMS-INTENT-DROPPED] … schwab_ineligible_cached`, 8×). Every BMGL fill is the Webull leg, qty 1.

| entry (ET) | exit (ET) | prices | result | exit path |
|---|---|---|---|---|
| 10:54:41 | 10:56:05 | 7.86 → 7.645 | −2.7% | confirmation exit (10:55 bar closed 7.6999 < trail 7.8451) — **the Webull leg WAS closed: #945 EXERCISED** |
| 13:39:58 | 14:01:11 | 7.22 → 7.58 | **+5.0%** | broker target (pair attached 0.95 s after fill) |
| 15:01:43 | 15:45:08 | 7.65 → 7.135 | −6.7% | ATR flip exit |
| 15:59:03 | **hand sale ~16:24** | 7.3399 → (operator's price, not ours) | — | confirmation exit fired 16:01 and was **REFUSED after hours** (09-06 rule); operator sold by hand and cancelled both Webull legs |

FTFT: **12:22:23 → 12:22:33, 5.04 → 5.28 (Schwab ×2) / 5.29 (Webull), +4.8% / +5.0%**, both broker targets, 10 s round trip. Clean.

## The two missed BMGL flips, one mechanism, pinned in code and on the tape (memory `project_mai_tai_resting_latch_blind_to_webull_only_fill`)
- **10:59 flip.** After the 10:56 exit RECLAIM1 released the segment (`entry_allowed=1`) but no rest was placed: the resting latch's only
  fill detector was the Schwab-scoped position poll (`position_qty_held`, "without inferring the Webull leg"), which read 0 all day.
  The 10:59 bar ran 7.75→8.08 through 7.8451 with nothing resting. 11:01:02 phantom `flip_no_fill` cancel — the tell. **Fixed by #977.**
- **12:11 flip.** The phantom cancel BOUND a fan-out slot under an idle owner → the 11:59 SELL flip marked the owner UNKNOWN → the recovery
  loop had no retire path for a flat unknown owner (690 lines) → admission refused 4× (12:02–12:10) → 12:11 BUY flip at 7.58 with nothing
  resting. Self-healed at the 13:08 sell flip, as predicted. **Fixed by #979** (no slot bind on cancel; flat-consumed early return; proof
  path for restored cancel-minted owners).
- FTFT 12:22 showed the latch is also **sampled per bar**: a sub-bar round trip read 0 at both ticks → phantom cancel 12:24. #977 covers it
  because the Webull leg filled. ⛔ **Residual, unbuilt:** Schwab-only fill + sub-bar round trip.

## Webull `ORDER_RISK_RULE_PRICE_AGGRESSIVE` — NOT understood, NOT fixed, now measurable
4 rejects (09:40, 10:08, 10:10, 10:20), all with our stop 9–15% above the ask; 6 acceptances since 10:49 at +3–4%. ⛔ The 10:13 acceptance
at +9.7% and a +31% acceptance on 09-09 mean distance is NOT the rule. #976 now stores `webull_request_id`, error fields and exact wire
prices on every reject; **the four morning request ids are in `oms.log` for the Webull escalation (codex lane).** It WILL recur on hard runners.

---

# ⛔⭐⭐ AFTER-HOURS EXITS — SETTLED WITH THE OPERATOR 2026-09-14 evening (read before touching any of it)

| mechanism | state | what it does |
|---|---|---|
| Webull protective pair | RTH-only by design (`CORE`, DAY). Webull drops the target leg at the bell, keeps the stop leg "Working" but it cannot fill until 09:30 | decoration after 16:00 |
| Schwab OCO | `DAY, NORMAL` — expires at the bell | gone after 16:00 |
| **software ladder** (−8% stop, +5% ride-past floor) | **ON, both brokers, 16:00–20:00**, EH-LIMIT 0.5% under the bid; releases the Webull pair first (`OMS-CANCEL-PAIR-REQUEST`); stand-down fails OPEN for Webull and EXPIRES for Schwab (~30 s after the bell) | the after-hours cover |
| **19:55 overnight flatten** | **ON.** Cancels our legs, sells EH-LIMIT, retries to 20:00 | ⛔ has **never closed a Webull share** — its one run (DAIC 08-24) found the share already gone. First real run is its test |
| confirmation exit | **blocked after 16:00** by the 09-06 rule (`outside_rth_pair_release_would_be_irreversible`) | **operator 09-14: leave it** |
| 16:00 EOD OCO transition | OFF since 08-04 (jam mitigation) | unchanged |
| **EOD1601** 16:01 cancel-and-re-exit (#889/#898) | built, **OFF, never run**; ⛔ **Schwab-only** — `routing.fetch_exit_legs_for_entry` RAISES for Webull and the sweep logs `UNANSWERABLE_HARVEST` | **operator 09-14: NOT wanted** |

⇒ **Operator's ruling:** the after-hours design IS the software ladder + 19:55 flatten, common to both brokers. Nothing to enable.
⇒ **Open trading-rule question (operator's, not ours to build):** a rest placed inside the last minutes can fill with no bar left for its
confirmation (15:56 rest → 15:59:03 fill → confirmation bar 16:00–16:01, after the bell). A pre-close cutoff is a rule change.

---

# ⭐⭐ STATUS SPLIT — ANSWERED vs UNEXERCISED

| ✅ ANSWERED / EXERCISED today | ⛔ still UNEXERCISED |
|---|---|
| **#945** account-neutral confirmation discovery — first Webull-only fill ever, closed the Webull leg at 10:56 | **19:55 flatten on a real Webull share** |
| **Webull attach after a bare fill — WORKS**: 3/3 today, attempt 1, 0.6–0.95 s bare (`project_mai_tai_dual_broker_fanout_build`'s "never succeeded" is STALE) | **EOD1601** (and it is Schwab-only) |
| **#960 / LIQPULL1** — two 3-bar liquidity pulls on BMGL 13:20 & 13:27, `guard_working 2/2` | **GATE1** qualifying shape |
| **SLOTCLEAR1** 5 fresh sells, 5 clean | **#979 restored-owner path** — under test Tuesday morning (above) |
| **RESERVE1** guard-working 4/4 releases, 0 reserved-share rejects | **#980** — graded at 03:50 ET Tuesday |
| **CONF3 fan-out** — the confirmation exit reached BOTH legs (`legs_total=2`) | Schwab-only sub-bar latch shape |
| the Webull-only-held shape — SEEN, 4× | reprice cancel-before-confirm window (measured FTFT 12:16–12:17: **66 s** bare, replacement on the NEXT bar tick) — codex lane |

---

# ▶ CARRIED — open, read-only

| item | question | owner |
|---|---|---|
| **Webull PRICE_AGGRESSIVE escalation** | send the 4 morning request ids to Webull; #976 captures future ones | codex-2 |
| **reprice window** | cancel-before-confirm: replacement placed on the next bar tick, not on cancel confirmation | codex-2 |
| **Schwab-only sub-bar latch shape** | held gate is sampled per bar; no fan-out fill event to clear on | unassigned |
| **pre-close entry cutoff** | last-minute rests cannot be confirmation-exited (see AFTER-HOURS) | **operator ruling** |
| confirmation-exit race · STOPMKT probe · PROV1 · 0.5% band · F6/F8/F9/F10 · BNC · claim overhang · fan-out size · ELIG · AMEND1 · REJ1 silence | carried unchanged | unassigned |
| 🔧 `board.sh overlap` on macOS | `mapfile` (bash 4) vs bash 3.2 — fix carried from #971, owner claude-1 | claude-1 |

---

# ⚠️ VERIFICATION FAILURES OF MINE TODAY, RECORDED SO THEY ARE NOT REPEATED

1. **Monitor pipeline ended in `cut`**, which block-buffers off a terminal: no event would ever have arrived. Replaced with an unbuffered read loop.
2. **`no tests ran in 0.00s` read as three mutation results** — zsh word-split trap, again. Explicit file lists only.
3. **Base full-suite run carried a stray `-x`** and stopped at the first failure; the pair was void until rerun.
4. **`PIPESTATUS` under zsh** skipped a label step silently. Print the status, never capture it in bash-isms.
5. **A time filter on `$2` let untimestamped continuation lines through — TWICE** (PRICE_AGGRESSIVE dumps, Webull cancel errors). Find the real timestamp by line number before attributing a time.
6. **A failed record regeneration let a trailing `git push` run from the main checkout** against `review-pins` — rejected as non-fast-forward, no damage. Pin chains are now `set -e` with explicit `cd`.
7. **"strategy is not restarted by the OMS deploy"** — wrong for the deploy script path; codex corrected it.
8. **"the software −8% and the flatten cover after hours"** was stated before the stand-down and the router were read. It held up, but it was stated first and read second.
