# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-21 (Mon) 18:31 ET.** Batch `2026-09-21-webull-false-flip-shared-path-pager-live-thirteen-prs`.
Integrator for this rotation. `claude-1` authored #1018 #1020 #1021 #1023 #1024 #1025 #1028 #1030 (`codex-2` reviewed);
`codex-2` authored #1013 #1019 #1022 #1026 #1027 (`claude-1` reviewed), executed every merge and the deploy, and reviews this PR.
The author never reviews.

> ⛔⭐⭐⭐ **OPERATOR'S STANDING LINES.** (09-18) Every bug ⇒ sweep the REAL history for its class; every exit must END in a named
> safe state — SOLD, or PROTECTED-AGAIN + PAGED. No freezes — fix forward. (09-21) **"An increment that forces its own rewrite
> changes nothing"** — build the SHARED path, siblings are call sites flipped next. **The uncovered-share page is never cuttable.**
> Count EPISODES before ranking a refusal total (the "58 refused hard stops" were 5 episodes, 1 real miss).

---

# ✅ PRODUCTION — RUNTIME SHA on the box is `2a4d444a`; GitHub main = `2a4d444a` + DOCS-ONLY commits (this handoff PR and later docs PRs)

| | |
|---|---|
| box (deployed) | **`2a4d444a422d7784aed3604c4a0e7c3fca9ef7a1`** — read FROM THE BOX by `claude-1` 2026-09-21 18:27:38 ET, clean (`dirty=0`) |
| GitHub main | runtime-identical to the box **as of this deploy**. ⛔ The old "main = box + docs only" invariant was **FALSE all day 09-21** (main ran up to 28 non-docs files ahead of `d6a6f59a` while PRs merged intraday). It is true again tonight. **Ritual step 4 must still RUN the diff** (`git diff --name-only <box-sha> origin/main`), never assume it |
| gate pin | `/home/trader/preopen.sh` (set by `codex-2`, read by `claude-1` 18:27 ET): `EXPECTED_DATE=2026-09-22` · `EXPECTED_SHA=2a4d444a…` · `EXPECTED_PID=2549985` · `EXPECTED_START='Thu 2026-09-17 20:14:30 UTC'` |
| restart evidence | `codex-2` after the deploy: **PASS 9/9** (not re-run by `claude-1`) |
| exposure | 18:20 ET: **non-zero account-position rows 0 · open managed rows 0 · both live accounts flat**. 09-21 fills: live:orb 16 buys / 16 sells · live:schwab_1m_v2 20 / 20 |
| flags | From `/proc/<oms pid>/environ` 18:27 ET: `…WEBULL_MIRROR_DEFERRED_RESUBMIT_ENABLED=true` (PA1) · `…EXIT_RELEASE_RESERVATION_ENABLED=true` · `…DUAL_BROKER_FANOUT_ENABLED=true` · `…WEBULL_MIRROR_ENABLED=true` · `…WEBULL_MIRROR_EH_ENABLED=true`. **LC1 late-close guard: still OFF** |
| Webull size | **1 share** (operator 09-21, row closed) |
| merges 09-21 (**13**) | **#1018** hard-stop design (docs) · **#1013** Momentum baseline ordering · **#1019** deploy-gate bootstrap · **#1020** four test pins · **#1021** PROTECT-FAILED forensic + API budget (docs) · **#1022** Webull SDK log redaction · **#1023** Schwab `limit == stop` analysis (docs) · **#1024** one-tick limit lift · **#1025** live-failure write-up (docs) · **#1026** production fixtures · **#1027** status-read starvation · **#1028** shared cancel-then-sell + uncovered-share page + pager source · **#1030** sync loop uses the rotating list |
| deployed tonight | everything above that is runtime: **#1019 #1020 #1022 #1024 #1027 #1028 #1030**, one deploy, operator GO *"go ahead and deploy now"*, `codex-2` executed 16:52 ET |
| migration | `alembic_version = 20260916_0021`, unchanged |
| **pager** | ⛔ **NEW FACT:** the INC1 pager is an INSTALLED, sha-pinned COPY (`/home/trader/unexercised_watch/watch.py`, root cron, `* 11-21 UTC` = 07:00–17:59 ET). It was repo version `313ea80` — three versions stale, reading ONE source — so **8 critical exit-seam incidents since 09-15 reached the phone 0 times**. Re-installed tonight from `2a4d444a` (sha `384a7e37…` == repo file, 3 crontab guards re-pinned). **First end-to-end proof ever:** 8 of 8 delivered, the eight closed by id, rerun `NO_OPEN_INCIDENT open=0` (22:00:15Z). **Merging `ops/health/unexercised_watch.py` never installs it** |

## Restarts (all `NRestarts=0`; 0 tracebacks after start for oms — verified on the box 17:52 ET)

| service | pid | started (ET) | why |
|---|---|---|---|
| **oms** | **3576222** | Mon 09-21 16:52:43 | deploy `2a4d444a` |
| **strategy** | **3576233** | Mon 09-21 16:52:43 | OMS deploy restarts strategy |
| schwab-1m-v2 2549985 · control 2273848 · reconciler 1626620 · market-data 2202865 · market-capture 2202817 · momentum-paper 2704889 · orb 2051823 | — | unchanged | — |

## What is LIVE and what to READ Tuesday 09-22 (owner · first read) — report the numbers to the operator UNPROMPTED

| change | first evidence to read | owner |
|---|---|---|
| **#1028 Webull false-flip exit on the SHARED `_webull_cancel_then_sell`** (abortable checks → irreversible release → sell + terminal-state guarantee; ONE quote-freshness decision, before the release) | **Report the FIRST real Webull confirmation exit within minutes.** `[OMS-WEBULL-CANCEL-THEN-SELL] exit=CONFIRMATION_EXIT … outcome= protection= seconds=` — every run must end `closed / close_submitted / resolved_by_fill / flat / reprotected / uncovered`; `[OMS-V2-CONFIRMATION-EXIT-QUOTE-AGED]` gives the quote age that was never logged before. **Pass mark: `EXITDONE1` live:orb gap = 0 over 5 EXERCISED sessions.** 09-21 baseline on the old code: live:orb fired 6 / sold 1 / reprotected 1 / **gap 4**; schwab 9 / 9. Status: **DEPLOYED, UNPROVEN** | claude-1 |
| **#1028 uncovered-share page** (`_check_webull_uncovered_shares`, RTH only, 30 s) | `[OMS-WEBULL-UNCOVERED-SHARE] status=PAGE cause=pair_released_not_sold \| reattach_failed \| never_protected`, then `INC1_STATUS.txt` `open>=1` **and** `delivered_now>=1`. Row written ≠ delivered. It cannot see: a fill the OMS has not detected; pre-market / after-hours (a YMAT 09-09 repeat is still silent). A release whose sell is still working after 30 s also pages — true statement | claude-1 |
| **#1027 + #1030 status-read rotation + list-today fallback** | time from a Webull fill to `[OMS-V2-MANAGED-OPEN]` (09-21: NCPL ~786 s, GRML ~255 s unseen); `codex-2`'s per-endpoint call counter vs where the 429s land | codex-2 (claude-1 reads the OPEN lag) |
| **#1024 one-tick limit lift** (`_lift_collapsed_schwab_stop_limit`) | Schwab STOP_LIMIT submissions with wire `limit == stop` should be 0 (was 45 of 532 since 09-01) | claude-1 |
| **#1022 SDK log redaction** | `x-app-key` / signature / escaped `_content` account id absent from `oms.log` after 16:52 ET | codex-2 |
| **PA1 + PA1b** — **PROVEN LIVE 09-21, the only proven item** | 09-21 whole log: queued 8 · resubmitted 8 · accepted 7 · rejected 0 · forgotten 22 · cap reached 0 · **`PRICE_AGGRESSIVE` rejects 0** (baseline 41 in 4 sessions) · no duplicate legs · the v2-cancel drop path exercised. (8 resubmitted vs 7 accepted: the eighth is not reconciled here) | claude-1 (passive 5-session counts due Fri 09-25) |
| **#1005 LC1 late-close guard** | flag OFF. Operator ruling 09-21: **lands TOMORROW** as the cure for BURST4 (redundant sells after the native stop already filled). GRML 15:58 ET 09-21 = a live instance: ~9 refused sells in 7 s, native legs filled 9.54 / 9.55, ended flat correctly | operator → codex-2 |
| **#1007 Momentum cool-off** | unchanged; bot still receives no prints until #1029 | codex-2 |

## What happened 09-21 (one paragraph each — the narrative is in `handoff-log.md`)

- **#1014 met its first live day and failed.** 4 of 5 clean Webull releases ended with no sell, no re-protect, no page: GLND 10:18
  (663 s uncovered), GRML 10:34 (621 s), NCPL 13:55 (474 s), GLND 14:20 (3,654 s, left by `CW_FLOOR` at +3.8% — luck). The release
  takes ~2.5 s inline on the serial consumer, then the generic 5 s stale-quote guard ran AGAIN on the same quote and bare-returned
  after the pending decision was popped. A race on quote age: NCPL 15:07 sold because its quote was ~1 s old going in. [pinned for
  GLND 10:18, 5.17 s measured; single-candidate inference for the other three.] #1014's tests used a broker that answers in 0 s.
- **Status-read starvation (defect B).** Open orders polled newest-first, the Webull detail budget ran out mid-pass, the same old
  order drew the 429 every sync: NCPL's fill unseen ~786 s, GRML ~255 s.
- **The pager had never delivered an exit-seam page** (see PRODUCTION row). Every "paged" written before tonight meant "row written".
- **DENOM58 / HSFLOOR answered.** 58 refused Webull hard-stop closes (09-04→09-18) = **5 episodes**: 4 redundant bursts after the
  native stop had filled, **1 real miss — YMAT 09-09 08:41:53 ET pre-market**, 3 refusals then nothing, open 78 min. 44 floor close
  orders = 20 episodes, 10 still untraced (FLOOR10).
- **`claude-1`'s live watch failed four times:** three distinct causes (a block-buffering `cut`; a filter without broker-leg close
  markers; a reconnect loop that gave up after 6 tries) **and one UNEXPLAINED** (13:26–14:03 ET, armed, 0 events, two hypotheses
  tested and refuted). Replaced by a 30 s offset-tracked poller + heartbeat + a 15-min full read (broker positions vs managed rows).
- **Retracted / corrected numbers:** the Schwab `limit == stop` harm "0 of 39" is **RETRACTED** → **0 of 2** measurable (tape
  exists only for 2; ask-through 2/5 with the fill second included, 4/5 at +2 s). `claude-1` told everyone to expect **2** open
  incidents — a 3-day query window truncated it; the pager found **8**. The 09-19 handoff's Webull totals are stale: verified
  09-21 = 1,473 `TOO_MANY_REQUESTS` / 500 status-fetch failures / 77 attach episodes.

## Open items — each with an OWNER and a NEXT ACTION (nothing boards without both)

| item | owner | next action |
|---|---|---|
| **HSFLOOR — `CW_HARD_STOP` + `CW_FLOOR` (then `CW_FLIP`, overnight flatten) still use the old one-try release** | **claude-1** (claim on `oms/service.py` kept) | flip them onto `_webull_cancel_then_sell` as call sites; **YMAT 09-09 is the named case**; the routine already refuses a non-protective reason and a `CW_HARD_STOP` control runs through it |
| **FLOOR10** — 10 floor episodes (CDTG, BNC, NUR, MOBX, SUNE ×2, FTFT, YMAT, DLXY 14:31, DAIC) untraced | claude-1 | how each actually exited (native leg already filled vs share left open) — owed WITH the hard-stop PR, operator is holding me to it |
| **BURST4** — self-inflicted refusals after the native stop filled (5 instances incl. GRML 09-21) | operator → codex-2 | LC1 flag tomorrow; do not re-derive |
| **INCCLOSE** — nothing ever closes an exit-seam incident (five sat open 6 days after the position closed) | claude-1 | small PR: auto-close when the linked managed row closes; until then close by id after each page |
| **The uncovered-share page is RTH-only and blind to an undetected fill** | claude-1 (design) / codex-2 (undetected fill, lane B) | pre-market cover needs a different question (no pair can rest pre-market); raise with the hard-stop PR |
| **Live-watch failure #4 unexplained** | claude-1 | none planned — the replacement does not depend on the cause; reopen if the poller ever misses against a full read |
| Pager install is manual | codex-2 | any PR touching `ops/health/unexercised_watch.py` ⇒ install + re-pin 3 crontab guards as its own verified deploy step; `test_the_pager_delivers_every_incident_source_the_oms_can_write` gates the source list |
| ~165 "order stuck in accepted" noise warnings / day | codex-2 (proposed, not yet accepted) | triage: real vs the rotation's by-product |
| **#1029 Momentum through the gateway** (draft, `898e2c67`, validate green) | codex-2 | population capture = detached one-shot 20:10 ET 09-21 (first attempt 18:04 ET correctly refused `UNMEASURED` 0/3); then the frozen replay in a flat 16:05–20:00 ET window; `claude-1` reviews, reading the CONTROL first. **No promise of Momentum trading 09-22** |
| Momentum test pins left from the 09-19 list (one-probe + connected-during-streak heartbeat · gateway needle `1008 (policy violation)`) | codex-2 | the four non-Momentum pins shipped in #1020; these two stay with Lane M |
| #1003 baseline report | codex-2 | #1013 merged `c3201f81`; the REPORT is still unread |
| Re-attach of a FULL pair while an old leg may still work is ASSUMED refused | codex-2 | NCPL 10:32 09-21 had a SECOND bracket accepted after an unanswerable release — read that tape; it may be the first real answer |
| Passive 5-session counts, due **Fri 09-25** | claude-1 | PA1 8% band (accepted vs refused distance) · `abandoned_no_fresh_quote` · `[V2-CW-SEED-CAP]` in-window caps. Count through Friday, build nothing |
| Schwab-ineligible cache (#992) · G5 parity · blind window 07:00–07:08 | none — parked by the operator | reopen on request |
| Schwab refuses 37 of 39 Webull-only opportunities | operator | unanswered design question; Webull stays at 1 share regardless |

**NOCHASE — closed by the operator 09-21, do not re-raise without a SECOND instance:** Webull issue-table rows #4, #6 (do not build
#1018's rule separately), #7, #9 (count through Friday, build nothing), #11. **Closed rows:** Webull size (1 share) · ZTG 0.5% band ·
hand-cancel procedure · DENOM58 (58 → 1) · #8 PA1 (closed as a win).

## Rulings and approvals (operator, 09-21)

1. **Webull stays at 1 share.** ZTG band and hand-cancel rows closed.
2. `limit == stop` analysis and the four non-Momentum test pins moved `codex-2` → `claude-1`; `x-app-key` log fix → `codex-2`.
3. One-cent (one-tick) limit lift approved as its own small PR (#1024).
4. Naked Webull shares intraday: **(a) keep running, no intraday restart or config stop-gap**; "let the software ladder manage it".
5. **A/B split:** `claude-1` = dropped confirmation exit (A, `oms/service.py`); `codex-2` = status-read starvation (B, Webull adapter).
6. **Tonight's scope = #1 + #3 only**, #1 written as the SHARED path; #5 (hard stop / floor) next and `claude-1`'s. **FALLBACK**
   (direct fix + page if the shared path was not green by 18:30 ET) — **not needed**: #1028 merged 16:31 ET.
7. **REGREEN:** evidence must be re-run on the FINAL code and say so. **YMAT 09-09 must be the named case.**
8. **Pager re-install approved** as its own verified deploy step; **all eight incidents authorized closed** after the proof.
9. **Deploy GO** ("go ahead and deploy now") → `2a4d444a`, 16:52 ET. **LC1 lands tomorrow**, never the same day as another flag.

## Pre-open 09-22 (`preopen.sh` pins set by `codex-2` after the deploy, read by `claude-1` 18:27 ET)

Run the gate and the grades as usual. Expect `EXPECTED_DATE=2026-09-22`, `EXPECTED_SHA=2a4d444a`, v2 pid `2549985`, restarted list
oms + strategy (16:52:43 ET), evidence **9/9**. ⛔ The pins move on every restart — if LC1's flag is set by an OMS restart before the
open, `codex-2` re-pins. From 07:00 ET `claude-1` runs the **poller + 15-min full read** (not a `tail -F` stream), reports the first
Webull false-flip exit within minutes, and checks `INC1_STATUS.txt` for `delivered_now` on any page — a written row is not a page.
