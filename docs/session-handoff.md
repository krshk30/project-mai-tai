# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.
> Previous day notes (10-02 → 10-09) are in git history of this file (main 7a845576 and earlier).

## PRODUCTION NOW — as of Fri 2026-10-16 23:4x ET (claude-1 close-out read of the box)

| Item | State now | Evidence (as-of) |
|---|---|---|
| Box checkout | `9df73a18` (branch `install-9df-20261016`): Group D (D1, D2 resting/ATR-BUY, D3, D4, D5) + MOMGUARDEND1 next-batch + SYNC2X hygiene on main `7a845576` | `git rev-parse` 21:4x |
| Installed today | 16:01 efbfa397 (KILLSW1 core, COUNTRYROUTE-ATR, WEEKMAINT1 units) · 18:47 db73a581 (WBATTACH1 + O-1015-14 + ORBROUTE1) · 19:28 DB `jit=off` (SYNC2X) · 20:00 a33a30c2 (MOMCANCEL1) · 21:06 MOMGUARDEND1 guard-only · **23:3x Group D + guard ExecCondition + SYNC2X hygiene (9df73a18)**: oms/v2/reconciler restarted; guard 4 artifacts + unit ExecCondition + manifest + daemon-reload; native off-hours start → `exec-condition` skip, inactive, no OnFailure, no paper | install records in claude-1 memory `project_mai_tai_live_rule_cards.md` |
| Services | oms (since 19:56), schwab-1m-v2 / orb-schwab / control (18:47), strategy (10-13 20:29), market-data (10-14 12:31), reconciler (10-13 19:47), market-capture (10-03). Momentum guard + paper **inactive until Mon 03:40 ET timer** (by design, session closed) | `systemctl` 21:4x |
| Switches (OMS `/proc`) | `BOT_CONTROL_ENABLED=true`, `STRATEGY_SCHWAB_1M_V2_COUNTRY_ROUTE_ENABLED=true`, `ORB_COUNTRY_ROUTING_ENABLED=true`, `ORB_WEBULL_ACCOUNT_NAME=live:orb`, `MOMLIVE_SEC_APPROVED_HEAD=a33a30c2…` | `/proc/<pid>/environ` 21:4x |
| Database | alembic `20261015_bot_control`; **`ALTER DATABASE project_mai_tai SET jit = off`** (required: the ORB reconcile query cost ~1.08M → JIT 744 ms per sync pass; rollback `ALTER DATABASE … RESET jit`) | 19:28 |
| Positions / orders | Flat both brokers; 0 working orders; 0 open managed rows; 0 nonzero VP | gates 21:4x |
| Loop numbers | OMS broker-sync pass p50 ≈ 0.45–0.57 s (baseline 0.525 s) after jit=off; reconciler cycle excess p50 65 ms / p90 91 ms (baseline before Group D) | 19:3x / 20:46 |
| Timers next | Sat 01:00 ET momlive-sec (SEC weekly) · Sat 05:00 prune-ticks · Sun prune-capture/retention · **Mon 03:40 ET momentum guard** · Mon 06:20 preopen | `systemctl list-timers` 21:4x |

## TO BE EXERCISED (installed, waiting for the first real event — read these first on Monday)

| Fix | First live event | What proves it |
|---|---|---|
| MOMCANCEL1 (O-1016-10) | next Schwab momentum buy resting > 2 s | `cancel_wire_sent_coid` set within ~2 s; no `cancel_entry_broker_identity_UNKNOWN`; `[MOMCANCEL1]` CRITICAL only if Schwab ACK id unknown |
| MOMGUARDEND1 (O-1016-12) | Mon 03:40 ET start; Mon 18:10 ET close | 03:40: guard + paper active, `live_handoff=True`; 18:10: STOPPING + exit 0, no watchdog core, unit inactive (not failed) |
| WBATTACH1 + O-1015-14 | first Webull bare fill / first managed software sell | attach within first try; prepared sell row durable before wire |
| ORBROUTE1 (ORB country routing ON) | first ORB entry 09:2x | US name → Schwab, non-US/UNKNOWN → Webull; ORB protection reconcile quiet |
| KILLSW1 core + COUNTRYROUTE-ATR | first ATR entry Monday | route decision logged; no stale-row refusals |
| SYNC2X (jit=off) | every sync pass | p50 ≈ baseline; other services pick up jit=off on next restart |

## INSTALLED LATE 10-16 (23:3x) — Group D, verify Monday

| Item | What changed | Monday proof |
|---|---|---|
| D1 | position/avg-price alerts: operator's manual shares → INFO (positive VP per strategy required); reconciler cycle now logs `duration_ms` | no CRITICAL for manual shares; reconciler duration ≈ 40–60 ms |
| D2 (resting + fresh ATR resting BUY) | no false "stuck accepted/pending" alerts when a live working proof exists (one off-loop coalesced write per sync pass, ≤ 64 orders) | **compare stuck-alert counts vs last week** — stricter proof raised alerts on historical replays (92→119 orders) because history lacks live receipts; if noisier live, tune next week |
| D3 | prior-session confirmation entries retired only when proven closed + flat (off-loop) | no 07:01 ERROR for previous-session fills; held positions keep their confirmation exit |
| D4 | momentum transient identity mismatch → INFO once; `cancel_entry_broker_identity_UNKNOWN` stays ERROR | fewer ERROR lines on momentum exits |
| D5 | legacy cancelled-partial child: durable fence + ONE incident (no exit-path change) | no duplicate incidents |
| Guard next-batch | `ExecCondition` closed-session skip; failure unit exit 0 when only the page fails | Mon 03:40 start runs (condition passes in session); 18:10 clean close |
| SYNC2X hygiene | ORB reconcile query rewritten (plan cost 1.08M → 6.4k) + guard + logical-account log line; WEEKMAINT reports `jit` setting | sync p50 ≈ 0.53 s |

Deferred to next week (board): **D2-momentum** (stripped — 52 failing controls), **O-1016-14 D2-ATR protective pairs** (no persisted complete-child evidence), O-1016-11 D5 recovery read.
Rejected tonight (52249c24, do not resurrect): Schwab flip exit via strict close receipts, Webull un-fence into release, FOR UPDATE on the OMS loop, per-order snapshot rewrite in the sync pass, D3 dropping held entries, new D1 CRITICAL.

## OPEN BOARD (not tonight) — owner / next step

| ID | Item | Next |
|---|---|---|
| O-1016-9 | MOMEXEC1 study: momentum execution price/speed/misses (13 signals/8 fills so far; misses, not slippage, carry the cost) | collect 100+ live trades, then conclude; broker switch (Lightspeed/DAS) PARKED |
| O-1015-10 | MOMCUTOFF1 (live momentum entries after 10:00) | watch & analyse until next week (operator) |
| O-1016-5 / O-1016-7 | XLAB late fill (root cause = MOMCANCEL1, installed) · FUFU 300 s live vs 600 s paper time exit | watch / board |
| O-1016-11 | D5 closed-episode recovery read | PARKED next week (class count from codex) |
| O-1016-8 | WB/ORB native-guard paths hold a DB session across broker awaits | next week |
| O-1016-6 | SEC → WEEK automatic chain (receipt ownership) | Sat supervised manual start |
| O-1016-1 | WEEKMAINT1 weekly maintenance | **Sat 10-17 supervised test with the operator** (fill `/etc/project-mai-tai/weekmaint.json` first; dry-run then real) |
| GC follow-up | gen2 GC pauses in pytest heap (90–130 ms) on new rate tests; prod impact unmeasured | lane L assessment card (gc.freeze after boot?) |
| O-1015-9 | VALIDATE-GREEN (CI test debt) | board |

## RULES ADDED TODAY (operator 10-16) — apply without asking

- **Bug fixes need no approval:** found bug → build, review, full test, VPS speed test, install after close, switch ON; tell him after. Ask him only when a fix would break trading with no fix available, or for a new rule.
- **Validation scan job** (every 15 min, both tools): its purpose is to catch every trading issue before he does — cross-broker price gaps, unwatched holdings, refused sells, plus (new today) **momentum price + speed gap per buy, momentum exit gap vs target/stop, momentum buy vs tape (+5/+30/+60 s), ATR exit price gap vs bracket/meant price**. Tools: `~/.claude/tools/mai-tai-fullscan-watch/run.sh` and `~/.claude/tools/mai-tai-fullscan/run.sh` (+ `extra.sql`, `exgap.py`, `mombuy.py`).
- **One speed test per bundle**, on the final composed head (VPS interleaved base vs candidate; GC-attributed or base-equal stalls are not regressions).
- **Every install reads loop numbers within 30 min** (sync-pass buckets) — SYNC2X was caught this way 30 min after the 18:47 install.
- **A large "alert fix" diff gets a hunk-to-item map before tests**; anything touching exits, cancels or sizing is out of scope unless named.
- **Broker:** stay with Schwab/Webull; professional broker parked as an option; keep measuring.

## MONDAY 10-19 FIRST READS (claude-1, unprompted)

1. 03:40 guard/paper start (MOMGUARDEND1) · 04:00 roll · loop numbers (sync p50, reconciler).
2. 07:0x scanner + watchlist adds + squeeze alerts (rule 9b) after the weekend maintenance restarts.
3. First Schwab momentum buy → MOMCANCEL1; first ORB entry → country route; first Webull bare fill → attach.
4. 15-min scans with the new price/speed gap sections; 18:10 guard close.
