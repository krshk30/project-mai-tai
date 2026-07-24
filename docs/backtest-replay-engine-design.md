# Design: Backtest REPLAY engine — replay the live code, never re-implement it

**Status:** DESIGN-FIRST 2026-07-24. The durable fix for the chronic "backtest ≠ live" bug class
([[project-mai-tai-backtest-fidelity-replay]]). Foundation = the canonical live spec
`docs/schwab-1m-v2-live-spec.md` (#539) — the replay must reproduce THAT, exactly.

## The problem (why re-implementation keeps biting)
Today there are SEVERAL backtest harnesses (`backtest/v2_sim.py`, `backtest/orb_sim.py::simulate_resting`,
`atr_cw_v2_variants`), each **re-implementing** pieces of the strategy with their OWN band / exit / window /
fill. Every drift is a live-money-relevant bug: 07-23 alone surfaced 3% band vs live 0.5%, `ExitEngine` vs the
live `cw_exit_decision`, post-market entries vs the 16:00 gate. The engine "imitates" live instead of running it.

## The principle
**Feed historical data through the ACTUAL live entry + exit code, reading the same `Settings`.** Anything the
replay still re-implements is a fidelity risk and must be (a) minimised and (b) pinned by a parity gate vs real
fills. Shared code cannot drift by construction.

## What's SHARE-ABLE (verified from code)
| Live component | Where | Replay-able? |
|---|---|---|
| **Entry signal** | `SchwabV2Strategy` (settings-driven; consumes bars/quotes via `on_bar`/`on_quote`; emits intent *drafts* to `_pending_intents`; no DB/broker coupling) | ✅ instantiate + feed |
| **Exit decision** | `exit_logic/cw_exit.py::cw_exit_decision` (pure fn: entry, bid, pcts, floor_enabled → action) | ✅ call directly |
| **Config** | `Settings()` (env-merged) | ✅ same object |
| **Entry-window + EH-limit routing** | the BOT (`schwab_1m_v2_bot.py::_maybe_emit`, `_apply_extended_hours_routing`) — NOT the strategy | ⚠ must EXTRACT to a shared fn or replay the bot gate |
| **OCO emit / stand-down / EOD transition** | `oms/service.py` (DB + broker coupled) | ✖ SIMULATE (decisions are simple; fills are the hard part) |

## Architecture — the replay harness
```
historical Schwab 1-min bars ─┐
historical tape (fills)      ─┤→ [1 FEED]
Settings() (same env)        ─┘
        │
        ▼
[2 ENTRY]  real SchwabV2Strategy(settings): on_bar(bar) / on_quote(quote) → capture drafts
        │  + the shared emit-gate (entry-window, EH-routing)  ← extract from the bot
        ▼
[3 FILL]   honest fill model against the tape: resting = min(ask, level*(1+band)) or MISS;
        │  reactive = marketable; EH = session=AM limit at ask; measured latency band; NO look-ahead
        ▼
[4 EXIT]   session-selected geometry (per the spec's two-geometry finding):
        │    RTH-open  → STATIC OCO: +2% target / −5% stop, first-touch on the tape (small model)
        │    EH-open   → cw_exit_decision(floor_enabled=True): +2% floor-RIDE / −5% / flip  ← SHARED
        ▼
[5 OUT]    trades + a PARITY report vs the day's REAL fills
```

## The fidelity guarantee + the honest residual
- **Shared (can't drift):** the entry signal, the CW exit decision, the config, the two thresholds we locked
  (N=3, 0.5% band). If live changes, the replay changes with it.
- **Re-implemented (the risk surface — keep SMALL + gated):** the emit-gate (mitigate by EXTRACTING it into a
  shared fn both bot and replay call), the fill model, the static-OCO first-touch, the tape feed.
- **⭐ The parity gate is the proof:** run the replay on REAL trading days and reconcile trade-for-trade vs the
  actual fills (like 07-23 SKYQ replay −1.74% ≈ real −1.57%). A CI parity test on a golden day fails red if the
  replay drifts. This is what makes "shared" *provable*, not just claimed.

## Data
- **Bars: Schwab 1-min (the LIVE source)** — NOT Polygon ([[project-mai-tai-bar-source-defect]]: only 54% of
  ATR flips agree; the input source WAS the bug). Fills/first-touch measured on the tape (`market_capture_trades`).
- Feed-coverage honesty: a name with too-sparse Schwab bars = SKIP-with-reason (never a silent absence), same as
  today's `--sheet`. (NVVE-on-07-23 was invisible for exactly this reason — surface it, don't hide it.)

## Phasing
- **P1 — Entry replay + parity.** Instantiate the real `SchwabV2Strategy`, extract the emit-gate to a shared fn,
  replay one real day, reconcile entries vs real fills. Prove the entry side is faithful.
- **P2 — Exit unification. ✅ BUILT** (`backtest/replay.py`: `replay_symbol_day` now continues past the
  entry fill into the full trade). The v2 replay exit uses ONLY the shared `cw_exit_decision` (EH floor-ride,
  tick-by-tick over the bids, reading `oms_v2_cw_*` from Settings) + a small static-OCO first-touch model
  (RTH open); `ExitEngine` is never referenced. Geometry is selected by the RTH/EH open. 07-23 SKYQ mechanic
  reproduced: neither OCO leg touched (tape 5.58-5.77 vs target 5.84 / stop 5.44) → close-at-bell ≈5.64 ≈−1.57%.
  One behavior-identical live seam: `_maybe_cw_flip_close`'s staleness clock routes through the existing
  `_now_ms()` seam (base returns wall-clock, byte-identical) so the replay's injected clock reaches the
  bar-close flip exit. (The full VPS DB reconciliation is the parity gate — run env-sourced on the box.)
- **P3 — Extended-hours ENTRY. ✅ BUILT** (`backtest/replay.py`: `replay_symbol_day` now FILLS entries
  OPENED in extended hours, both modes, through the REAL strategy code). The live EH entry is a marketable
  EH-LIMIT at the ask: **reactive-EH** = `_cw_v2_quote` (with its EH live-bar guard) + the shared
  `entry_gate.route_extended_hours` (session=AM/PM limit@ask); **resting-EH** = `_eh_resting_cross_check`
  (P-B2) emitting the marketable EH-LIMIT on the ATR up-cross. The EH-limit FILL/ABANDON model
  (`_eh_entry_reprice`) SIMULATES the OMS pre-submit re-price (`oms.service._apply_v2_eh_resting_entry` /
  `_apply_v2_eh_reactive_entry` are DB/broker-coupled → SIMULATE per this doc + Decision 2, same class as
  the static-OCO first-touch): resting fills at `min(ask, level×(1+band))` and ABANDONS a gap-through
  (`ask > cap`); reactive (P-B1 on) caps off the signal `×(1+max_cross%)` and abandons past it; no fresh
  ask ⇒ ABANDON. The EH-opened position exits via the P2 floor-ride (selected by the RTH/EH open). Flag-
  awareness: `build_replay_settings(eh_enabled=True)` turns BOTH EH flags on (`..._cw_v2_eh_resting_entry_
  enabled` + `oms_v2_eh_entry_enabled`); the **LIVE deployed defaults stay OFF**, so the default replay is
  RTH-only like production. Five synthetic CI tests (`test_backtest_replay.py` P3 block): pre-market
  resting cross→band-fill→floor-ride, reactive marketable fill, gap-through→ABANDON, the EH live-bar guard
  blocking a stale bar, and the flag-off mutation (RTH-only). **No live seam was touched** — the strategy
  EH paths + shared gate already carry the P1/P2 `_now_ms()` clock seam, and the OMS band-cap is simulated
  (not instantiated).
  - **⚠ HONEST SCOPE — EH REAL-DATA PARITY IS DEFERRED.** There are NO real EH trades yet (the live EH flags
    are dormant until enabled post-4PM / Monday), so P3 proves the EH **mechanism** on synthetic fixtures
    ONLY. This is NOT EH real-data parity — the real-fill parity gate (replay vs a real EH trade) is a
    follow-up once real EH fills exist, exactly like the P1/P2 golden-day reconciliation.
  - **Out of scope for P3:** the **Webull mirror leg** (the dual-broker bake-off in the replay) — a later
    item once the EH parity gate exists.
- **P3b (later) — Both brokers.** The mirror leg (optional, for the bake-off).
- **P4 — Deprecate the old harnesses** (`v2_sim` re-impl, `orb_sim::simulate_resting`) — one replay, one truth.
- Each phase gated by the parity test on a golden day. CI-enforced.

## ✅ Decisions LOCKED (operator 2026-07-24)
1. **Emit-gate: EXTRACT** into a shared pure fn (both the bot and the replay import it) — behavior-identical
   refactor of the live bot (characterize → extract → prove identical).
2. **Scope: strategy + exit replay** (leaner, parity-gated) — NOT the full bot+OMS mock.
3. **Exit asymmetry: model BOTH** (RTH static +2% hard-OCO vs EH +2% floor-ride) — keep as-is for now; the
   "is the asymmetry intended long-term" question stays parked for after P2.
4. **Golden parity day: 2026-07-23** (first CI fixture; NVVE 11:20@8.40 + SKYQ 15:45@5.73 are the real entries
   to reconcile). Add a clean up-flip winner as a 2nd fixture once one exists.

## Risks
- Hidden strategy infra deps surfacing when instantiated standalone (mitigate: P1 proves it early).
- Fill-model fidelity in thin pre-market (the residual; the parity gate bounds it).
- Schwab historical bar availability/retention for older days (coverage honesty).
