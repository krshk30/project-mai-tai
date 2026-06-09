# Plan: schwab_1m → v2 cutover + v2 Day-1 streamer activation

**Status: PLAN — awaiting operator review. NO execution until approved.** Design-first, like
everything in this arc. This retires two live-money bots (schwab_1m, macd_30s) and is v2's
**first live streamer activation**, so it gets a full plan + back-out before any flag flips.

**This remains PAPER-v2.** Day-1 = streamer activation only. Real-money conversion is a
**separate later step**, not in scope here.

**Gating fact (from the 2026-06-08 investigations):** Schwab permits **exactly one streamer
WebSocket session per credential** (official Streamer Guide: "a maximum of 1 Streamer
connection at any given time from a given user"; second connect kicks the first). Today that
one session is the **strategy-engine's** `SchwabStreamerClient`, multiplexing `CHART_EQUITY`
(schwab_1m) **and** `LEVELONE_EQUITIES`/`TIMESALE` (macd_30s) on one WS. v2's streamer is
`CHART_EQUITY`-only and currently OFF.

## Goal end state

- **Exactly ONE Schwab streamer session on the credential — v2's** (`CHART_EQUITY`).
- **schwab_1m retired** — v2 is its functional successor (same CHART_EQUITY 1m feed, sub-5s
  streamer latency vs the ~85s REST floor).
- **macd_30s decommissioned** — its `LEVELONE`/`TIMESALE` tick feed has no home post-cutover
  (v2's streamer is CHART-only; no REST path for tick-derived 30s bars; we are **deliberately
  NOT** extending v2's streamer to carry ticks — that would break v2's isolation). Code/config
  **preserved in git** (dormant, not erased).
- **polygon_30s untouched** (separate Polygon feed; stays parked/enabled exactly as today).
- Account flat, CYN protected, intact.

## Pinned mechanisms (read-only verified, deployed code)

- **Strategy-engine streamer is gated on enabled Schwab-streamer bots.**
  `_build_schwab_stream_client()` (`strategy_engine_app.py:7980`) returns **`None`** when
  `self.state.schwab_stream_strategy_codes()` is empty. With that None, `_run_init_phase`
  skips `self._schwab_stream_client.start(...)` (`:6187–6188`) → **the strategy-engine holds
  NO streamer session, opens no WS, fetches no streamer credentials.** This is the clean
  stand-down lever — no "empty session that Schwab idle-closes and the client re-grabs"
  problem, because the client is never built.
- **Bot enablement → routing.** `Settings` build-bot gates (`settings.py:577/585/589`):
  `strategy_macd_30s_enabled` (default **True**), `strategy_schwab_1m_enabled` (default
  False), `strategy_schwab_1m_v2_enabled` (default False). A disabled bot is **not added to
  `self.state.bots`** → out of routing entirely (no intents, no subscriptions, no processing).
- **Current env:** `MACD_30S_ENABLED=true`, `SCHWAB_1M_ENABLED=true`, `SCHWAB_1M_V2_ENABLED=true`,
  `POLYGON_30S_ENABLED=true`; v2 streamer flag **absent (=false)** → v2 is REST-only today.
- **v2 streamer subscribe-early (PR #224) is in place** — `_apply_strategy_state_event` passes
  the full watchlist to `streamer.set_desired_symbols(selected)` immediately, so on activation
  v2 SUBS right after LOGIN and Schwab holds the session (this fixed the 2026-05-23 Day-1
  empty-subscription idle-close flap).
- v2's streamer reads the **same shared OAuth token** the production streamer used. The token
  SPOF is unchanged by this cutover (Workstreams A/B cover resilience/visibility); the cutover
  only changes *which* client holds the one session.

## Cutover sequence — collision-free ordering (stand DOWN before bring UP)

The one rule: **never two concurrent streamer sessions.** Stand the strategy-engine streamer
fully down and verify it's gone BEFORE flipping v2's streamer on.

**Step 0 — pre-flight (attended).** Account-flat verified at the moment (`virtual_positions`
all zero); reconciliation = CYN-only; record exact current env values for the back-out.

**Step 1 — stand DOWN the strategy-engine streamer (retires schwab_1m + macd_30s).**
In `/etc/project-mai-tai/project-mai-tai.env`:
```
MAI_TAI_STRATEGY_SCHWAB_1M_ENABLED=false
MAI_TAI_STRATEGY_MACD_30S_ENABLED=false
```
(leave `POLYGON_30S_ENABLED=true` and `SCHWAB_1M_V2_ENABLED=true` untouched). Then
`systemctl restart project-mai-tai-strategy.service`.

**Step 2 — VERIFY the session is freed (zero overlap gate).** Before touching v2, confirm the
strategy-engine holds no streamer session:
- `strategy bot config` log line no longer lists `schwab_1m` or `schwab_30s`/macd_30s (only
  `polygon_30s`).
- **No** `Schwab streamer connected` and **no** streamer-credentials fetch in strategy.log
  after the restart (the client is None — it never starts). No `[SCHWAB-CHART-RECONNECT-CAUSE]`.
- Optional belt: a brief watch (~1–2 min) confirming silence on the strategy streamer path.
- **Only proceed to Step 3 once this is confirmed** — this is the no-overlap guarantee.

**Step 3 — bring UP v2's streamer (v2 takes the now-free session).**
```
MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=true
```
Then `systemctl restart project-mai-tai-schwab-1m-v2.service`.

**Step 4 — verify v2 holds the single session cleanly (below).**

## macd_30s decommission — dormant, not erased (what it concretely means)

- **Stop + disable + out of routing:** `MAI_TAI_STRATEGY_MACD_30S_ENABLED=false` (Step 1) →
  on restart it is **not built into `self.state.bots`** → no intents, no Schwab subscriptions,
  no decision processing. The systemd `strategy` service keeps running (it hosts the other
  bots); macd_30s simply isn't instantiated. There is **no separate macd_30s service** to stop.
- **Preserved:** all macd_30s code (`schwab_native_30s.py`, the bar-builder, entry logic),
  its `Settings` fields, its `strategies` DB row, and its `paper:macd_30s` broker account stay
  in place — **dormant**. Re-enabling later = flip the flag back + restart; no reconstruction.
- **No orphaned references:** disabling the flag gates instantiation, so nothing in the running
  engine references it. The DB/registry rows persisting is intentional (the dormancy).
- **Note:** macd_30s default in `settings.py` is `True`, so the env override to `false` is
  **load-bearing** — do not merely remove the env line (that falls back to default-enabled).

## schwab_1m retirement — same shape

- `MAI_TAI_STRATEGY_SCHWAB_1M_ENABLED=false` (Step 1) → out of routing. v2 is the functional
  replacement (CHART_EQUITY 1m, now via streamer). Code (`schwab_native_30s.py` 1m path), DB
  row, `paper:schwab_1m` account preserved dormant. (schwab_1m default is `False`, so here the
  env line could also be removed — but set it explicitly `false` for an unambiguous record.)
- CYN reminder: `paper:schwab_1m` holds the operator-frozen CYN position (PR #116 protected).
  Retiring the *bot* does not touch the position or the protected-symbols block; CYN stays
  protected and untouched.

## Day-1 activation safety (first live v2 streamer activation)

- **Window:** measured-quiet + still some bar flow to verify. Two options, operator's pick:
  - **(A) After RTH close, extended hours** (~20:00–23:00 UTC / 16:00–19:00 ET) — minimal
    live exposure during the transition; extended-hours CHART_EQUITY bars still flow enough to
    confirm streamer delivery. **Recommended.**
  - **(B) Midday RTH lull** (~16:00–17:00 UTC / 12:00–13:00 ET) — full bar flow for the
    clearest streamer-vs-REST latency comparison, but schwab_1m/macd_30s are briefly down
    mid-session during Steps 1–3. Acceptable only because account is flat + they're being
    retired anyway.
- **Attended**, account-flat at the moment, CYN protected. Paper-v2 means a v2 streamer hiccup
  is paper-only; the real transition risk is the brief schwab_1m/macd_30s downtime in Steps 1–3,
  minimized by an account-flat quiet window.

## Post-cutover verification (the success definition)

1. **Exactly one streamer session on the credential = v2's.** v2 heartbeat
   `streamer_enabled=true`, `streamer_connected=true`, **stable** (no reconnect storm — watch
   for `[V2-WS-LOGIN-OK]` then `[V2-WS-SUB]` within ~1s, NOT a `[V2-WS-DISCONNECT]` loop).
   Strategy-engine streamer remains absent (Step-2 state holds). No session war (a war would
   show as alternating disconnects on both sides).
2. **v2 bars flowing via the STREAMER, not just REST.** Persist-lag drops from the ~85s REST
   floor toward **<5s** on streamer-fed bars (`strategy_bar_history` lag query for
   `schwab_1m_v2`); v2 heartbeat `data_flow=flowing`; bar-handle activity attributable to the
   streamer path (REST stays as warmup + gap-fill only, C3-gated).
3. **v2 loop_health=healthy** (Workstream A) throughout — the new streamer task is also under
   the per-task backstop + liveness supervision; no `[V2-TASK-DIED]`, no `[V2-LOOP-...]` storm.
4. **schwab_1m + macd_30s stopped + out of routing** — absent from `strategy bot config` and
   from the strategy-state snapshot; no new intents/orders from either.
5. **polygon_30s untouched** — still enabled, still on the Polygon feed, parked as before.
6. **Account flat / CYN intact** — `virtual_positions` flat; CYN still protected.

## Back-out path (if v2's streamer does NOT hold the session cleanly)

Trigger: v2 streamer reconnect storm / never holds SUBS / bars don't move to streamer latency /
any instability in the verification window. Restore **today's known-good state**:
1. `MAI_TAI_STRATEGY_SCHWAB_1M_V2_STREAMER_ENABLED=false` → restart v2 (v2 back to REST-only,
   releases the session).
2. Re-enable the prior production streamer: `MAI_TAI_STRATEGY_SCHWAB_1M_ENABLED=true` +
   `MAI_TAI_STRATEGY_MACD_30S_ENABLED=true` → restart strategy (strategy-engine rebuilds its
   streamer, re-takes the one session, schwab_1m + macd_30s resume).
3. Verify back to baseline: strategy streamer connected, both bots in routing, v2 REST-only.
Because the sequence is flag-only (no code/DB changes), back-out is a clean inverse. Do the
back-out in the same stand-down-before-bring-up order if both sides are involved (v2 streamer
OFF first, then strategy streamer ON) to preserve the no-overlap rule.

## Open questions / risks for the reviewer

1. **Window choice** — (A) after-close extended hours (recommended) vs (B) midday lull. Pick.
2. **Verification dwell** — how long to watch v2 holding the session before declaring success
   (suggest ≥15–30 min of stable `streamer_connected=true` + sub-5s lag across multiple bars).
3. **macd_30s dormancy record** — do we want a one-line marker in the handoff/`strategies` row
   noting it's intentionally dormant (so a future reader doesn't "fix" the disabled flag)?
4. **Token SPOF unchanged** — v2's streamer now rides the shared token as the sole streamer
   consumer; a dead token still darkens v2 (Workstream A keeps the loop alive; Workstream B
   would surface it). Acknowledge, not solved here.

## Out of scope
- **Real-money conversion of v2** — separate later step; this is paper-v2 streamer activation.
- Extending v2's streamer to carry LEVELONE/TIMESALE (explicitly rejected — breaks isolation).
- Workstream B (dashboard/dead-token visibility) — separate.
- Any change to polygon_30s, CYN, PR #227/#238.
- Deleting macd_30s/schwab_1m code or DB rows (preserved dormant by design).

---

**End of plan. Awaiting operator review before any execution.**
