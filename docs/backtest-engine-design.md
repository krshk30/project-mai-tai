# Validated backtest engine — design

**Status:** DESIGN-FIRST (no code until operator approves). Grounded in a read-only map of the live strategy code (`file:line` cited) + the real `market_capture_*` data + the hand-verified cases.
**Author:** session 2026-07-07.

## 0. Mandate
Every backtest this session was a throwaway `/tmp` script, and **≥3 real bugs changed real strategy conclusions**: the exit-model fake-win (trail peak seeded at the entry bar's high, not the fill → a loss shown as a win); the CELZ bar-based running-high phantom re-entries (a stale level re-crossed every tick → **93 trades / −$39** shown for what was really **23 / +$1.91**); and SDOT still un-chart-checked. The operator has traded ATR/ORB **profitably for years**, so his experience may be right and the buggy backtests wrong. This engine must be **trustworthy by construction** — validated against ground truth, not assumed correct — so it can re-adjudicate those conclusions.

## 1. Principles
1. **Production code, in-repo, version-controlled, own CI suite** — not a script.
2. **Mirror live BY CONSTRUCTION where possible:** the engine *imports the same pure decision leaves the live code runs*, so it cannot silently drift from live. Where a leaf isn't pure yet (ORB entry/counting lives inside a service), the design extracts it into a shared pure module both the live service and the engine import.
3. **Validated against hand-verified ground truth:** the engine is trusted only after it reproduces a suite of hand-verified real cases (broker fills / chart-checked days). A case it can't reproduce is a bug in the engine, full stop.
4. **Honesty over optimism:** real ~3s fill latency and full per-name spread are charged; "instant fill / no spread" is the optimism that lied.
5. **Component decomposition:** separable, independently unit-tested parts — not one tangled script.
6. **One strategy first (ORB running-high — most hand-verified cases), proven, then extend.**

## 2. What already exists (reuse, don't reinvent)
The live code already contains pure leaves the engine must reuse so it mirrors live:
- **`OrbTickAggregator`** (`strategy_core/orb_tick_aggregator.py:39-55`) — tick→1-min bar; `add_tick` returns a *closed* bar only when a tick rolls into a later minute. The live ORB builds its decision bars with this; the engine builds them the same way.
- **`exit_logic.ExitEngine` + `Position` + `TradingConfig.make_v2_variant`** (`exit_logic/{engine,position,config}.py`) — pure v2 exit ladder (hard/floor/scale/peak-ratchet). The OMS drives these for v2 exits; the engine drives the *same* objects.
- **`_ratcheted_trailing_stop`** (`oms/service.py:2343-2351`, pure staticmethod) — the ORB trail math (bid-only, stop only rises). Lift to a shared pure module.
- **`analysis/atr_flip.py::compute_atr_trail`** — the ATR oracle the live `_update_atr_state` is a port of (pinned by a determinism test); the engine's ATR entry reuses it.
- **NOT reusable / to be replaced:** `scripts/orb_exit_backtest.py`, `orb_fill_slippage.py`, the `/home/trader/orb_*_bt.py` throwaways, `analysis/replay_study.py` (a forward-return study, not a ladder sim). `tests/replay/` is parity fixtures only — **no reusable full engine exists today.**

The one component that is NOT a pure leaf yet: the **ORB running-high entry + re-entry counting** (inside `orb_app.py`, coupled to Redis/OMS). §3.2/§3.3 propose extracting it — this is the component with the bug history, so mirror-by-construction matters most here.

## 3. Component architecture (each independently testable)

### 3.1 Data layer
- **Source (ground truth, provider `massive`/Polygon):** `market_capture_bars` (60s OHLCV+vwap+transactions, key `event_ts`, ~95 names, **coverage 2026-06-23 → present**), `market_capture_trades` (26M real trades — for intrabar fills), `market_capture_quotes` (12M NBBO — for spread). **NOT `strategy_bar_history`** (per-strategy, provider-limited, truncated — a source of the phantom-flip confusion).
- **Decision bars:** build 1-min bars from `market_capture_trades` via **`OrbTickAggregator`** (exact live mirror); cross-check against `market_capture_bars` (both from the massive feed). Only 60s bars are pre-built — **intrabar decisions/fills must come from the trades table.**
- **Coverage caveat (design constraint):** capture begins **2026-06-23**; the older 06-10→06-18 exit study is *unavailable* — the validation suite is bounded to 06-23→present.
- Testable via: fixture windows (a name-day's trades/quotes) → deterministic bar output.

### 3.2 Entry detection (strategy predicates)
Mirror the live rules exactly:
- **ORB running-high** (`orb_app.py:525-557`): **bar-close only** (`_on_bar` fires only on a closed bar, `orb_app.py:385-387`). First bar ≥09:25 ET *seeds* `running_high`. A break = `bar.high > running_high` within 09:30–10:00 ET; fill = the broken `level` unless `bar.open > level` (gap) → `bar.open`; gated by the 1.5% gap-cap. **Then `running_high = max(running_high, bar.high)` at the end of *every* bar** (`:557`).
  - **⚠️ The CELZ-bug guard (most important line in the engine):** the running high advances **once per closed bar** to `max(rh, bar.high)`, including the breaking bar — it is **never** a per-tick stale level. Modeling it per-tick is what produced 93 phantom CELZ re-entries. The engine's running-high tracker is bar-close-advancing by construction (reuse the live `_on_bar_running_high` logic via §3.3 extraction).
- **ATR flip** (`schwab_1m_v2.py:645-773`, variant B): bar-close touch of the prior short-segment trail (`atr_prev_trail`), one entry per short segment; plus the intrabar **hold-confirm** (`on_quote:476-529`, `_resolve_hold:531-555`) whose **`fallback_thin` branch enters anyway** on thin coverage (the current live-dominant "Path-B" — the engine MUST model it). Reuse `analysis/atr_flip.py` for the ATR state.
- Testable via: a bar sequence → the exact set of (entry_time, entry_price) the predicate emits.

### 3.3 Re-entry / trade counting — THE component the CELZ bug lived in (most scrutiny)
Mirror the live ORB state machine (`orb_app.py` `_SymbolState:96-120`, `_can_enter:392-396`, `_ENTRY_ATTEMPT_CAP=2:141`, `_apply_order_event:451-480`):
- `attempts` increments **only** on an ORB entry emit (`:550`), cap 2 — OMS cancel churn never burns the cap.
- `pending` on emit; `traded=True` on a confirmed BUY fill; **`traded` clears on flat** (a SELL fill to zero) → a genuine RECLAIM is possible (the CANF/DSY case).
- A **genuine distinct entry** = break → fill → exit → flat → new break (≤ cap). A **suppressed** one = still pending/holding, or `attempts ≥ 2`. Stale-pending expiry (`_expire_stale_pending:483-498`).
- **Design recommendation:** *extract* this decision logic (`_on_bar_running_high` + `_can_enter` + the `_SymbolState` transitions in `_apply_order_event`) into a **pure module** (e.g. `strategy_core/orb_decision.py`) that BOTH the live `orb_app.py` service and the backtest engine import — so the counting mirrors live by construction and the bug can't recur in a re-implementation. This is a behaviour-identical refactor of the live ORB service (design-first, characterization-tested against the live outputs — the F-series discipline). *Alternative:* re-implement in the engine and rely solely on the hand-verified cases to validate — lower up-front cost but re-implementation can drift from future live changes. **Recommend the extraction** (mirror-by-construction is the whole point).
- Testable via: the CELZ 06-30 fixture must yield **23 genuine breaks, not 93**; the KIDZ 07-06 fixture must show the 2-attempt cap suppressing the 09:53 break.

### 3.4 Fill modeling — honest latency
Mirror the live fill path + charge real latency:
- **Entry:** the OMS quote-priced limit = `min(ask + 1 tick, break × (1 + gap_cap))` (the live Piece-1 logic); the Webull fill lands **~3s later** (measured KIDZ 07-06: submit→fill **+2.97s**). Fill only if the ask is at/below the limit within the window, else **abandon `ASK_PAST_GAP_CAP`** (exactly the KIDZ 09:31 abandon vs the 09:32 fill). Read the ask from `market_capture_quotes` at fill-time.
- **Stop exit:** stop-hit → Webull native STOP fill **~2.84s later** at the then-bid.
- **The point:** filling *at the trigger tick* is the optimism that flipped intrabar KIDZ **+$0.22 → −$1.00**. The engine fills at the quote **~3s after** the trigger, never at the trigger.
- Bot/OMS legs (submit +0.28s, arm +0.02s) are sub-200ms → no slack to model; only the ~3s venue legs matter.
- Testable via: KIDZ 07-06 — buy 5 @ **1.155** (09:32:03) → sell @ **1.12** (09:32:18), **−$0.175**; the engine must reproduce this to the cent.

### 3.5 Spread modeling — full spread, charged honestly
- **Per-name spread from `market_capture_quotes`** (bid/ask), NOT a flat constant — measured range **0.34%–0.87%** across the qualified names (CELZ 0.34 … DSY 0.87).
- Charge the spread on every fill (entry at ask, exit at bid).
- **Report modeled-vs-verified separately:** cross-check modeled fills against real broker fills via the existing `orb_fill_slippage.py` join (`broker_orders.payload→metadata.orb_intended_or_high`/`stop_price` → `fills.price`). The engine reports "modeled net" and "real-fill net (where available)" side by side — spread is the thing that turns gross-positive into net-negative, so it's reported explicitly.
- Testable via: a name-day's modeled spread cost vs the quote-derived spread.

### 3.6 Exit logic — match the REAL OMS behavior
- **ORB:** the in-memory trailing stop — reuse `_ratcheted_trailing_stop` (bid-only ratchet, stop only rises). **HWM seeded at the FILL price** (`oms/service.py:2574` — `max(entry_price, prior_hwm)`), *not* the entry bar's high (the fake-win bug). Trigger when bid/last ≤ stop (`_resolve_hard_stop_trigger_price:2313-2342`). Exit fill per §3.4 (native STOP ~2.84s, or a fallback limit — real ORB stops sometimes reject → limit exit, per JEM 07-01/DSY 07-02).
- **v2:** drive the *same* `Position` + `ExitEngine` objects the OMS uses (`_evaluate_v2_managed_exit:1440-1537`): `update_price(bid)` → `check_hard_stop` then `check_intrabar_exit` (precedence hard>floor>scale, one action/quote); exit fills at the **leg LEVEL** price (hard = `entry×(1−stop%)`, floor = `floor_price`, scale = scale-level), not the observed bid — mirroring the OMS by construction.
- Testable via: seed a `Position`, feed a price path, assert the exact scale/floor/stop sequence matches an `ExitEngine` reference.

## 4. Validation approach — the heart (trust = reproduces ground truth)
A CI-gated **hand-verified case suite**. The engine is trusted *only* if it reproduces every case; a new conclusion is quoted only from a green engine.

**Primary accuracy gate (broker-exact):**
- **KIDZ 07-06** — real `live:orb`/Webull fills: buy 5 @ 1.155 (09:32:03) → sell @ 1.12 (09:32:18), hold 15s, **−$0.175**, `HARD_STOP_NATIVE_BACKUP`; entry #1 (09:31) abandoned `ASK_PAST_GAP_CAP`; 2-attempt cap suppressed 09:53. The engine must match to the cent + reproduce the abandon + the cap.

**Chart-validated cases:**
- **CELZ 06-30** — continuous running-high = **23 genuine breaks / +$1.91** (trended 2.7→4.67). The buggy **93 / −$39.10 must NOT reproduce** (regression guard).

**Flagged-unvalidated (must be hand-checked before trusted):**
- **SDOT 06-26** — currently **−$10.97 intrabar / −$6.73 bar-close**, the biggest loser, **never chart-checked**. The suite encodes it as PENDING-VALIDATION: the operator eyeballs it vs the chart; only then does it become a trusted anchor. (After being wrong on CELZ, no single big-loser is trusted un-checked.)

**Full 15-name-day table** (corrected: qty 5, 3s latency, per-name spread; IB=intrabar, BC=bar-close, A/B=freshness) — the engine must reproduce each cell; whole-strategy expectation = **net-negative in all four variants, least-bad BC-B −$8.18/15 days**. (Retracted pre-fix numbers — IB-A 185/−69.81 etc. — must NOT reproduce.)

**Regression guards (the 4 documented real bugs, as explicit failing-then-passing tests):**
1. phantom re-entry (bar-based running-high) → the CELZ 23-vs-93 test;
2. fake-win exit-peak (HWM seeded at bar-high) → a case where wrong-seeding shows a win but correct-seeding (fill price) shows the real loss;
3. instant-fill optimism → KIDZ intrabar +$0.22 (no-latency) vs −$1.00 (3s latency);
4. zero-spread → gross-vs-net divergence on a high-trade-count name.

**Cross-check vs real fills:** where broker fills exist (`fills`/`broker_orders` for strategy=orb — KIDZ, DSY 07-02 reclaim, CANF, IVF, SDOT, TDTH), the engine's modeled fills are reported against the actual fills (`orb_fill_slippage.py`-style join).

## 5. Where it lives + enforcement as the single backtest path
- **Location:** in the code repo, e.g. `src/project_mai_tai/backtest/` (engine components) + `tests/backtest/` (the validation suite as CI tests) + `docs/backtest-engine-design.md` (this doc). Reuses `strategy_core`, `exit_logic`, `analysis/atr_flip.py`.
- **CI gate:** the hand-verified case suite runs in `validate` (the same gate as everything else). A change that breaks a case reds CI. This is what keeps it trustworthy over time.
- **Enforcement (so we don't slip back to throwaway scripts):**
  1. **Quarantine the throwaways:** move `scripts/orb_exit_backtest.py` + `orb_fill_slippage.py` (keep the real-fill join, fold it into the engine's cross-check) into `scripts/legacy/` or delete; document in the README that backtests go through the engine.
  2. **A single documented entry point** (`python -m project_mai_tai.backtest <strategy> <date> <symbol>`) — the only supported way to run a backtest; conclusions cite an engine run + the passing case suite.
  3. **README + CONTRIBUTING note:** "no new /tmp backtest scripts — add a strategy adapter to the engine + a hand-verified case." Optionally a CI check that fails if a `*_bt.py`/backtest script is added outside `src/backtest/`.
  4. Every strategy conclusion in the handoff/reports must reference the engine + the green case suite (not an ad-hoc script).

## 6. Scope discipline + build sequence
Build the **core + ORB running-high first** (most hand-verified cases), prove it, then extend. Never all strategies at once.

- **Step 1 — Data layer** (`market_capture_trades`→`OrbTickAggregator` bars + quotes/trades access). Validate: deterministic bars for a fixture window; coverage/loader tests.
- **Step 2 — Fill + spread models** (3s latency, per-name spread, quote-priced entry + abandon). Validate: KIDZ 07-06 entry fill = 1.155 + the 09:31 abandon; spread cross-check vs quotes.
- **Step 3 — ORB entry + re-entry counting** (extract `orb_decision.py` from `orb_app.py`, shared with live; characterization-test the live service is byte-identical after extraction). Validate: CELZ 23-not-93, KIDZ cap-suppresses-09:53.
- **Step 4 — ORB exit** (reuse `_ratcheted_trailing_stop`, HWM=fill price, tick-trail stop-first). Validate: KIDZ −$0.175 end-to-end; the exit-peak regression guard.
- **Step 5 — Full ORB validation suite** — reproduce the 15-name-day table + the regression guards; hand-validate SDOT. **Gate: ORB engine is trusted only when the whole suite is green.**
- **Step 6+ — Extend** to ATR flip (reuse `atr_flip.py` + `ExitEngine`; model the hold-confirm `fallback_thin` path) → then P1/P3/P5, each with its own hand-verified cases before its conclusions are trusted.

Each step: component built + unit-tested + its validation cases green before the next.

## 7. Open questions for operator
1. **ORB counting: extract-shared (recommended, mirror-by-construction, a behaviour-identical refactor of `orb_app.py`) vs re-implement-and-validate (faster, but can drift)?** The extraction is the trustworthy-by-construction path but touches the live ORB service (design-first + characterization, like the F-series).
2. **Decision bars: build from `market_capture_trades` via `OrbTickAggregator` (exact live mirror) or read `market_capture_bars` (pre-built 60s)?** Recommend the former (mirror), with the latter as a cross-check.
3. **SDOT and the other un-chart-checked name-days:** which days will you eyeball vs the chart to become trusted anchors? (The suite needs a few operator-validated cases beyond KIDZ/CELZ.)
4. **Enforcement teeth:** do you want a CI check that *fails* on a new backtest script outside `src/backtest/`, or is the README convention enough?
5. **Coverage:** the engine can only backtest 2026-06-23→present (capture start). OK, or do we backfill earlier Polygon data first?
