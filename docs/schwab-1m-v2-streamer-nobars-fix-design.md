# schwab_1m_v2 streamer no-bars — two-step fix design

Status: **DESIGN — review before code.** Approved sequencing: Step 1 isolated (verify
via diag) → only then Step 2. v2 stays cut over (live repro), PAPER, back-out staged.
Diag (`strategy_schwab_1m_v2_streamer_diag_enabled`) stays ON through both steps; revert
after Step 2 is confirmed.

## Evidence that pins the fork (PR #256 diag, live pre-market 2026-06-09)
~10 min, streamer connected + SUBS'd (code-0 ack) to 25 symbols:

```
[V2-WS-DIAG-TALLY] frame_composition={'notify': 62, 'response': 1}  chart_sample_logged=False
[V2-WS-DIAG] data-frame lines: 0      [V2-WS-DIAG-CHART-RAW]: (none)
streamer_connected=true  rest_bars_gated_total=0  secs_since_last_bar rising
```

Only `notify` (heartbeats) + the one `response` (SUBS ack). **Zero `data` frames.**
→ NOT a parse/field mismatch (no records to parse). It's a **data-channel / subscription
problem**: Schwab acks the SUBS but pushes no CHART_EQUITY data to v2's session.

---

## STEP 1 — Finding 1: SUBS-ordering fix (the blocker, the uncertain one) — ISOLATED

**Hypothesis.** The streamer code is equivalent to the *working* production streamer
except **one structural divergence: SUBS ordering.**
- Production (`schwab_streamer.py` ~266-268): login → **send SUBS immediately** → receive loop.
- v2 (`schwab_v2_streamer.py` 166-178): login → `_sync_event.set()` → **receive loop →
  `ws.recv()` (consumes a notify) → THEN SUBS** (~1s later, from inside the loop).

v2 does a `recv()` **before** its first SUBS; production subscribes before any recv.
"Subscribe before consuming inbound frames" is a plausible Schwab requirement for the data
channel to activate. It's the only code difference and the working reference does it the
other way — so it's the one concrete lever. **This is a hypothesis, tested live, not assumed.**

**Change (v2 `run()`, lines 166-178).** Send the initial SUBS synchronously right after the
login ack, before entering the receive loop — mirror production exactly:

```python
await self._login(ws, self._creds)
self._connected = True
self._connect_failures = 0
logger.info("[V2-WS-LOGIN-OK] ...")
# Fresh SUBS on every (re)connect — no server-side subscription memory.
self._requested_symbols = set()
# Finding-1 fix: subscribe IMMEDIATELY after login, before the first recv()
# — mirror the production streamer, which subscribes before consuming any
# inbound frame. (Was: _sync_event.set() + SUBS deferred into the receive loop.)
await self._apply_subscription_delta(ws)
await self._receive_loop(ws)
```

- Drops the `self._sync_event.set()` at line 177 (the deferred-SUBS trigger). The
  receive-loop's `_sync_event` path **stays** for subsequent watchlist changes —
  `set_desired_symbols()` sets `_sync_event` itself (verified), so live re-subscribes
  still work. After the direct apply, `_requested_symbols == _desired_symbols`, so a stray
  `_sync_event` would compute an empty delta (no-op) — safe.
- Scope: this one block. No change to `_apply_subscription_delta`, login, parse, routing.

**Verify live (diag ON, isolated — no other behavioral change):**
- ✅ confirmed if `[V2-WS-DIAG] data frame services=['CHART_EQUITY']` appears +
  `[V2-WS-DIAG-CHART-RAW]` content logs + `rest_bars_gated_total` starts climbing.
  → hypothesis confirmed cleanly (no confound). Proceed to Step 2.
- ❌ if still only `notify` → ordering wasn't it. **Do NOT guess further.** Escalate to the
  Schwab-side question: does something about this session/credential/account suppress
  CHART_EQUITY data push despite a code-0 ack? (Possible Schwab API support question.)
  The diag gives this confirm/deny immediately — no blind iteration.

---

## STEP 2 — Finding 2: streamer-warms-its-own-symbols decouple — ONLY after Step 1 ✅

**Problem.** `_handle_bar_from_streamer` buffers every streamer bar until
`symbol ∈ _rest_warmup_done`, and a symbol only warms when **REST** delivers a fresh bar.
REST pricehistory is dry pre-market (gotcha #5) → `warmed_size=0` → streamer bars are
stranded in exactly the window the streamer exists to fill. (Independent of Step 1: a
correctly-ungated path is useless until data actually arrives — hence Step 1 first.)

**Change (bot `_handle_bar_from_streamer`, ~849).** A streamer-delivered bar for a
not-yet-warmed symbol → forward + mark streamer-warmed, no REST-warmup gate:

```python
async def _handle_bar_from_streamer(self, symbol, bar):
    if symbol not in self._rest_warmup_done:
        # Finding-2 decouple: the streamer warms its own symbols. REST can't
        # warm pre-market (pricehistory dry), so buffering-until-REST strands
        # streamer bars. Forward directly + mark warmed; the strategy's
        # min_bars=135 + freshness guards prevent premature/out-of-order signals.
        self._rest_warmup_done.add(symbol)
        logger.info("[V2-STREAMER-WARMED] schwab_v2 streamer warmup for %s (warmed=%d/%d)",
                    symbol, len(self._rest_warmup_done), len(self._watchlist))
    await self._handle_bar(symbol, bar)
```

Principle preserved: **streamer authoritative, REST fallback.** REST stays
warmup-before-streamer-up (instant 135-bar warm when it works) + reconnect gap-fill only.

**Edge case to confirm before coding Step 2** (the reason the W2 buffer existed): if the
streamer warms a symbol pre-market, then REST later (RTH) delivers its historical warmup
batch, those older bars arrive **out-of-order** relative to streamer bars already fed.
Need to confirm the strategy's `on_bar` rejects/updates-in-place on older-or-equal
timestamps gracefully — OR gate REST warmup to skip symbols already streamer-warmed. Will
audit `on_bar` timestamp handling + `_handle_bar_from_rest` ordering and fold the resolution
into the Step-2 design before writing code.

**Verify live:** `rest_bars_gated_total` climbing, `secs_since_last_bar` small,
persist-lag → <5s, bars reaching the strategy.

---

## Constraints (both steps)
PR #227 stays · PR #238 untouched · CYN untouched · polygon parked · production Schwab bots
disabled · v2 stays cut over (live repro) · PAPER · back-out staged, not a deadline · no
time pressure. Diag stays deployed until Step 2 confirmed, then reverted.
