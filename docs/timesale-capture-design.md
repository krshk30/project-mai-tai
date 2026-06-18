# TIMESALE_EQUITY tick capture — additive, capture-only (DESIGN — review before PR)

**Status:** DESIGN. No code yet. Per streamer-design-first discipline (the v2 streamer is the flap-prone
LNAI watch-item), bringing the design + load/entitlement assessment for review BEFORE implementing.
**Goal:** capture true time-&-sales (TIMESALE_EQUITY) so tick-confirmation/ATR research runs on trade-grade
ticks, not the throttled LEVELONE last-price snapshots we have today. CAPTURE ONLY — no execution change.

## 1. Entitlement (open item #1 — needs confirmation)
- `market_trade_ticks` has **0 TIMESALE rows** because the v2 streamer **never subscribes TIMESALE** (it does
  CHART_EQUITY + optional LEVELONE only) — NOT because Schwab rejected it. So entitlement is UNKNOWN.
- The legacy `schwab_streamer.py` defaults `_timesale_service_available=True` and downgrades gracefully on a
  rejected SUBS. The build must do the same: attempt the TIMESALE SUBS, and on a non-OK response code, log it,
  UNSUB, mark unavailable, and continue (LEVELONE+CHART_EQUITY unaffected). **Confirm entitlement by enabling in
  a paper/quiet window and reading the SUBS response — that's the only definitive test.**

## 2. Capture-only — structurally guaranteed (the safety core)
- v2 strategy entry uses **CHART_EQUITY bars ONLY** (`_handle_message`: CHART_EQUITY → bar feed; LEVELONE/TIMESALE
  → teed to `on_tick`, never read by the strategy). `on_quote`/trade ticks do not drive v2 entry.
- `SchwabV2TickWriter` is a **pure DB tee**: buffers ticks, batch-flushes to `market_trade_ticks`/`_quote_ticks`
  off the event loop, `ON CONFLICT DO NOTHING`, **never backpressures the streamer** (drops oldest on overflow),
  "shares nothing with the strategy/bar feed, not execution-critical."
- It does **NOT** publish to any Redis stream. The execution-facing trade-tick stream (OMS hard-stop;
  strategy_engine momentum bots' `handle_trade_tick`/`_evaluate_intrabar_entry_from_trade_tick`) is fed by the
  GATEWAY/Massive path, **not** the v2 tee. So TIMESALE capture reaches the DB only.
- **Conclusion: TIMESALE capture cannot touch entry, ATR, the OMS, or any live bot.** The live ATR path
  (CHART_EQUITY + bar-close) is provably unchanged.

## 3. Implementation (additive, flag-gated, default-off)
In `schwab_v2_streamer.py`:
- Add `TIMESALE_EQUITY_SERVICE = "TIMESALE_EQUITY"`, `TIMESALE_EQUITY_FIELDS = "0,1,2,3,4"` (symbol, trade-time,
  last-price, last-size, seq).
- New flag `strategy_schwab_1m_v2_timesale_capture_enabled` (default **false**). When off: no TIMESALE SUBS sent,
  branch unreachable — **byte-identical to today** (same guarantee LEVELONE capture already has).
- When on (and `on_tick` wired): send an additional SUBS for TIMESALE alongside CHART_EQUITY+LEVELONE on the
  SAME WebSocket (one connection). Add a `_handle_message` branch: TIMESALE content records → build `SchwabTick`
  with `service="TIMESALE_EQUITY"`, trade-only (price+size from fields), tee to `on_tick`.
- `SchwabV2TickWriter` persists with `service="TIMESALE_EQUITY"`. The dedupe unique key is
  `(provider, service, symbol, event_ts, raw_hash)` — **TIMESALE rows are fully separable from LEVELONE rows by
  the `service` column**; no schema change, no migration.
- Graceful-rejection handling per §1.

## 4. LOAD / flap risk (your flag — the real gating question)
- TIMESALE pushes **every trade** vs LEVELONE's throttled snapshots → materially higher inbound rate on the v2
  streamer (the LNAI-flap watch-item streamer). For the v2 watchlist (~5–9 penny-stock symbols), estimate
  ~10–50 trades/sec aggregate in active periods — manageable for the async parse-and-tee loop, BUT it's
  additive CPU on the streamer event loop, and the streamer's stability is the open concern.
- Mitigations: (a) the tee NEVER backpressures (drops oldest on overflow, counts it) — DB load can't stall the
  WS; (b) flag-gate = instant disable; (c) the parse path is the same code as LEVELONE, just more records;
  (d) start on the existing small watchlist.
- **Required validation:** after enabling, monitor the v2 streamer health for ≥1 session — reconnect/flap rate
  (vs the LNAI-flap baseline), `on_tick` overflow-drop counter, CPU — and confirm no degradation. If the flap
  rate rises, disable via the flag and reassess (e.g., separate WS connection for TIMESALE, or symbol subset).

## 5. Staging (reviewed, not admin-merged, not deployed blind)
1. Design review (this doc). 
2. Build behind the flag (default off). Characterization: with flag off, CHART_EQUITY + LEVELONE capture
   byte-identical (the streamer's existing default-off guarantee). Unit test the TIMESALE parse + writer
   service tagging.
3. PR — normal review, **NOT admin-merged**.
4. Enable in a quiet/paper window (flag on); read the SUBS response (entitlement §1); monitor streamer health
   §4 for a session; confirm TIMESALE rows accruing with density >> LEVELONE.
5. If clean, leave on to accrue ~10 trading days by early July for the Option-B decider.
6. Rollback: flip the flag off (instant; no redeploy if runtime-config, else a quick restart).

## 6. Option-B impact (the payoff)
Once ~10 days of TIMESALE accrue, the Option-B per-path test runs on **trade-grade** ticks: lower no-tick rate,
higher-resolution upticks>downticks, and a real-trade basis for the live go/no-go. Re-confirm the LEVELONE-grade
ranking (P5/P1 help, P4 doesn't) holds on real trades. Also unblocks dense-tick ATR hold-confirmation testing.
