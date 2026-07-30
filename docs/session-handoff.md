# Session Handoff — CURRENT STATE (read this first)

> ## ⛔ HOW TO MAINTAIN THIS FILE — two verbs, never merge them
> 1. **OVERWRITE this file.** It answers one question: *what is true RIGHT NOW.* If a line here is
>    no longer true, **delete or rewrite it** — never append.
> 2. **APPEND to [`handoff-log.md`](handoff-log.md).** That is where *what changed today* goes.
>
> ⭐ **Why it is split (2026-07-29).** These two jobs used to share one file. Appending is easy and
> got done every day; overwriting requires noticing that something written two weeks ago is no
> longer true, and nothing forced it. Result: the narrative was always current while LIVE OPS STATE
> sat **twelve days stale**, and the file's own "keep under 400 lines" rule was structurally
> impossible. Keep them apart and staleness needs someone to actively write something false.
>
> **Target: ~150 lines.** If it grows, detail belongs in the log or a linked doc, not here.
> To onboard an agent: *"Read `docs/session-handoff.md`."* — this file alone should be enough to
> know what is live, what is open, and what to watch.

> **⛔⭐ OUTPUT CONSTRAINT (every study, no exceptions): report per-trade %, MEDIAN-FIRST, with a
> drop-one. NEVER a bare dollar total.** One VEEE at $25-29 notional outweighs sixteen $1-7 names
> and flips conclusions (violated twice 07-15 -> two wrong answers). This is the single line that
> saves the most rework. [[project_mai_tai_percentages_not_dollars]]

---

## ⛔⭐ FIRST THING AT THE OPEN — one decision, 30 seconds

**The live volume floor is `10000`. The operator's last word on it was "keep 5K, my mistake about
10K".** These disagree and the live value is the more restrictive one, so the bot is currently taking
**fewer** entries than intended.

I did **not** change it unattended: lowering a live gate makes the bot trade *more*, which is a risk
increase, and nobody was in the loop. `#588` aligned the CODE DEFAULT *up* to production's 10000 —
which was accurate about production but moved away from the instruction.

**If 5K is still what you want:** set `MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_FLIP_VOL_FLOOR=5000` in
`/etc/project-mai-tai/project-mai-tai.env`, `systemctl restart project-mai-tai-schwab-1m-v2`, and the
code default needs a one-line PR to match. **If 10K is fine, say so and this line gets deleted.**

---

## 🟢 FLEET — what is live right now

**As of 2026-07-29 EOD.** HEAD `b951b4e`. All services `active`, **NRestarts=0** (a deliberately
restart-free session, so the day's numbers are usable). **Both brokers FLAT of anything ours.**

| service | PID | note |
|---|---|---|
| `project-mai-tai-oms` | 1890366 | restarted for #608 mid-session |
| `project-mai-tai-schwab-1m-v2` | 1736517 | |
| `project-mai-tai-strategy` | 1733721 | |
| `project-mai-tai-orb` | — | **inactive + disabled** (see below) |

⛔ **Schwab holds CYN 5000 sh @ $2.5057 (~$5,350) overnight — that is the OPERATOR'S MANUAL TRADE,
not ours.** Verified: we have **zero** CYN orders, fills, intents, or bars. Leave it alone. It is
listed here so nobody "reconciles" it. Only Mai Tai's *own* positions are ours to manage.

**Live flags (v2 + OMS):**

| flag | value |
|---|---|
| `..._ATR_FLIP_VOL_FLOOR` | **10000** — ⛔ see the decision at the top of this file |
| `..._CW_V2_RESTING_ENTRY_ENABLED` | true |
| `..._CW_V2_EH_RESTING_ENTRY_ENABLED` / `OMS_V2_EH_ENTRY_ENABLED` | true / true |
| `..._CW_V2_RECLAIM_ENABLED` / `..._CW_V2_RECLAIM_GAP_BARS` | true / 1 |
| `..._DUAL_BROKER_FANOUT_ENABLED` / `..._WEBULL_FANOUT_QUANTITY` | true / 1 |
| `OMS_NATIVE_OCO_EXIT_POLL_ENABLED` / `OMS_RECORD_NATIVE_OCO_EXIT_FILLS_ENABLED` | true / true |
| `WEBULL_BRACKET_REALIGN_ON_FILL_ENABLED` | false (broken at the broker) |
| `ORB_ENABLED` / `ORB_QUANTITY` | true / 10 — ⛔ **the BOT is decommissioned; the flag stays true on purpose** |

⛔ **ORB THE BOT IS OFF.** Real money is now **Schwab v2 only** (+ its Webull fan-out leg). Fills on
`live:orb` are **FAN-OUT legs, not ORB trades**. ⛔ **Do NOT set `MAI_TAI_ORB_ENABLED=false`** —
`runtime_registry:119` gates the ORB registration, which seeds the `live:orb` BROKER ACCOUNT that
`webull.py:87` builds its adapter map from. Setting it false breaks the live fan-out.
[[project_mai_tai_orb_decommissioned_but_flag_stays_true]]

**Entry bound:** one resting + one reclaim per ATR segment (`max_entries_per_flip=2`).
**There is NO cooldown** — removed 07-28 (#590); the per-segment cap is the bound.
⛔ Do not re-add it: it would contradict the 1-bar reclaim gap.

⛔ **Check the ENV before quoting any code default as the live value.** Reclaim gap is 0 in code vs 1
live; the vol floor disagreement above is the same class of drift. Tool: `ops/health/env_default_drift.py`.

---

## 🔬 THE ALL-DAY TRADE RECORDER — running from tomorrow's open

**Live now.** Cron `*/5 * * * *`, guards its own 07:00–20:30 ET weekday window (⛔ `CRON_TZ` is
ignored on this box, and a UTC crontab range *cannot* express an ET window — it silently loses
20:00–20:30 and all of Friday's post-19:00 tail).

| path | verb | holds |
|---|---|---|
| `/home/trader/trade_records/<day>.jsonl` | **append-only** | one closed round trip per line |
| `/home/trader/trade_records/<day>.unpaired.jsonl` | **overwritten** | entries with no exit fill *yet* |
| `/home/trader/trade_records/cron.log` | rotating | proof of life |

Per trade it records both brokers' order ids, intended vs actual fill + `slippage_pct`, path /
`cw_entry_n`, `held_secs`, `mfe_pct`/`mae_pct`, and the what-ifs: **floor+2% with 2/3/5% trails**, the
**tiered stop (<$3:−5 / ≥$3:−3)**, and whether a **3-min time stop** would fire.

⭐ **Why it exists:** reconstructing 07-29 from the DB after the fact gave **three answers, two
wrong** — FIFO pairing invented a −8.40% AMIX trade that never existed, and coid pairing exposed 5
exits dated *before* their own entry. **Attribution must be captured, not inferred.**

⛔ **Read `intrabar_ambiguous` before trusting a what-if.** When a stop and target share one 1-minute
bar, bars cannot order them. ⛔ And an unpaired entry is **not** a naked position — ask the broker.

---

## 👀 WATCH NEXT SESSION

1. **Let the recorder run a full clean day first.** 07-29's what-ifs are directional only:
   **7 of 23 round trips closed inside one minute**, so no completed bar exists and the bar path
   cannot speak for 30% of the day. Decide the exit rule on **3–5 clean days**, not on one.
2. **Fewer entries are expected** — the liquidity floor now gates all three live entry paths
   (reactive / resting / fan-out); before 07-28 it guarded only replaced code.
3. **`trade-coach` CPU is inherent, not drift.** The restart moved it 43%→47%. OMS heartbeat
   starvation (the 09:00/09:09 pages) **will recur**. Folded into open item 2.
4. **A hand-cancel is only half a stop.** Cancelling at the broker does NOT stop the Webull fan-out
   leg (a software price-cross detector). Hand-cancel **AND** set `global_manual_stop_symbols`.
5. **`cw_arm_bar_ts` is `0` on 6 of 23 records** — the `rth_resting` path does not stamp it, so
   segment identity is unavailable for those. Cosmetic today; it breaks first-vs-reclaim splits.

---

## 🔴 OPEN THREADS — 3 (detail: [`handoff-open-items.md`](handoff-open-items.md))

✅ Driven from **66 → 3** with the operator on 2026-07-29; everything closed moved verbatim to
[`handoff-log.md`](handoff-log.md) with its reason. The 07-29 after-close batch closed **all 5**.

1. **Re-run backtest-vs-live on a STABLE-CODE day.** Config parity FIXED + verified (89/90, #592);
   the engine reproducing a live day end-to-end is still unproven (only STKH matched, +1.90% both).
   07-28 had six mid-session deploys and cannot judge parity. ⭐ **The recorder now gives the
   clean live side of this comparison for free.**
2. **polygon / strategy-engine freeze** — 60-80s at open/close, ~72% CPU in the JSON snapshot encode;
   `#366` throttle deployed and INSUFFICIENT. Now also: `trade-coach` 47%, `control` 33.7%/1.6 GB.
3. **VPS retention prune** — remaining: `reconciliation_findings` 1142 MB, `dashboard_snapshots`
   1017 MB. ⛔ **NEVER prune the `schwab_1m_v2` rows of `strategy_bar_history`** — that is the
   backtest bar source *and* the recorder's bar path. (Dead strategy_codes already pruned:
   1,091,270 rows, 1962→815 MB, backtest re-verified byte-identical.)

⛔ **KEEP THIS AT ~3.** When something closes, MOVE it to the log. A study nobody will run, a rule
that is never "done", and a dormant feature's item are NOT open work — that is how this reached 66.

---

## 📜 HISTORY

- **What happened, day by day:** [`handoff-log.md`](handoff-log.md) (append-only)

| archive | covers |
|---|---|
| [`handoff-archive/2026-07.md`](handoff-archive/2026-07.md) | **07-16..07-25** — OCO build + STEP-1 live, the wrong-bars root cause, resting flip-entry, Webull mirror, EH trading, dual-broker fan-out |
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | go-live + morning verdict + #326 + restart recovery, OMS exits, ATR qualifier, age-gate, 04:00 race, tick-capture |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine groundwork, regressions |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | earliest — token-SPOF saga, early v2 scaffolding, streamer fixes |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2-isolated bot's own deep design/status history |

## 🔗 KEY REFERENCE DOCS (design-first / canonical)

- **Entry rules:** [`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md) ·
  [`schwab-1m-v2-atr-flip-entry-design.md`](schwab-1m-v2-atr-flip-entry-design.md) ·
  [`schwab-1m-entry-gates-extracted.md`](schwab-1m-entry-gates-extracted.md)
- **ATR qualifier + warmup:** [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md) ·
  [`v2-atr-early-warmup-fix-design.md`](v2-atr-early-warmup-fix-design.md) ·
  [`v2-warmup-db-seed-fix-design.md`](v2-warmup-db-seed-fix-design.md)
- **Go-live / race fix:** [`v2-paper-to-live-credential-transition-scoping.md`](v2-paper-to-live-credential-transition-scoping.md) ·
  [`v2-0400-watchlist-race-fix-design.md`](v2-0400-watchlist-race-fix-design.md)
- **Exits / ticks / pricing:** [`v2-tick-capture-design.md`](v2-tick-capture-design.md) ·
  [`v2-reference-price-fix-design.md`](v2-reference-price-fix-design.md)
- **Resilience / ops:** [`schwab-1m-v2-loop-resilience-design.md`](schwab-1m-v2-loop-resilience-design.md) ·
  [`vps-deployment.md`](vps-deployment.md)

## 🧠 MEMORY POINTERS (auto-load each session; listed for cross-reference)

[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project-mai-tai-v2-real-account-routing-risk]] · [[project-mai-tai-v2-entry-criteria]] ·
[[project-mai-tai-oms-scoping-invariant]] · [[project-mai-tai-oco-exit-fill-blackout]] ·
[[project-mai-tai-schwab-bar-build-core]] · [[feedback-session-doc-and-memory-discipline]]
