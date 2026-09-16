# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-15 20:05 ET.** Batch `2026-09-15-blind-window-selection-census-watch-fixes`.
Integrator for this rotation; `codex-2` executed both deploys and reviews this PR. The author never reviews.

> ⛔⭐⭐⭐ **RECLAIM1 IS LIVE.** `MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true` read from the running v2
> (pid `1628896`) at 19:51 ET. One trade per ATR segment, first resting entry only, no reclaim.

---

# ✅ PRODUCTION — main and box IN SYNC at `7bdcbd0` (everything merged today is DEPLOYED, nothing restarted)

| | |
|---|---|
| box (deployed) | **`7bdcbd004a019c60124b8f00e467fa3b3146c798`** — read FROM THE BOX 19:46 ET, branch `main`, clean. Two sync-only deploys today: `f2d45d4` at 05:40 ET (#982), `7bdcbd0` at ~19:35 ET (#983 #984 #985 #986). Both executed by `codex-2` on operator GO |
| GitHub main | **`7bdcbd0`** — identical. Merging this handoff PR moves main ahead by docs only — DOCS-ONLY DIVERGENCE rule holds; no sync, no restart |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-16` (bumped by `codex-2` ~19:50 ET, read back from the box 20:20 ET) · `EXPECTED_SHA=7bdcbd004…` (bumped with the sync) · `EXPECTED_PID=1628896` · `EXPECTED_START='Mon 2026-09-14 21:40:48 UTC'` · snapshot `v2-restart-before-20260914.json` unchanged · report `v2-restart-evidence-20260915.md` · still `--expected-quiet-service reconciler` |
| open PRs | **none** — this handoff PR excepted. #987 (codex draft, first-loss follow-through) was CLOSED at 23:44 UTC and its branch deleted; nothing from it is carried |
| exposure | **19:51 ET: open managed rows 0 · nonzero broker positions 0** (gate snapshot instrument 17:21 ET: 2/2 accounts, 0/0) |
| merges 09-15 (**5**) | **#982** BARCONT1 live-at-stop floor · **#983** overlap.sh bash-3.2 patch package (docs) · **#984** PRE07 shadow report · **#985** PHANTOM1 read-time clock + row reasons · **#986** selection census (pre-registered, result FAIL) |
| local toolkit | `~/.claude/mai-tai-fleet/overlap.sh` patched from #983 and RE-PINNED 09:10 ET (sha `c10ba936…`); `board.sh overlap` runs on Apple bash 3.2 |

## Services — no restart today; all five still from 09-14, `NRestarts=0`

| service | pid | started (UTC) |
|---|---|---|
| reconciler | 1626620 | 09-14 21:34:21 |
| oms | 1627713 | 09-14 21:37:08 |
| strategy | 1627724 | 09-14 21:37:08 |
| **schwab-1m-v2** | **1628896** | 09-14 21:40:48 |
| control | 1629380 | 09-14 21:41:28 |

⛔ The 09-14 pre-restart snapshot still holds `open managed rows=1` (BMGL hand sale). **Every gate run prints that one FAIL until the
next restart writes a new snapshot.** Ruling unchanged: green ⇔ the ONLY failed row is "Both live accounts flat before/after" with
before rows=1, after 0. Do not edit the snapshot.

---

# FLAGS — read from the RUNNING v2 (pid 1628896, 19:51 ET)

| flag | value |
|---|---|
| `…FLIP_OWNED_FIRST_ENTRY_ENABLED` | **true** (RECLAIM1) |
| `…CONFIRMATION_ACCOUNT_NEUTRAL_DISCOVERY_ENABLED` | true |
| `…CW_V2_RECLAIM_ENABLED` | false |
| `…TICK_CAPTURE_ENABLED` / `…TIMESALE_CAPTURE_ENABLED` | true / **false** (TIMESALE never tried; the only Schwab-side lever for pre-07:00 bars — operator has NOT asked for it) |
| `…ATR_FLIP_PROBE_SYMBOLS` / `…MACD_PROBE_SYMBOLS` | `*` / `*` |

---

# ⭐⭐ WEDNESDAY 2026-09-16 PRE-OPEN

1. **~06:30 ET:** `ssh mai-tai-vps /home/trader/preopen.sh`. `EXPECTED_DATE` is already `2026-09-16`.
   Green = only the known flat-before FAIL. **Bar continuity is now floored at the stop (#982):** a symbol that left the watchlist before a
   restart and rejoined after no longer reads as a restart hole (`bracketing pairs NOT live at stop … excluded=N` on the row).
2. **PHANTOM1 now names its reason** (#985): a COULD_NOT_TELL page carries `reasons=[acct:sym VERDICT: …]`. The 09-15 09:20 page was a clock
   race (run-start clock vs a fresh Webull mirror write), fixed. A repeat with a reason string is a real finding; without one is a regression.
3. **The 07:00–07:08 blind window is STRUCTURAL, not a bug** (memory `project_mai_tai_0700_blind_window`): Schwab CHART_EQUITY yields ZERO
   bars before 07:00 (0/10 sessions since 09-01; earliest live v2 bar ever = 07:00:00 since May), the 04:00-sliced ATR needs ~2×5 bars, so the
   first live probe is the 07:08 bar on every one of the six live days checked. A cross inside 07:00–07:07 is never a flip. Any lever is a
   RULE change (operator). PRE07 ten sessions found ONE such flip (MYSZ 09-15 07:05) and it would have lost (MFE +0.4% / MAE −9.6%).
4. Session-local tape monitors DIED with this session. The known-defect cron (every 5 min, 03:50–20:15 ET weekdays) and GATE1 are the
   persistent watches. RESERVE1 will still read RECURRENCE=20 until the 04:00 anchor rolls — that is today's VEEA storm, see below.

---

# ⛔⭐⭐ TODAY'S TRADE TRUTH — 22 legs / 15 decisions, all closed flat, actual −42.8 pp (8 wins, 14 losses)

Schwab refused MYSZ and SUGP openings all day ("Opening transactions for this security must be placed with a broker" → `schwab_ineligible_cached`),
so every MYSZ/SUGP fill is the Webull qty-1 leg. VEEA, IPW, MEDS filled both legs.

| decision (ET) | legs | result | exit path |
|---|---|---|---|
| MYSZ 08:53 @2.44 | W | −0.3% | ATR flip 09:25 (first entry of the day; bought a bounce in an all-day fade) |
| MYSZ 09:35 @2.465 | W | −7.9% | broker OCO stop 09:57 |
| SUGP 09:35 @1.15 / 09:43 @1.15 | W / W | −4.8% / **+5.2%** | confirmation exit 09:37 / broker target 09:44 (day high) |
| **MYSZ 11:19 @2.30** | W | **+5.0%** | ⛔ **WICK FILL, NOT A FLIP**: scanner re-CONFIRMED MYSZ 11:17:58 after the 10:15 FADE → bot rested first-slot stop 2.3029 at 11:18:02 → 11:19 bar wicked to 2.33 and closed 2.29 → confirmation exit FIRED 11:21 but the Webull close was **REFUSED `pair_cancel_unconfirmed`** (Webull 429s) → 11:22 bar flipped BUY anyway → held → target 11:39 |
| VEEA 11:47 @5.82/5.83 | S+W | +5.0 / +5.0 | broker targets 11:51 |
| MYSZ 12:08 / 12:44 / 12:51 | W | −9.1 / −2.2 / −6.8 | stop / confirmation exit / flip |
| IPW 13:21 @3.15/3.16 | S+W | −7.9 / −7.9 | stops 13:33 |
| VEEA 13:35 @6.13 | S+W | −8.1 / −8.2 | broker stops 13:49 — ⛔ **20 Webull sell rejects in 8 s**, see OVSD1 below |
| IPW 14:01 @2.87/2.88 | S+W | −1.9 / −2.1 | out 14:03 (symbol left the watchlist 14:02) |
| VEEA 14:19 / 15:12 | S+W | +2.7 / +5.0 · +4.6 / +5.0 | targets |
| MEDS 15:39 @1.90 | S+W | −4.5 / −8.4 | confirmation exit / stop |

**Operator what-ifs (one day, n=22, bar high/low fills, NOT rules):** +3/−5 → −28.0 pp · +2/−8 → −49.7 pp · same simulator on +5/−8 → −48.6 pp vs
actual −42.8 (the 6-pp gap = the confirmation exit and the pairs, which the simulator does not model). **The stop is the lever, the target is
not, and neither changes that 14 of 22 legs went the wrong way on the first bar.** That is selection — see the census.

## ⛔ OVSD1 — 5th instance, 1st on Webull, the #608 CEILING exercised for the first time (VEEA 13:49 ET)
Webull OCO **stop leg filled 13:49:20.956 @5.63** (−8.2%). **130 ms later** the software CW_HARD_STOP decided at the same level;
`OMS-CANCEL-PAIR-REQUEST requested=2 confirmed=0` → UNCERTAIN, yet the close emitted (stand-down fails OPEN for Webull) → **20 market sells
rejected `NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT` 13:49:21–13:49:29** → `OMS-V2-EXIT-REJECT-CEILING` at 20 stopped the loop →
`EXIT-STAND-DOWN` ×30 → 13:50:14 OCO fill recorded, row resolved. Schwab leg same minute: stand-down HELD, 0 rejects. No exposure. This is
what RESERVE1 `recurrence=20` is. ⛔ codex first read it as "historical, unrelated" — corrected on the PR. **Design fact:** while a Webull pair
is attached at −8%, the software hard stop at −8% is REDUNDANT and can only race it. Owner: codex pair-release lane (parked OVSD1).

---

# ⭐⭐ RESEARCH SETTLED TODAY (all read-only, all pre-registered before results)

| question | answer | where |
|---|---|---|
| Why no MYSZ entry after the 07:05 chart cross? | **Structural blind window 07:00–07:08**, every day, every symbol (above). Not the broker. | memory `project_mai_tai_0700_blind_window`, PRE07 report in #986 |
| Does "old high + faded" predict a failed +5%? | **NO.** 683 canonical BUY flips / 855 confirmed symbol-days since 07-09: blocked 185/440 = 42.05% vs kept 104/243 = 42.80% → pre-registered FAIL; **all 9 pre-flip features non-monotonic.** Base rate: **42.3% of bar-close BUY flips reach +5% before the next SELL.** Do NOT add a fade / high-age / range / ER / volume filter on this evidence. | `docs/confirmed-selection-census.md`, `analysis/reports/confirmed-selection-census-2026-07-09-to-2026-09-15.md`, memory `project_mai_tai_selection_census_fade_block_failed` |
| ⛔ UNIT MISMATCH the census exposed | **Live fills are rests at the trail that fill BEFORE the flip bar closes.** 588/858 logical fills matched no canonical flip even at [flip, flip+2m); SUGP 09:43 won +5.2% while the canonical 09:44 flip was a miss; MEDS filled twice with no canonical BUY flip. The census answers the FLIP question; the live-book question needs a **live-fill census** (entry = fill price) — recorded on #986 for pre-registration, not built. | #986 comments |
| Confirm path | PATH_C_EXTREME_MOVER on 2005/2020 memberships — the scanner confirms names that have ALREADY moved (MYSZ confirmed at 00:00 already +44.5%; SUGP at 08:17 already +53%). | census population table |

---

# ⭐⭐ STATUS SPLIT — EXERCISED vs UNEXERCISED

| ✅ EXERCISED today | ⛔ still UNEXERCISED |
|---|---|
| **#977 LATCH1** — Webull-only fills recognised every time (MYSZ ×6, SUGP ×2): `V2-FLIP-OWNER-FILL account=live:orb`, no phantom `flip_no_fill` cancels | **#979 restored-owner recovery path** — FTFT/BMGL were RELEASED at 04:00 by the ROLL (`reason=session_reset_flat`), not by recovery |
| **#980 BOOT1** — 03:50 ET run GUARD_WORKING, state.json `delivered=false` | **19:55 flatten on a real Webull share**, **EOD1601**, **GATE1** shape, **Schwab-only sub-bar latch** |
| **#982 BARCONT1** — gate re-run 05:42 ET: brackets 3 live / 0 spanning / 1 excluded | **TIMESALE pre-07:00 measurement** (flag exists, OFF, never tried; operator has not asked) |
| **#608 reject CEILING** — first firing on Webull, stopped the storm at 20 | **Live-fill census** (pre-register first) |
| **#945 account-neutral confirmation discovery** reached the Webull leg (MYSZ 11:21) — and was refused `pair_cancel_unconfirmed` → the refusal is the SAFE side, the lane is OVSD1 | |
| **#985 PHANTOM1** verified on the box after the sync (0 open rows) | |

---

# ▶ CARRIED — open, read-only

| item | question | owner |
|---|---|---|
| **OVSD1 — Webull stand-down fails OPEN** | 5th instance today (VEEA 13:49); software hard stop races the attached pair; CEILING bounds it at 20. Also the confirmation-exit `pair_cancel_unconfirmed` refusal (MYSZ 11:21) is the same reservation class | codex-2 (pair-release lane, parked) |
| **Live-fill census** | same instrument as #986, unit = logical live fill, entry = fill price; widen the descriptive match to [flip−1m, flip+2m) | codex-2 (pre-register; claude-1 reviews design first) |
| **Webull PRICE_AGGRESSIVE escalation** · **reprice window** | carried from 09-14 | codex-2 |
| **Schwab-only sub-bar latch shape** | held gate is sampled per bar | unassigned |
| **pre-close entry cutoff** · **07:00–07:08 blind window** · **exit geometry (+3/−5)** | all RULE changes; evidence on file, no build without a ruling | **operator** |
| confirmation-exit race · STOPMKT probe · PROV1 · 0.5% band · F6/F8/F9/F10 · BNC · claim overhang · fan-out size · ELIG · AMEND1 · REJ1 silence | carried unchanged | unassigned |
| ~~`board.sh overlap` on macOS~~ | **CLOSED** — #983 merged, patch applied and re-pinned 09:10 ET | — |

---

# ⚠️ VERIFICATION FAILURES OF MINE TODAY, RECORDED SO THEY ARE NOT REPEATED

1. **`cut -c1-200` chopped `state=` off the probe lines** → my first instrument check read "07:08 live probe is missing". Untruncated → agree. Never truncate a line you are about to parse.
2. **Mutation harness restored with `git checkout --` before the fix was committed** → reverted my own fix and printed a false "4 failed". Commit first, then mutate; restore from the commit or an explicit saved copy.
3. **zsh word-split: two test paths in one `$T` string** → `no tests ran in 0.00s` and one spurious ruff error. Explicit arguments only (third time this trap is recorded).
4. **First fixture for the overlap rule used a path that does not exist in the repo** → false clean of my own fixture. Rule 1 needs a tracked file; recorded in the #983 report.
5. **The ritual's `git pull --ff-only origin main` ran in the main checkout, which is codex's worktree** (branch `codex/fix-seed-cap-fresh-sell-reset`, marker `codex`). Harmless (branch already inside main) but it moved codex's branch pointer. Use a fresh worktree for every read that needs origin/main.
6. **I first told the operator "eight decisions" for the eleven +2/−8 losers** — that count was of the losing subset, not the day. The day is 15 decisions / 22 legs.
