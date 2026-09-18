# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-17 20:55 ET.** Batch `2026-09-17-momentum-first-session-crash-form-t-six-prs-deployed`.
Integrator for this rotation; `codex-2` executed every merge and deploy and reviews this PR. The author never reviews.

> ⛔⭐⭐⭐ **RECLAIM1 IS LIVE, unchanged.** `MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true` read from the running v2
> (pid `2549985`, `/proc/…/environ`, 16:22 ET). One trade per ATR segment, first resting entry only, no reclaim. A hard-stop exit does
> NOT release the slot (operator ruling 09-16). ⛔ **Consequence seen live 09-17 (KXIN): a BUY flip that closes within one bar of a
> confirmation exit has NO entry path** — the reset only re-enables a REST, and a rest exists only while ATR is SHORT. Census: 1 of 6
> confirmation exits since 09-05 had the flip within 2 bars; a "buy the flip" rule would have lost 3 of the 5 times it fired. **Finding,
> not a task** — reopens on a second ≤2-bar instance.

---

# ✅ PRODUCTION — main and box IN SYNC at `f9233366` on runtime code (main is `68dc1384` = `f9233366` + #1003, an OFFLINE study module)

| | |
|---|---|
| box (deployed) | **`f9233366e8681c4e8fe34afaf2be138c94796776`** — read FROM THE BOX 2026-09-17 16:22 ET, branch `main`, clean; pulled 16:09:44 ET |
| GitHub main | **`68dc1384`** — one commit ahead: #1003 `src/project_mai_tai/backtest/momentum_live_rule_baseline.py` + its test + `docs/review-artifacts/…` — a `python -m` study, no service imports it. This handoff PR is docs-only: no sync, no restart |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-18` · `EXPECTED_SHA=f9233366…` · `EXPECTED_PID=2549985` · `EXPECTED_START='Thu 2026-09-17 20:14:30 UTC'` · `SNAPSHOT=/home/trader/restart-evidence/v2-before-corrective-20260917T201400Z.json` · `--restarted schwab-1m-v2` · `--expected-alembic-head 20260916_0021` · `--no-schema-change` (read 16:22 ET) |
| open PRs | **none** at 20:55 ET besides this handoff. #1000 and #1003 both merged after review |
| exposure | after-restart evidence 16:14 ET: **open managed rows 0 · nonzero account-position rows 0 · both live accounts flat before and after** |
| merges 09-17 (**7**) | **#997** Momentum heartbeat crash loop · **#998** per-bar suppressed-rest line · **#999** Momentum Form T eligibility · **#1000** Momentum 20% trigger + fresh-move re-entry + shared tape · **#1001** 24/7 service restart-storm paging · **#1002** ntfy delivery receipts · **#1003** 30-session live-rule baseline harness (offline) |
| migration | `alembic_version = 20260916_0021`, unchanged (no schema change today) |

## Restarts — all 09-17 after the close, `NRestarts=0`, 0 tracebacks after start (verified on the box 16:22 ET)

| service | pid | started (ET) | why |
|---|---|---|---|
| **schwab-1m-v2** | **2549985** | 16:14:30 | #998. Restarted TWICE: the first landed exactly on a bar timestamp and the strict bar-continuity checker could not bracket it; `codex-2` took a fresh flat snapshot and repeated it — evidence **9/9 PASS without a waiver**, BOOT-HOLD released 16:14:37 `reconstructed_uncapped=0` |
| **momentum-paper** | **2548204** | 16:10:00 | #997 + #999 + #1000. Was crash-looping 03:55–06:20 ET (1,244 restarts); stopped by hand 06:20 ET on operator yes; started with the deploy. **Publishes NO heartbeat after the close by design** — first real proof is **03:55 ET 09-18** |
| oms 2270050 · control 2273848 · strategy 2274173 · reconciler 1626620 · market-data 2202865 · market-capture 2202817 · orb 2051823 | — | unchanged | — |

## After-restart evidence (`v2_restart_evidence.py`, run by `codex-2` 16:14 ET, re-read by `claude-1` 16:22 ET)

**9/9 PASS.** Restarted 1/1 · not-restarted unchanged · flat before/after · running-process flags 5/5 (first-entry true, reclaim false,
seed false) · REST warmup · BOOT-HOLD released · bar continuity 0 gaps spanning the restart · tracebacks 0. Fleet health `latest.txt`
**18/18 GREEN** at 16:20 ET (nine services × inactive/restart-storm rows). Notification self-test HTTP 200, receipt `Oj26jnaYWncH`.

## What is LIVE tonight and what to READ tomorrow (owner · first read)

| change | live? | first evidence to read | owner |
|---|---|---|---|
| **#997 heartbeat `degraded` while the feed is closed** (was `waiting`, an invalid literal ⇒ crash loop) | live (unit 16:10) | **03:55–04:15 ET:** MainPID stable, NRestarts flat, 0 `ValidationError`, heartbeat `degraded` then `healthy` once `T.*` connects. If the unit dies, **#1001 pages** (inactive or >3 restarts/5 min) | codex-2 |
| **#999 condition 12 (Form T) NEUTRAL** — the old rule rejected 100% of pre-market prints (0/100,162 DAIC) | live | **06:30 ET:** eligible/total prints, excluded BY CODE (37 should dominate; 12-alone must be zero), and whether live `T.*` frames carry `x` and `trfi` (count of None) | codex-2 |
| **#1000 20% trigger, no cooldown, fresh post-exit low required, shared evidence tape keyed (id, exchange, trf, t)** | live | **09:35 ET:** detections/fills/NO_FILL/UNANSWERABLE/targets/stops per bot; PATH rows vs prints in the union of event ranges; `tape_key_collisions`; unit RSS during any squeeze. Detector status reads **`UNCALIBRATED`** until the baseline exists | codex-2 |
| **#998 `[V2-RESTING-SUPPRESSED-BAR]`** — one line per eligible bar while a segment's first slot is consumed (the deduped `[V2-RESTING-SLOT-CONSUMED]` is untouched; SLOTCLEAR1 unaffected) | live (v2 16:14) | lines per suppressed segment vs that segment's eligible bars. Four boot-capped SHORT segments exist tonight (AEMD, DAIC, MNOV, NUWE) but the resting window is closed ⇒ **UNEXERCISED**; first reading in the first in-window consumed segment | claude-1 |
| **#1001 fleet-runtime paging** — nine units, per-service fingerprints, expiring `maintenance.txt`, root cron `*/5 * * * *` (trader line removed) | live all day | any page names one unit and one condition; a planned stop must be entered in `/home/trader/fleet_health/maintenance.txt` (`<unit> <until-ISO-UTC> <reason>`) or it pages | codex-2 |
| **#1002 `[NTFY-DELIVERY]` receipts** — the known-defect watch pages only through this sender | live | every page attempt logs `accepted= http_status= message_id=`; a non-2xx stays un-acked and retries | codex-2 |
| **#992 mirror lag / shape guard** (deployed 09-16) | live | **first read 09-17:** 16 mirror attempts — 9 accepted (lag 402–1,556 ms, one >1 s), 5 rejected at Webull (TURB ×2, AEMD ×3), **2 `abandoned_no_fresh_quote`** (TURB 09:45, AEMD 10:10) ⇒ the 2,000 ms fallback revisit is now live, owner claude-1 | claude-1 |
| **#993 SHORT-segment seed cap** (deployed 09-16) | live | 6 caps since the 09-16 restart, 6/6 with the SELL older than the watch start; 0 RED. Every restart caps every seeded SHORT name (5 at 19:43 ET 09-16, 4 at 16:14 ET 09-17) — expected, resets at the 04:00 roll | claude-1 |

## ⛔ MISSED TODAY — trades and tasks, so tomorrow can start on them (operator asked for this list)

**The day's live tape (fills, both accounts, 09-17):** Schwab v2 **7 round trips, 4 wins, median +2.92%, gross +$1.16** (qty 2) ·
Webull fan-out leg **11 round trips, 7 wins, median +4.94%, gross +$1.43** (qty 1). Same-second entries diverged across brokers three
times (AEMD 13:27 Webull +5.07% / Schwab −2.44%; DAIC 11:16 +5.56% / −1.95%; DAIC 08:50 +4.84% / +2.92%) — the CONF3 dispersion class.

| missed | what it cost | why | tomorrow's task (owner) |
|---|---|---|---|
| **KXIN 09:54 ET BUY flip** — 1.68 → 1.95 (+16%; +5% was inside the flip's own next bar) | one +5% winner on the Webull leg; Schwab could not trade it at all | Webull rest filled 2 bars early → confirmation exit sold −1.5% → owner reset 09:53:24 → the real flip closed 38 s later; a reset only re-enables a REST and rests need a SHORT bar; reclaim is OFF | **none** (finding, 1 of 6 in two weeks; a buy-the-flip rule lost 3 of 5). Reopens on a second ≤2-bar instance (claude-1) |
| **KXIN + TURB on Schwab** — 5/5 and 25/25 opens rejected "must be placed with a broker" | every Schwab entry on both names; Webull took TURB 3 trades (−1.5%, +4.9%, −7.8%) | Schwab-restricted symbols; we resend every bar | intake spec: after the first such reject, mark the symbol Schwab-ineligible for the session; log it (claude-1 spec → codex-2 build) |
| **Momentum 30/60 — the entire first session** (04:11–09:30 ET) | 0 detections on a morning with AEMD 1.43 → 14 pre-market | 03:55 crash loop (#997) and, underneath it, the Form T rule that rejected 100% of pre-market prints (#999) | both deployed 16:10 ET; **first real session is 09-18** — reads at 03:55, 06:30, 09:35 ET (codex-2) |
| **AEMD before 10:45 ET** — 10 Webull `PRICE_AGGRESSIVE` rejects (10:03–15:06), 22 Schwab rests cancelled by reprice before the first fill | unknown; the first fill came at 10:51 after the 10× move | the resting stop chased a fast tape; Webull refuses stops far above the ask | the PRICE_AGGRESSIVE distance census from #976 fields is still owed (codex-2, unchanged from 09-16) |
| **USDE** — 5 Webull mirrors `NO_FRESH_QUOTE`, 11 Schwab rests cancelled, no fill | none proven | the #992 fallback abandons when the OMS snapshot is stale | read the abandon lines' quote ages vs the 2,000 ms fallback (claude-1) |
| **One Webull round trip per Schwab trade is the design; sizes are qty 2 / qty 1** | — | — | no task; noted so the +$ figures are read as percentages |

**Tasks planned today and NOT done (carry):** the one-week broker-refusal census (147 `event_source='broker'` rejects since 09-10 —
the week is complete, census not run; claude-1) · the late-close-after-broker-fill guard (needs the operator's yes; codex-2) ·
`abandoned_no_fresh_quote` fallback read (claude-1) · Schwab-ineligible-for-session spec (claude-1) · seed-cap-per-restart measurement
(claude-1) · Momentum 30-session baseline (running; codex-2) · the `momentum-paper` `_publish_state()` exception guard (one publish error
still kills the unit; codex-2, never started).

## The 30-session baseline (#1003) — RUNNING, results UNSEEN

`codex-2` is capturing 30 sessions ending **2026-09-16** (after-close only, ~23.5k API calls/session, 3/30 at 18:35 ET, 0 rate-limit
lines on the live gateway). Frozen before capture: `DETECTOR_SUSPECT` = detections > 10; **more than 3 of 30 suspect sessions rejects
the band and recommends nearest-rank P95 (never below 10)**; 09-17 was already seen (14/16 detections, 29/30 fills on AEMD) and is
EXCLUDED from calibration. ⛔ Neither agent reads a partial report; the replay runs only at 30/30. The study changes no runtime; the
operator decides.

## Rulings and approvals made today (operator)

1. **Both deploys after hours** — #997 was not deployed intraday even though Momentum was down (half its window was already lost).
2. **Stop the Momentum crash loop at 06:20 ET** — a full-market Massive subscription was being opened and dropped every ~7 s on the key the live gateway shares.
3. **Condition 12 neutral** — accepted as a defect repair of the pre-registered rule (the study used Massive 1-second bars, which count Form T prints).
4. **20% trigger with immediate fresh-move re-entry** — recorded in `docs/momentum-paper-bot.md` as operator-approved; `codex-2` merged #1000 on it. ⚠ `claude-1` did not witness the approval directly; it was asked twice and not contradicted.
5. **Close-out tonight** — this PR.

## Open items — each with an OWNER and a NEXT ACTION (nothing boards without both)

| item | owner | next action |
|---|---|---|
| **Late software close after a broker leg already filled** — the RESERVE1 "recurrences" 09-14..09-16 (BMGL ×1, VEEA ×20, DLXY ×4, ZTG ×5, reject `NEW_NO_POSITION_…_CAN_NOT_SELL_SHORT_FOR_LT_2K`) all hit an ALREADY-FLAT Webull position: broker OCO stop/target filled first (VEEA 13:49:20), our sells came 1–6 s later (13:49:21–29), our `oco_exit` row was written 54 s late. **Zero harm.** ⛔ The only thing stopping these sells from OPENING A SHORT is the <$2k margin rule in the refusal text | operator → codex-2 | operator decides (asked 18:35 ET): treat a `NEW_NO_POSITION` reject as terminal for the close episode + one position re-read; split the watch row by evidence (LATE_CLOSE_AFTER_BROKER_FILL vs a true reservation). Codex builds on yes, claude reviews |
| RESERVE1 watch row title is a WRONG REASON for 30/30 of its recent rejects | codex-2 | part of the item above; until then a RESERVE1 RECURRENCE means "late close", not "reserved shares" |
| `abandoned_no_fresh_quote` ×2 on 09-17 (TURB 09:45, AEMD 10:10) | claude-1 | read the abandon lines' quote ages and the 2,000 ms fallback; propose or close after 5 sessions of denominators |
| Schwab-restricted names (KXIN 09-17 5/5 "Opening transactions for this security must be placed with a broker"; ZTG 09-16) — we keep sending opens Schwab will refuse | claude-1 | intake spec: after the first such reject, mark the symbol Schwab-ineligible for the session (per-broker eligibility design exists); observability + no new order path |
| `[V2-CW-SEED-CAP]` fires on EVERY seeded SHORT name at EVERY restart (watch_start = boot) | claude-1 | measure only: count caps per restart vs names that later printed a watched SELL; no rule change without that number |
| Momentum 30-session baseline | codex-2 | finish capture 30/30 → replay off-box → report.md + report.json + tape sha256 list + band verdict verbatim |
| Momentum unit memory: the 600 s tape-fingerprint map (~180k entries at AEMD rates) | codex-2 | report peak RSS on the first live squeeze |
| ~20 other `ops/health` senders still log no ntfy receipt | none (parked) | only if a page is disputed again |
| G5 parity five bad days · ZTG ASK_PAST_BAND 0.5% band · 07:00–07:08 blind window · Webull PRICE_AGGRESSIVE distance census · post-close hand-cancel of one leg | unchanged from 09-16 | see 09-16 handoff-log entry; none moved today |

## Pre-open 09-18 (`preopen.sh` pins moved by `codex-2` 16:14 ET, verified by `claude-1` 16:22 ET)

Run the gate and the grades as usual. Expect: `EXPECTED_SHA=f9233366`, v2 pid `2549985`, snapshot `v2-before-corrective-20260917T201400Z`,
no waiver line, BOOT1/SEED1 green. ⛔ **All seven pins move on every restart** — this morning's gate failed because only four of them had
moved (stale snapshot, stale `--restarted` list, stale schema mode). Expected 03:55 ET: Momentum prepares its session and stays up;
its heartbeat reads `degraded` until `T.*` connects — that is the fix working, not a fault. Any `[V2-CW-SEED-CAP] … SHORT` line before
07:00 on a 04:00-watchlist name is still a RED.
