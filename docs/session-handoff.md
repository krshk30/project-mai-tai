# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-09-16 20:20 ET.** Batch `2026-09-16-blind-window-proven-stale-flip-cap-mirror-lag-deployed`.
Integrator for this rotation; `codex-2` executed every deploy and reviews this PR. The author never reviews.

> ⛔⭐⭐⭐ **RECLAIM1 IS LIVE.** `MAI_TAI_STRATEGY_SCHWAB_1M_V2_FLIP_OWNED_FIRST_ENTRY_ENABLED=true` read from the running v2
> (pid `2269797`) by the after-restart evidence at 19:47 ET. One trade per ATR segment, first resting entry only, no reclaim.
> **A hard-stop exit does NOT release the segment's entry slot — operator RULING 09-16: that is a guardrail and stays.**

---

# ✅ PRODUCTION — main and box IN SYNC at `07cba271` (everything merged today is DEPLOYED or deliberately DORMANT)

| | |
|---|---|
| box (deployed) | **`07cba271e35be7a2467937931d6960b706eb6b74`** — read FROM THE BOX 2026-09-16 20:05 ET, branch `main`, clean |
| GitHub main | **`07cba271`** — identical at the time of writing. This handoff PR is docs-only: no sync, no restart |
| gate pin | `/home/trader/preopen.sh`: `EXPECTED_DATE=2026-09-17` · `EXPECTED_SHA=07cba271…` · `EXPECTED_PID=2269797` · `EXPECTED_START='Wed 2026-09-16 23:43:43 UTC'` (read from the box 20:05 ET) |
| open PRs | **none** — this handoff PR excepted |
| exposure | after-restart evidence 19:47 ET: **open managed rows 0 · nonzero account-position rows 0 · both live accounts flat before and after** |
| merges 09-16 (**7**) | **#988** handoff 09-15 · **#989** paper ORB stale dashboard · **#990** Momentum paper pre-registration · **#991** Momentum 30/60 paper bots · **#992** Webull mirror wire lag + crossed-stop shape guard · **#993** SHORT-segment seed cap (FTFT) · **#994** Massive ATR seed — **DORMANT, flag false** |
| migration | `alembic_version = 20260916_0021` (Momentum paper tables), read 20:15 ET. The 19:47 ET restart evidence was taken BEFORE the activation and shows `20260910_0020`; that is expected ordering, not a discrepancy |

## Restarts — all 09-16, all `NRestarts=0`, 0 tracebacks after start (verified on the box 20:05 ET)

| service | pid | started (UTC) | why |
|---|---|---|---|
| **orb** (paper) | **2051823** | 10:20:40 | #989 — 06:20 ET, before the open, `codex-2` on operator GO |
| **schwab-1m-v2** | **2269797** | 23:43:43 | #993 live; #994 code present, `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_MASSIVE_SEED_ENABLED` read **false** from `/proc/2269797/environ` |
| **oms** | **2270050** | 23:44:12 | #992 |
| **momentum-paper** (new unit) | **2273356** | 23:54:55 | #991 activation — `project-mai-tai-momentum-paper.service`; old `polygon_30s` runtime unit removed (0 units match) |
| **control** | **2273848** | 23:55:17 | #991 card activation |
| **strategy** | **2274173** | 23:55:38 | #991 activation. ⛔ codex's deploy summary said strategy was untouched — it was untouched by the #992/#993 window and restarted by the later Momentum activation |
| reconciler 1626620 · market-data 2202865 · market-capture 2202817 | — | unchanged (09-14 / 08-30) | — |

## After-restart evidence (`v2_restart_evidence.py`, run by `codex-2` 19:47 ET, re-read by `claude-1`)

**8/9 PASS.** The one FAIL is **Bar continuity: gaps spanning restart 1/115** — NAMI had no print in the 19:44 ET minute and Schwab
REST also has no candle for it, so no bar was fabricated; a genuine no-trade minute at the restart boundary, not a hole. Restarted
2/2 · not-restarted 7/7 (at 19:47; control/strategy moved later, above) · flat before/after · running-process flags 4/4 incl. the
seed flag false · REST warmup 8/8 · BOOT-HOLD released 19:43:55 ET `reconstructed_uncapped=0` · tracebacks 0.

## What is LIVE tonight and what to READ tomorrow (owner · first read)

| change | live? | first evidence to read | owner |
|---|---|---|---|
| **#993 SHORT-segment cap** — a symbol (re)joining the watchlist with a SHORT state rebuilt from a SELL older than its watch-start cannot rest until a SELL we watch | live (v2 23:43Z) | `[V2-CW-SEED-CAP] <sym> reconstructed SHORT segment capped …` on the first intraday add whose seed ends short; MEDS-shaped 04:00 names must NOT be capped | claude-1 |
| **#992 mirror lag + shape guard** — Schwab resting primary returns after acceptance, no inline reconcile; Webull mirror re-shaped at the wire (LIMIT within band if crossed; ASK_PAST_BAND / NO_FRESH_QUOTE abandon) | live (OMS 23:44Z) | `[OMS-FANOUT-MIRROR-LAG] … lag_ms=<1000` on every mirror; **zero `shape=abandoned_no_fresh_quote`**. UNEXERCISED at close: 0 mirrors since restart | claude-1 |
| **#991 Momentum 30 / Momentum 60 paper** — `$500` notional, 04:11–09:29:59 ET detection, +5% / −15% / 600 s | live (unit 23:54Z) | feed health and first paper events; first meaningful check **09:35 ET** | codex-2 |
| **#994 Massive ATR seed** | **DORMANT** — G5 FAILED | nothing; the installer refuses without a literal `**G5 verdict: PASS**` line | — |
| #989 paper ORB dashboard | live since 06:20 ET | 04:00 roll shows only current-session symbols | codex-2 |

## Rulings made today (operator)

1. **Hard-stop non-reset stays.** MEDS 13:57 ET BUY flip went unowned because the segment's first slot was consumed at 11:48:59 by
   the hard stop of the second false flip; a confirmation exit resets, a stop-out does not. Operator: "these are all working like
   guardrails." Cost accepted (+5% target missed).
2. **Fresh-flip rule confirmed in full**: after a confirm or re-confirm only a flip we watched may own an entry; a SELL one minute
   before the re-join is void. The 07-30 cap covered the LONG half; #993 adds the SHORT half.
3. **Massive seed: "flag on" ruling OVERTAKEN by the pre-registered G5** (parity 98.050% < 99.5% on 5 of 30 days; gained 65 flips
   sum −16.89%, median +1.03%). Merged dormant. Any enablement needs a new design + evidence decision.

## Open items — each with an OWNER and a NEXT ACTION (nothing boards without both)

| item | owner | next action |
|---|---|---|
| `[V2-RESTING-SLOT-CONSUMED]` prints ONCE per segment; MEDS 12:10–13:57 ET (107 min of suppressed rests) left no trace | claude-1 | intake spec: `[V2-CW-STATE-PROBE]` already carries `cw_resting_taken=` but prints only while ARMED; a short (unarmed) segment prints nothing per minute. Add a per-minute suppressed-rest line or extend the probe to short state; observability only, no rule change |
| NO_FRESH_QUOTE abandon on the Webull mirror (#992) | claude-1 | read the first 5 sessions' `[OMS-FANOUT-MIRROR-LAG]` lines with the denominator (mirrors placed); any `abandoned_no_fresh_quote` → revisit the 2000 ms fallback |
| G5 parity: five bad days (08-11 95.7%, 08-12 89.8%, 08-19 95.3%, 08-25 80.8%, 08-28 83.4%) — aggregation masked them | none (parked) | only if the seed is ever revisited: per-symbol breakdown of those days |
| ZTG 08:47 ET `ASK_PAST_BAND` (0.5% band) cost a +5% winner one bar later | operator | rule: keep or widen the band — not built |
| Blind window 07:00–07:08 / pre-07:00 bars | operator | the seed was the lever and failed its gate; PRE07 census stays as is |
| Webull `ORDER_RISK_RULE_PRICE_AGGRESSIVE` distance rule still not characterised (ZTG 09:35, MEDS 11:31: stops 10–26% above market) | codex-2 | census of accept/reject vs distance-from-ask, from #976 evidence fields |
| Post-close hand-cancel of ONE leg is not a stop (FTFT 11:01 ET: Schwab cancelled by hand, Webull mirror filled 17 min later) | operator | use the manual-stop lever for both legs; no code change requested |

## Pre-open 09-17 (`preopen.sh` pins already moved by `codex-2`)

Run the gate and the grades as usual. Expect: `EXPECTED_SHA=07cba271`, v2 pid `2269797`, BOOT1/SEED1 watches green, seed flag
false, `[V2-ATR-SEED-CENSUS]` **absent** (flag off — its absence is expected, not a miss). Any `[V2-CW-SEED-CAP] … SHORT` line
before 07:00 on a 04:00-watchlist name is a RED (the cap must only bite symbols whose SELL predates their watch-start).
