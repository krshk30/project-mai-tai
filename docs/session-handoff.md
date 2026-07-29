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

## 🟢 FLEET — what is live right now

**As of 2026-07-28 EOD.** HEAD `b9fd715`. Fleet **FLAT** (0 shares held, 0 non-terminal open
intents), all services `active`, NRestarts=0, 0 errors.

| service | PID |
|---|---|
| `project-mai-tai-oms` | 1725295 |
| `project-mai-tai-schwab-1m-v2` | 1736517 |
| `project-mai-tai-strategy` | 1733721 |

**Live flags (v2 + OMS):**

| flag | value |
|---|---|
| `..._ATR_FLIP_VOL_FLOOR` | **10000** |
| `..._CW_V2_RESTING_ENTRY_ENABLED` | true |
| `..._CW_V2_EH_RESTING_ENTRY_ENABLED` / `OMS_V2_EH_ENTRY_ENABLED` | true / true |
| `..._CW_V2_RECLAIM_ENABLED` / `..._CW_V2_RECLAIM_GAP_BARS` | true / 1 |
| `..._DUAL_BROKER_FANOUT_ENABLED` / `..._WEBULL_FANOUT_QUANTITY` | true / 1 |
| `OMS_NATIVE_OCO_EXIT_POLL_ENABLED` | **true** (enabled 07-28) |
| `OMS_RECORD_NATIVE_OCO_EXIT_FILLS_ENABLED` | true |
| `WEBULL_BRACKET_REALIGN_ON_FILL_ENABLED` | **false** (broken at the broker) |
| `ORB_ENABLED` / `ORB_QUANTITY` | true / 10 |

**Entry bound:** one resting + one reclaim per ATR segment (`max_entries_per_flip=2`).
**There is NO cooldown** — removed 07-28 (#590); the per-segment cap is the bound.
⛔ **Do not re-add it**: it would contradict the 1-bar reclaim gap.

⛔ **Three settings whose CODE DEFAULT disagrees with PRODUCTION** — vol floor (was 5000 vs live
10000, now aligned), reclaim gap (0 vs live 1), entry cap (flag-derived).
**Check the ENV before quoting any default as the live value.**

---

## 👀 WATCH NEXT SESSION

1. **Fewer entries are expected.** The liquidity floor now gates all three live entry paths
   (reactive / resting / fan-out) — before 07-28 it guarded only replaced code. Intended, but it is
   a real behaviour change at the open.
2. **First job: re-run the backtest-vs-live comparison on a STABLE-CODE day.** 07-28 had six
   deploys mid-session, so it cannot judge parity. Config parity itself is FIXED and verified
   (89/90, #592); what is unproven is whether the engine reproduces a live day end-to-end — only
   STKH matched so far (+1.90% both). Scripts on the box: `_parity_diff.py`, `_live_today.py`,
   `_cnet_probe.py`, `_density.py`.
3. ⛔ **When comparing, respect three structural limits** — the replay takes **ONE round trip per
   symbol-day** (so "1 replay vs 6 live" is expected; compare only the FIRST live trade) · quote
   density ~1/4s vs a continuous live feed · sparse-bar symbols are uncomparable (CNET: 71 bars).
4. **A hand-cancel is only half a stop.** Cancelling at the broker does NOT stop the Webull fan-out
   leg (software price-cross detector). Hand-cancel **AND** set `global_manual_stop_symbols`.

---

## 🔴 OPEN THREADS — one line each (full detail: [`handoff-open-items.md`](handoff-open-items.md))

⚠️ That file was moved **verbatim**; nothing in it was judged closed. **Prune it with the operator.**

- **429 can still lose an exit fill** past the ~45s retry bound (#585) — deliberate: an open managed
  row blocks fan-out re-entry, so protection outranks bookkeeping.
- **07-27 exit history is permanently short** — backfill recovered **4 of 8**; the rest are
  unrecoverable (one-exit-per-entry, fixed forward in #585).
- **36% of in-window BUY flips never armed 07-28** (8 of 22). ⛔ NOT the cooldown — watchlist
  ABSENCE. ⚠️ Number NOT trustworthy yet: its windows come from the table #582 fixes forward-only.
  **Re-run on a clean day.**
- **Spurious `position qty N -> 0` transitions** still release the reclaim claim with no real exit;
  logged as `SPURIOUS-no-shares-ever-held`. Measure before changing.
- **P2 measurements not run:** entry-quality (re-sourced onto DB reject reasons), gap-through caps.
  ⛔ **Reclaim-trigger is NOT measurable the obvious way** — `entry_price` and `cw_flip_level` are
  written from the SAME variable on the resting path, so "premium over flip level" is 0.00% by
  construction. Use the FILL price vs `cw_segment_high`.
- **Architectural (07-17):** the injected-settings seam is a convention, not an invariant — any
  default-flip PR must run the FULL `tests/unit` suite. See the detail file.

---

## 📜 HISTORY

- **What happened, day by day:** [`handoff-log.md`](handoff-log.md) (append-only)
- **Deep history:** the archive index below

## 📚 ARCHIVE INDEX (deep history — open only to dig)

| file | covers |
|---|---|
| [`handoff-archive/2026-07.md`](handoff-archive/2026-07.md) | **07-16..07-25** — OCO build + STEP-1 live, the wrong-bars root cause, resting flip-entry, Webull mirror, EH trading, dual-broker fan-out build |
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | go-live + morning verdict + #326 + restart recovery, OMS exits, ATR qualifier, age-gate, 04:00 race, tick-capture |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine groundwork, regression battles (56 entries) |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | earliest — token-SPOF saga, early v2 scaffolding, streamer fixes (3 entries) |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2-isolated bot's own deep design/status history |
| `session-handoff-global.md` | frozen pre-split monolith (backup; to be retired) |

---

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

[[project-mai-tai-context]] · [[project-mai-tai-0400-watchlist-staleness-race]] ·
[[project-mai-tai-v2-real-account-routing-risk]] · [[project-mai-tai-v2-entry-warmup-gate]] ·
[[project-mai-tai-v2-no-exits]] · [[project-mai-tai-v2-entry-criteria]] ·
[[project-mai-tai-schwab-bar-build-core]] · [[feedback-session-doc-and-memory-discipline]]

