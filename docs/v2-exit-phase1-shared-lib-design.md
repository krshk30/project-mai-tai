# Track 2 Phase 1 — shared ExitEngine library extraction — DESIGN

**Status:** design for review. **No code yet.** Phase 1 of the split build (operator-sequenced).
**This is a pure, behavior-neutral relocation** — its ENTIRE job is to move the exit ladder into a
neutral shared package that both `strategy_core` and (later) the OMS import, while leaving the live
momentum-bot exit behavior **provably byte-identical**. No v2, no OMS, no new logic.

**Why Phase 1 ships alone (operator rationale):** it touches the **live momentum-bot exit path**
(`exit.py` / `position_tracker.py` import graph in the strategy-engine), so it must ship and be verified
as a **no-op** before any v2 integration (Phase 2) rides on it. Phase 2 is then 100% new code importing
the frozen shared lib — touching nothing that already works.

---

## 1. The hard constraint (the whole point)

**Existing momentum-bot exit behavior (macd_30s / schwab_1m / polygon_30s, etc.) must be byte-identical
after this change.** Every existing import site keeps working unchanged, and the exit decisions
(scale-outs, floor ratchet, hard stop, tier exits) are produced by the same code. The proof obligation
(§4) gates everything downstream — a failed neutrality proof stops Phase 2.

---

## 2. What moves, and where (the extraction)

Create a neutral **leaf** package `src/project_mai_tai/exit_logic/`:

| New module | Contents (moved verbatim) | From |
|---|---|---|
| `exit_logic/config.py` | `TradingConfig` (pure `@dataclass`) + the `make_1m_variant()` / `make_30s_variant()` factories | `strategy_core/trading_config.py:7` |
| `exit_logic/position.py` | the pure `Position` value object (entry/peak/tier/floor/scale math: `update_price`, `_calculate_floor_pct`, tier upgrades, `get_scale_action`, `is_floor_breached`, …) | `strategy_core/position_tracker.py:20` |
| `exit_logic/engine.py` | `ExitEngine` (`check_hard_stop`, `check_intrabar_exit`, `check_exit`) | `strategy_core/exit.py:10` |

**Verified-clean import surface** (so `exit_logic` is a true leaf the OMS can import without dragging in
the engine): `ExitEngine` imports only `logging` + `TradingConfig`; `Position` imports only stdlib
(`csv`/`json`/`datetime`/`pathlib`) + `strategy_core.time_utils` + `TradingConfig`. `exit_logic` →
`strategy_core.time_utils` is the one back-edge, and it's fine — `time_utils` is itself a pure leaf
(datetime/zoneinfo only) that **the OMS already imports** (`oms/service.py:41`). Nothing in the cluster
touches `strategy_engine_app`, `schwab_streamer`, `bar_builder`, `indicators`, `polygon_30s`, the DB, or
the gateway.

### The re-export shims (this is what makes it behavior-neutral)
The old `strategy_core` modules become thin re-exports, so **every existing import across the codebase
resolves to the relocated symbol unchanged**:
- `strategy_core/exit.py` → `from project_mai_tai.exit_logic.engine import ExitEngine` (+ `__all__`).
- `strategy_core/trading_config.py` → `from project_mai_tai.exit_logic.config import TradingConfig,
  make_1m_variant, make_30s_variant`.
- `strategy_core/position_tracker.py` → keeps **`PositionTracker`** (the file-persisting collection — does
  NOT move; it's the momentum bots' state home) but imports `from project_mai_tai.exit_logic.position
  import Position`.

So `StrategyBotRuntime` (`strategy_engine_app.py:290-291`) still does `ExitEngine(definition.trading_config)`
+ `PositionTracker(...)` and gets the same objects running the same code. **Zero call-site edits in the
bots.** `from project_mai_tai.strategy_core.exit import ExitEngine` anywhere keeps working.

### Why extract `Position` now (not just `ExitEngine`)
Two reasons: (1) the OMS (Phase 2) must reuse the **exact** floor/scale/tier math — reimplementing it OMS-
side is the drift risk we're explicitly avoiding; (2) it serves the split-build intent — Phase 1 absorbs
**all** the behavior-neutral live-path relocation, leaving Phase 2 as purely-new OMS code. `PositionTracker`
(the file I/O collection) deliberately stays behind: the OMS uses DB-backed state (Phase 2 §2), so it never
imports the file-persisting part.

---

## 3. What is explicitly NOT in Phase 1

- No v2 anything. No OMS changes. No `oms_managed_positions`, no fill→ladder feed, no gateway-consumer
  registration. No flag (a pure refactor changes nothing observable — nothing to gate).
- No logic edits — not a single behavioral line changes. Pure relocation + re-export. If a "cleanup"
  temptation arises mid-move, it does **not** belong in Phase 1.
- No `PositionTracker` move (stays in `strategy_core`).

---

## 4. Behavior-neutral proof (the gate)

Per the endorsed behavior-identical-refactor methodology, with `origin/main` as the oracle:

1. **Characterize on unmodified `main` — golden vectors.** Build a battery of representative inputs and
   capture the outputs of the **named** methods on clean main:
   - `ExitEngine.check_hard_stop`, `check_intrabar_exit`, `check_exit` across: each scale tier (PCT2 /
     FAST4 / PCT4_AFTER2), each floor band (peak 1/2/3/4%+ → BE/0.5/1.5/trail), hard-stop hit, and each
     tier MACD/stoch exit (T1/T2/T3) with representative `indicators` dicts.
   - `Position.update_price` peak/tier/floor transitions across a price path; `get_scale_action` /
     `is_floor_breached` at the boundaries.
   Freeze these as golden expected-output vectors (the **by-name regression vs main**).
2. **Extract** — the pure move + re-export shims, zero logic edits.
3. **Re-prove identical:**
   - The **full existing suite green** (esp. `test_strategy_core.py`, exit/position tests, and the
     momentum-bot integration tests) — unchanged.
   - The **golden vectors** reproduce **identically** through the relocated `exit_logic` symbols (named
     method → identical output; main is the oracle). Any diff = an accidental logic change during the
     move → fail, fix, before Phase 2.
   - **Structural assert:** `strategy_core.exit.ExitEngine is exit_logic.engine.ExitEngine` etc. — the
     re-exports resolve to the moved objects (so all existing import sites get the same code).
   - **Import-graph guard:** a test asserting `exit_logic` imports nothing from `strategy_engine_app` /
     streamer / gateway / DB (keeps the leaf property the OMS depends on in Phase 2).

---

## 5. Deploy (behavior-neutral, but it's the LIVE momentum-bot path)

Higher stakes than Track 1's v2-only deploy: this changes the import graph of the **strategy-engine**
(the live momentum bots), so deploying it = restarting that shared process.

- **Gate:** attended, after-close, **momentum bots account-flat** (confirm macd_30s/schwab_1m/polygon_30s
  current enabled/dormant state + zero open positions at the restart moment). Standard live-restart
  choreography for the strategy-engine.
- **Risk profile:** a pure import relocation — the realistic failure mode is an **import/startup error or
  a circular import**, not a logic change (the golden vectors + suite rule out logic drift pre-deploy).
  So the prod verification is: **strategy-engine starts clean** (no import error), heartbeat resumes,
  and the momentum-bot exit path continues firing normally (watch the next live exit / the exit-decision
  logs match prior shape). The golden-vector suite **is** the behavior proof; prod confirms the wiring.
- **No flag** (nothing to toggle — it's neutral). **Rollback** = revert the commit + restart (clean, since
  it's a pure relocation; the re-exports mean even a partial issue is contained to import paths).

---

## 6. Open questions for review (before building Phase 1)

1. **`Position` scope.** Confirm extracting the **whole** `Position` value object to `exit_logic` (my
   recommendation, §2) vs the minimal "ExitEngine + config only" (leaves `Position` in `strategy_core`,
   pushing its extraction into Phase 2 against the split-build intent). I recommend the former.
2. **`make_*_variant` location.** Move the factories to `exit_logic/config.py` (keeps config self-
   contained for the OMS to reuse `make_1m_variant` in Phase 2) vs leave them in `strategy_core`. Lean:
   move them (they build `TradingConfig`; the OMS wants `make_1m_variant`).
3. **`time_utils`.** Leave the `exit_logic → strategy_core.time_utils` back-edge (recommended — it's a
   pure leaf the OMS already imports) vs also relocate `time_utils` into `exit_logic` (more churn, no
   real gain). Lean: leave it.

---

## 7. Honest boundaries

- This delivers **nothing functional** on its own — it's scaffolding whose only merit is being provably
  behavior-neutral. That's the point: de-risk the live-path change in isolation before Phase 2's new code.
- The neutrality proof (§4) is the deliverable that matters; if the golden vectors don't reproduce
  identically, the extraction is wrong and Phase 2 does not start.
- Per-quote vs per-bar cadence and the position-table choice are **Phase 2** decisions (recommendations
  flagged separately) — they do not affect this extraction.
