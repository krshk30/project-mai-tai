# ORB Opening-Range Breakout (P6 "OPEN") — research COMPLETE, settled config

> **Status: RESEARCH COMPLETE → deployment-readiness scoping.** The entry and exit are settled below.
> This is a **leading candidate for live, NOT a proven verdict** — it still needs intrabar execution and
> forward validation before any go-live (see §8). All work read-only / bar-close backtest on historical
> 1-min data. Companion: `orb-opening-path-spec.md`. Engine: [`scripts/orb_exit_backtest.py`](../scripts/orb_exit_backtest.py).

## ✅ SETTLED CONFIG
- **ENTRY = PRIOR:** 5-min opening range from **09:30** · enter on first bar that **closes > OR_high** ·
  **vol ≥ 1.5× OR_avg** · **close > VWAP** · **close > EMA9** · **skip if OR_width% > 12%** (chop) ·
  **cutoff 10:30** · **one trade per symbol** per session.
- **EXIT = TRAIL-8%:** stop starts 8% below entry, **ratchets up 8% below the high-water-mark, never down.**

## 1. The proposal & the data fix
ORB on the qualified small-cap scanner names, 09:25–10:30 ET. **Decisive data finding:** the bot's stored
`schwab_1m_v2` bars are **watchlist-gated** — winners are promoted *after* their breakout (CRVO first
stored bar 09:35, ATPC 09:51), so their 09:30 opening range **does not exist** in stored bars, biasing any
stored-bar backtest against the winners. **All backtests source 1-min bars from Schwab REST pricehistory**
(`needExtendedHoursData=true`), validated exactly against the operator's TradingView/Pine reference (CRVO
OR_low 4.610, breakout 5.330). **Live prerequisite:** the scanner must surface candidates **before 09:30**
or the bot can't measure the opening range.

## 2. Method
- Universe: all names the bot engaged (≥2 intents) per day, via REST. 7-day sweep (35 entries) then a
  **25-day extension (2026-05-11 … 06-18, 159 entries)** for de-monstered robustness.
- **RGNT-06-15 excluded** everywhere (a +209%-MFE freak; exception-list mechanism, extensible). RGNT-06-11
  is a separate legitimate entry, kept.
- Judge on **win% + median capture (Ret÷MFE) + avg return + give-back** — **not** total return
  (monster-driven) and **not** hit-rate alone (it misled twice).

## 3. EXIT study → TRAIL-8% is the settled winner

7-day finding held and **strengthened on 25 days**. Master (25 days, 159 entries, RGNT out):

| Exit | Win% | Avg% | **Med Cap** | Give-back |
|---|---|---|---|---|
| **TRAIL-8%** | **55** | 3.4 | **+0.22** | 7.4 |
| C — 2×EMA9 | 43 | 3.5 | −0.11 | 9.0 |
| B — VWAP | 43 | 3.6 | −0.12 | 9.3 |
| TRAIL-5% | 45 | 1.9 | −0.06 | 5.5 |
| COMBO (T8 OR 2×EMA9) | 47 | 2.1 | −0.06 | 6.8 |
| TRAIL-3% | 42 | 1.0 | −0.20 | 3.7 |
| A — EMA9 / E2 / E3 / D / F | 36–40 | 0.9–3.3 | −0.13 … −0.26 | — |

- **TRAIL-8% is the only exit with positive median capture (+0.22)** across 159 trades and the highest
  win% (55%) by a wide margin.
- **TRAIL-3% was overfit** — the 7-day capture leader (0.41) collapsed to −0.20 on 25 days (the tight stop
  gets shaken out across choppier days). The **room (8%) is what's robust.**
- **COMBO (TRAIL-8% OR 2×EMA9, first-to-fire) does NOT beat pure TRAIL-8%** — "whichever fires first" can
  only exit *earlier*, never hold longer, so it can't add C's monster-holding.
- **Multi-layer "2-of-3" (E2)** underperforms — its ingredients (swing-break / volume-dry / red-bar) are
  correlated, so it fires about as early as a single EMA9 cross.
- C/B-VWAP win only on monster-driven *total* return; they bleed the median trade.

## 4. ENTRY study → PRIOR wins; the 09:25/7-min sharpening failed to beat it

Tested whether a 09:25 / 7-min baseline / frozen-high / no-VWAP-EMA structure beats PRIOR. Three levers,
all dead ends (exit fixed at TRAIL-8%, one change at a time):

| Variant | Win% | Avg Ret% | Med Cap | Scratch% | Hit@20% |
|---|---|---|---|---|---|
| **PRIOR** (5m/09:30/VWAP+EMA) | **55** | **3.4** | **+0.22** | **51** | 41% |
| NEW-7min (no filter) | 47 | 2.5 | −0.04 | 62 | 43% |
| HYBRID (7m/frozen + VWAP+EMA) | 47 | 2.5 | −0.05 | 61 | 43% |
| NEW7 vol 2.0× / 2.5× / 3.0× / 4.0× | 47/48/49/40 | 2.2/2.1/2.3/1.9 | ≈−0.05 | 59–64 | 41/40/39/37% |

- **Structure:** the 09:25/7-min/frozen trigger catches marginally more runners (and gets CRVO/QUCY
  *earlier & cheaper*, and catches INHD/EZGO that PRIOR width-caps) **but** floods in faders — worse net
  expectancy (avg 2.5 vs 3.4, win 47 vs 55, median capture negative). Higher recall did **not** pay.
- **Hybrid (VWAP/EMA added back) is INERT** on this structure — it rejected **1 breakout out of 492**.
  A frozen-high volume breakout is intrinsically already above VWAP/EMA9. The earlier "dropping VWAP/EMA
  hurt precision" attribution was a **confound** — the precision gap is **structural**, not the filter.
- **Volume sweep — no sweet spot:** raising the multiple (1.5→4.0×) cuts trades **indiscriminately** —
  scratch% stays ~60%, avg return *falls*, hit-rate bleeds at every step. Volume does not separate
  runners from faders here.
- **Without-monsters clincher:** strip INHD (+314%) and QUCY (+82%) and the new structure **collapses
  (avg 2.5 → 1.7)** while PRIOR is **robust (3.4 → 3.1)**. The new trigger's edge was 2 names.

**Decision:** no variant beats PRIOR on net expectancy, and the new structure leans on 2 monsters →
**ENTRY = PRIOR. Stop adding levers.**

## 5. Per-runner (illustrative — the structure's local wins that still didn't win in aggregate)
| Runner (day) | gain% | PRIOR | NEW-7/HYBRID |
|---|---|---|---|
| CRVO 06-18 | 57 | 09:35 @5.33 +15% | 09:34 @4.97 +23% |
| QUCY 05-15 | 64 | 09:44 @2.97 +53% | 09:32 @2.60 +82% |
| INHD 06-08 | 314 | SKIP (width-cap) | 09:32 @1.25 +64% |
| EZGO 06-17 | 18 | SKIP (width-cap) | 09:33 @1.45 +27% |

## 6. Honest framing (do not oversell)
ORB is a **thin-edge, runner-dependent strategy.** Best achievable is **~3.4% avg/take, 55% win, +0.22
median capture** — profit lives in the **tail**, most entries are scratches. It is a **leading candidate
for live, NOT a proven verdict.** Known residual cost: the 12% width cap rejects rare monsters (INHD
+314%) — accepted, since removing it admitted big losers.

**Gap-through caveat (kept on record):** TRAIL-8% is a **hard intrabar stop**, so it is exposed to
gap-through slippage (cf. the CDT −3.7% incident, 2026-06-18). Backtest fills are modeled at the stop
(open on a gap-down) — **optimistic on thin microcap books**, so the live trailing edge may erode. The
live version needs the **intrabar execution layer** (bar-close entry is late on a fast open) and
**forward validation** before go-live.

## 7. Guardrails
- One change at a time (entry varied, exit fixed at TRAIL-8%; then exit varied, entry fixed).
- RGNT-06-15 excluded from every number; new outliers (INHD/QUCY) reported with/without.
- 25 days is real but finite — **leading candidate, not a verdict.** Totals remain monster-sensitive →
  trust win% + median capture + avg return.
- Skipping faders counts equal to catching runners.

## 8. Next steps (deployment readiness)
1. **Scope intrabar execution for ORB** on the current LEVELONE feed (no TIMESALE dependency — the 8%
   trail forgives coarse data; ORB is time-separated from P1/P4/P5/ATR). Per-path isolation, ORB policy,
   gap-through frequency. (Design/read-only first.)
2. **Forward validation** of PRIOR+TRAIL-8% before any go-live.
3. Go-live (later): small, attended, sized for a thin-tail edge.

## 9. Intrabar execution build + results (2026-06-18 pt2)
The flag-gated ORB intrabar layer was built and validated on stored bars (backtest logic; not yet
production-wired). Findings:
- **PARITY — EXACT ✅:** intrabar module in `bar_close` mode == canonical ORB on **159/159 trades** (all 25
  days), 0 mismatches. Backward-compatible by construction; default-off is inert.
- **Gap-through frequency (TRAIL-8%, hard intrabar stop):** a 1-min bar dropping >8% below prior close while
  in a position = **15 / 4,372 held bars = 0.34% of bars; 9.4% of trades**; median overshoot 9.6% (~1.6%
  beyond the stop), worst 14–16%. **Rare and bounded → a LEVELONE-fed OMS can run the exit without TIMESALE.**
- **Intrabar benefit is UNMEASURABLE on stored data (don't oversell):** OHLC-proxy says +4.4pp/trade but is
  optimistic (ideal fill at OR_high). The more-faithful captured-tick replay *contradicts* it — the ticks are
  **watchlist-gated** (start when the bot subscribed the name, i.e. at/after breakout), so "first tick ≥
  OR_high" chases: CRVO +15.7% ≈ bar-close +15.1% (marginal), ATPC fills at 3.95 (late-chase artifact, −7.1%).
  **The real benefit answer requires the small attended live run.**

## 10. ⚠️ The coverage gate (the binding constraint for go-live)
A coverage skip-guard (arm ORB only if the live feed saw the OR window 09:30–09:34; else skip — no chasing)
is **correct and necessary**, but on the 3 days with tick data it would **ARM 0 / SKIP 10** of the ORB
takers: **every ORB winner was subscribed at/after its breakout, never during its opening range.** The bot
*does* run a pre-open scan (~6 names/day) but it surfaces the **fader watchlist** (06-18: WKSP/CAST/CDT/LNKS/
BYAH/APWC), NOT the runners (CRVO/ATPC surfaced at breakout). So under current scanner timing ORB fires ~0
useful trades and a live run has nothing to test. **The binding gate is the scanner's pre-open candidate
SELECTION** — it must surface the runner-candidates by ~09:25. This is NOT an over-flag and NOT the intrabar
wiring; it is the prerequisite for ORB to trade at all. Resolve before production-wiring + live run.

## Scripts
- [`scripts/orb_exit_backtest.py`](../scripts/orb_exit_backtest.py) — exit-sweep engine (REST pricehistory + bar cache).
- Entry study (structure / hybrid / volume sweep), 25-day extension, intrabar parity/benefit, coverage guard —
  run from the same cached bars (VPS `/tmp/orb_*.py`, cache `/tmp/orb_bars.pkl`).
