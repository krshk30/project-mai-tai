# Session Handoff — CURRENT STATE (read this first)

> ## ⛔ HOW TO MAINTAIN THIS FILE — two verbs, never merge them
> 1. **OVERWRITE this file.** It answers one question: *what is true RIGHT NOW.* If a line here is
>    no longer true, **delete or rewrite it** — never append.
> 2. **APPEND to [`handoff-log.md`](handoff-log.md).** That is where *what changed today* goes.
>
> **Target: ~150 lines.** To onboard an agent: *"Read `docs/session-handoff.md`."*

> **⛔⭐ OUTPUT CONSTRAINT (every study): report per-trade %, MEDIAN-FIRST, with a drop-one.
> NEVER a bare dollar total.**

---

## ⚡ FIRST SCREEN — act on this alone

**As of 2026-08-17 EOD.** Fleet active. **Deployed HEAD `634ff21`** (pulled + verified by CONTENT
after each of two deploys). Tree **0 local changes**. Account **FLAT on everything** — verified
against a 3s-fresh live sync, not a bare zero.
✅ **XPON is GONE.** Friday's `live:schwab_1m_v2` −1000 short (the operator's own) now reads
`0.00000000` on a live, actively-syncing table. Open item 16 still stands: the reconciler filters
`quantity > 0`, so **a short drift of OURS would still be undetectable.**

### Today's deploys — both clean, both UNEXERCISED
| PR | what | deploy | outage |
|---|---|---|---|
| **#706** | 3 noise guards on the Webull attach + `RoutingBrokerAdapter.fetch_quotes` | 11:00 ET | **3s** |
| **#707** | `[WEBULL-EXIT-PAIR-REFUSED]` diagnostics + retry horizon 4.0s → ~29s | 11:44 ET | **10s** |

`schwab-1m-v2` **deliberately NOT restarted** either time ⇒ no v2 bar hole; continuity check **0
gaps**; 0 Tracebacks, 0 ERROR/CRITICAL; all 5 heartbeats fresh.

## ⛔⛔ THE BIG CORRECTION — Friday's Tier A direction was aimed at the WRONG MECHANISM
Friday said: *re-price off a fresh quote, refuse-if-not-held, serialise.* I shipped two of those and
they are defensible — **but none of them is the cause**, and three hypotheses are now dead:

| hypothesis | verdict |
|---|---|
| stale entry price | **refuted for #689 bare-fill** (CGTL's levels were **244 ms** old). Survives for #692 reprotect only |
| CORE-session / prior-close reference | **dead** — AKAN's stop 7.74 was *below* its 08-13 close of 9.49, still refused |
| malformed payload | **dead** — Probe X: the production builder's own output previews **HTTP 200** |

⛔⭐⭐ **`[WEBULL-PROTECT-ATTACHED]` = 0 and `[WEBULL-EXIT-PAIR-PLACED]` = 0 across ALL SEVEN
retained `oms.log` files (08-11 → 08-17).** `place_order` has never once returned. It is not
"0-for-11", it is **0-for-EVER**. This path has never worked.

⛔⭐⭐ **`preview_order` DOES NOT VALIDATE POSITION BACKING** — it returned 200 for this exact
payload while the account was FLAT. So **Probe W4's 200 only proved the shape PARSES, never that it
PLACES.** Two comments still say "BROKER-PROVEN" (`webull.py::_is_exit_only_pair`,
`tests/unit/test_webull_attach_protection.py`) and both overstate the evidence.

⛔ **The reject CODES name the REQUIRED relation, not the violation**, so
`STOP_LOSS_PRICE_LT_MARKETPRICE` reads as its own opposite. The 200-char log cap hid the message
text (*"...should be lower than the cu"*) — that truncation is why the wrong story stood for a week.
**Fixed in #707 (cap now 1000).**

### What survives — PLAUSIBLE, NOT PROVEN
**Position backing at place time.** Attempts fired 0s/2s/4s against a settle lag measured at
**12.7s** (CGTL: FAILED logged **8s before** the position became visible); one episode logged
`NEVER VISIBLE after 300s`. #707 widens the horizon to ~29s to test it.
⛔ **Counter-evidence is real:** in **4 of 6** bare-fill episodes attempts 2–3 fired *after*
`SETTLE-LAG: VISIBLE` and were still refused. ⇒ **If refusals persist tomorrow, the settle window is
exonerated** and `[WEBULL-EXIT-PAIR-REFUSED]` is what tells us the real reason.

## 🕐 OPEN PRs
**NONE.** #706 + #707 merged and deployed today.

## 🔴 OPEN THREADS — tiered, detail in [`handoff-open-items.md`](handoff-open-items.md)

> ⛔ Tiers, not priorities within a tier. **Tier A is what can lose money today.**

### 🔴 TIER A — can cost money now
1. **⭐⭐ TOMORROW'S FIRST READ — did the attach EVER place?** *(item 15)* On the first Webull
   fan-out fill, read **`[WEBULL-EXIT-PAIR-REFUSED]`** (new): it carries the exact payload we sent
   AND the broker's full response. One episode should settle a cause that cost a week of inference.
   ⛔⛔ **A LOWER REFUSAL COUNT IS NOT A FIX.** The only PASS is `[WEBULL-PROTECT-ATTACHED]`, never
   once observed. Both PRs are **UNEXERCISED** — flat account since deploy.
2. **THE RETRY BOUND IS STILL UNREACHABLE** — `_v2_exit_close_failures` resets on every positively-
   HELD read, so a reject on a position we truly hold retries forever. Untouched by #706/#707.
3. **PRE-MARKET IS 0% BRACKETED** *(item 11)* — 14d: RTH 172/172 orb, 131/132 schwab; PRE 0/34,
   0/13. Both brokers limit-only in EH; the RTH-edge arm (#647 Gate 2, built, dark) is the answer.
4. **`virtual_positions` FALSELY READS ZERO ON A POSITION WE HOLD** *(item 12)* — `[VIRTUAL-CLEAR]`
   fires ~0.7s after a fill, inside the settle window, never restores. ✅ reconciler half FIXED
   (#704) but **UNEXERCISED**; ⛔ root cause + 4 consumers untouched.

### 🟡 TIER B — wrong numbers / wrong reasons, no immediate loss
5. **⛔ NEW 08-17 — `entry_fix_watch` recorded 2 COMPOSITION BREACHES on 08-14 that were never
   reported.** `STATUS.txt` (last run Fri 16:10 ET): `[BREACH] ONFO resting=2` and
   `[BREACH] VWAV resting=2` against the #644 cap of ≤1 resting. Real money, on the board nowhere
   else. Also 9 armed crosses produced no entry (median max-high vs flip **+4.11%**) and 2 buy fills
   matched no armed cross. ⛔ **Silence from this watch is NOT green — read the file.**
6. **THE RECONCILER CANNOT SEE A SHORT AT ALL** *(item 16)* — `quantity > 0` filter.
7. **`broker_order_events` CONFLATES OUR ABORTS WITH BROKER REFUSALS** — needs a `source` column.
8. **THE WEBULL LEG BUYS UNDER A LOOSER CAP** *(item 13)* — deferred 08-14 by the operator.
9. **THE FLOAT CEILING SILENTLY DROPS LARGE-FLOAT MOVERS** *(item 14)* — reject reason is
   `logger.debug` only. Threshold = selection = **DISCUSS FIRST**; the INFO-logging half is cheap.
10. **ENTRY LEVELS QUANTISED BY ~70 bps** — the 0.5% band collapses to ZERO under ~$2.
11. **THE ORPHAN WATCH READS SCHWAB ONLY** — it can never clear a Webull question.

### 📋 TIER C — known, parked, not decaying
12. **CHURN IS THE BIGGEST NUMBER** — median 5 entries/symbol-day, max 16. Selection is
    **DISCUSS BEFORE BUILDING.**
13. The 16:00 bracket death · P0 boot-hold freshness gate · Redis evicts the heartbeat stream
    *(item 9)* · per-lot attribution gap — unchanged.
14. **⚠ Flaky test** `test_scanner_cycle_history_retention_and_dedup` — cross-file ordering.
15. **📄 DOC DEFECT:** `handoff-log.md`'s **08-14 entry sits at the BOTTOM (~line 1744)**, not the
    top, against that file's own rule. Left alone (append-only) but nobody will find it there.

## 🔔 ALERTING
`orphan_order_cron.sh` (Schwab-only) · `bar_gap_watch_cron.sh` · `reconcile_alert_cron.sh` ·
`entry_fix_watch/watch_cron.sh` ⛔ **silence is NOT green — read `STATUS.txt`** (it is holding 2
unreported breaches right now, see B5) · OMS liveness · pre-open readiness · token expiry · OCO
capture.
⛔ All ROOT crontab, ET-guarded **inside** the script. ⛔ A hand-chmod on the box BLOCKS EVERY
DEPLOY; the exec bit is committed (#693) — do NOT `git checkout` it away.

## 🔑 SCHWAB TOKEN
**Read from the store 2026-08-17 06:27 ET:** `refresh_token_expires_at = 2026-08-19T09:21:35Z`
⇒ **Wed 2026-08-19 05:21 ET.** Access token refreshing normally on its ~30-min cadence.
⚠️ 05:21 is **before** Wednesday's 07:00 EH open — **re-auth Tue evening or Wed after the close.**
⛔ **Never quote this date from memory; read `refresh_token_expires_at`.**

## 🧠 MEMORY POINTERS (auto-load each session)
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_restart_bar_gap_checklist]] **(READ BEFORE ANY RESTART)** ·
[[project_mai_tai_reprotect_chain_uncovered_window]] **(REWRITTEN 08-17)** ·
[[project_mai_tai_probe_w_webull_stoplimit_master]] **(its "broker-proven" claim is a PREVIEW)** ·
[[project_mai_tai_exit_reservation_conflict]] · [[feedback_a_wrong_reason_is_worse_than_a_missing_one]] ·
[[feedback_fixture_must_match_production_config]] · [[feedback_unexercised_is_not_a_result]] ·
[[feedback_report_times_in_et]] · [[feedback_mutate_the_code_pin_the_threshold]]
