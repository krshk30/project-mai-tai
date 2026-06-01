# Design: CHART_EQUITY case-2 grace window (fix v3 for PR #228 cold-start residual)

**Status:** **APPROVED 2026-06-01 with three required changes (now incorporated below)**. Proceeding to implementation.

**Resolved open questions:**
- **OQ1 (test case 4 semantics — grace + stale path-3 message):** **suppress.** A fresh SUBS-confirm semantically invalidates the prior stream's freshness clock — the new subscription's bars haven't had their chance to arrive yet. Production code carries an explicit comment at the grace check stating this.
- **OQ2 (default N):** **92s.** Alignment with PR #228's `_chart_exchange_deadline_seconds()` is a feature (single tuning knob via `schwab_stream_symbol_stale_after_seconds`).
- **OQ3 (settings placement):** **env-tunable** `Settings.schwab_chart_subscription_grace_seconds`, default 0 means "use computed default." Matches the existing codebase pattern (e.g., PR #238's `schwab_cluster_reconnect_chronic_lag_threshold_secs`).

**Origin:** the 2026-06-01 verdict against the post-revert (`241`) baseline diagnosed a third reconnect-cause class distinct from what PR #228 and the reverted PR #237 targeted. See `docs/session-handoff-global.md` (2026-06-01 entry) for the verdict findings, and the related memory `[[project-mai-tai-schwab-bar-build-canonical-pipeline]]` for the broader pipeline context.

This doc proposes a narrow, design-first fix for that third class. **It is a doc, not code.** Code lands only after operator approval of the design.

## 1. Proven mechanism (the bug being fixed)

`SchwabStreamerClient._should_force_reconnect_for_chart_inactivity` (`schwab_streamer.py:769-779`):

```python
def _should_force_reconnect_for_chart_inactivity(self, now: float) -> bool:
    chart_state = self._service_states[self.CHART_EQUITY_SERVICE]
    if not chart_state.confirmed_symbols:
        return False                                                  # (path 0: not subscribed)
    if self._chart_exchange_deadline_exceeded(now):
        return True                                                   # (path 1: PR #228 / reverted #237 target)
    if chart_state.last_message_monotonic is not None and (
        now - chart_state.last_message_monotonic
    ) <= self._service_stale_after_seconds(self.CHART_EQUITY_SERVICE):
        return False                                                  # (path 3: CHART has recent message)
    return self._other_services_showing_life(self.CHART_EQUITY_SERVICE, now)
                                                                      # (path 2: CHART silent + other services alive)
```

Path 2 fires when CHART_EQUITY has confirmed_symbols but `last_message_monotonic is None` (no CHART message ever received this connection session), AND another service (TIMESALE/LEVELONE) has received recent messages. The function returns True → caller (`_handle_service_liveness_timeout`) raises `RuntimeError` → reconnect.

**Measured production impact (2026-06-01, post-revert):**
- 369/hr `[SCHWAB-CHART-RECONNECT-CAUSE]` events at 08 UTC, 417/hr at 09 UTC, 396/hr at 10 UTC.
- 100% have `exchange_deadline_exceeded=False chart_msg_age=none` (the path 2 signature exactly).
- Cascade cadence: reconnect every ~8s during the storm.
- **Bar-flow held through the cascade** (schwab_1m at 0.62–0.98 bars/sym/min, persist-lag avg 7–9s; macd_30s/polygon_30s healthy). The cascade is loud-in-logs but operationally invisible.

**Why the cascade self-sustains:**

After every reconnect:
1. `_reset_service_states` clears all per-service state (`confirmed_symbols`, `last_response_monotonic`, `last_message_monotonic`).
2. New websocket connection establishes.
3. SUBS dispatched via `_apply_subscription_delta(force_resubscribe=True)`.
4. TIMESALE/LEVELONE messages start arriving tick-by-tick (sub-second cadence) → those services' `last_message_monotonic` becomes "fresh".
5. CHART_EQUITY waits for the next minute boundary to emit its first 1-min bar — typically 0–60s wait depending on where in the minute the connect landed.
6. Within ~1s of connect, `_handle_service_liveness_timeout` fires (driven by the recv-loop `asyncio.wait_for(websocket.recv(), timeout=1.0)` at line 382). It calls `_should_force_reconnect_for_chart_inactivity(now)`.
7. CHART has confirmed_symbols (just populated by SUBS-confirm) AND `last_message_monotonic is None` (no CHART data yet) AND `_other_services_showing_life()` is True (TIMESALE already has messages). **Path 2 returns True. Reconnect fires.**

The cycle reseats and repeats every ~8s. CHART never has a chance to deliver its first bar of a session because the liveness check trips before the next minute boundary.

**Why bar-flow doesn't actually break:** the strategy-engine has a separate REST history-replay path (`_immediate_schwab_1m_history_refresh`, `_refresh_stale_schwab_1m_history` in `services/strategy_engine_app.py`) that pulls completed bars via Schwab REST when the live stream lags. That path covers the gap, and persisted bars eventually arrive — just from REST instead of from the streamer push.

## 2. Why prior fix v2 directions are obsolete

Yesterday's handoff proposed three design directions: (a) per-symbol close tracking, (b) MAX-of-all-symbols with no reset, (c) N-second deadline-disable after subscription change. **All three addressed path 1 (the exchange-deadline check) — i.e., the PR #228 / reverted #237 sub-problem.** None of them touches path 2.

Today's proof shows the exchange-deadline path is silent (0 `exchange_deadline_exceeded=True` events all day) post-revert. The cascade is path 2. **The three directions are discarded.**

## 3. Proposed fix — grace window on path 2

Insert a grace-window check immediately before path 2 in `_should_force_reconnect_for_chart_inactivity`:

```python
def _should_force_reconnect_for_chart_inactivity(self, now: float) -> bool:
    chart_state = self._service_states[self.CHART_EQUITY_SERVICE]
    if not chart_state.confirmed_symbols:
        return False
    if self._chart_exchange_deadline_exceeded(now):
        return True
    if chart_state.last_message_monotonic is not None and (
        now - chart_state.last_message_monotonic
    ) <= self._service_stale_after_seconds(self.CHART_EQUITY_SERVICE):
        return False
    # NEW: grace window after CHART subscription confirmation.
    # CHART_EQUITY emits a 1-min completed bar at each minute boundary; a fresh
    # SUBS-confirm followed by "no messages yet" is the expected state during
    # the gap to the next minute boundary, not a fault. Path-2 would otherwise
    # treat it as a fault when TIMESALE/LEVELONE are alive in parallel
    # (PROVEN 2026-06-01: 369/hr false-positive cascade at 08 UTC,
    # chart_msg_age=none on every event). Grace anchor is last_response_monotonic
    # (set on any Schwab response for CHART_EQUITY, including SUBS-confirm).
    if chart_state.last_response_monotonic is not None and (
        now - chart_state.last_response_monotonic
    ) <= self._chart_subscription_grace_seconds():
        return False
    return self._other_services_showing_life(self.CHART_EQUITY_SERVICE, now)
```

Plus a new helper:

```python
def _chart_subscription_grace_seconds(self) -> float:
    # CHART_EQUITY interval (60s) + delivery slack matching the exchange-deadline
    # tolerance (max(30, base*4) = 32s default), totaling one bar interval + slack.
    # Tunable via settings.schwab_chart_subscription_grace_seconds for operator
    # adjustment without code change.
    configured = float(
        getattr(self.settings, "schwab_chart_subscription_grace_seconds", 0.0) or 0.0
    )
    if configured > 0:
        return configured
    base = max(5.0, float(self.settings.schwab_stream_symbol_stale_after_seconds))
    return self.CHART_BAR_INTERVAL_SECONDS + max(30.0, base * 4.0)
```

Default N = 92s (60s interval + 32s slack), identical to `_chart_exchange_deadline_seconds()` by construction — same boundary as PR #228's interval-aware deadline. Operator can override via `MAI_TAI_SCHWAB_CHART_SUBSCRIPTION_GRACE_SECONDS` env if production data suggests retuning.

**That is the entire code change.** ~10 lines plus a settings field.

## 4. Why `last_response_monotonic` is the right anchor

`state.last_response_monotonic` is set in two places:
- **`_handle_message:642`** — `state.last_response_monotonic = now` whenever Schwab sends a response message for that service (matched by `service` field in the payload's `response` array). Fires for SUBS/UNSUBS/ADD confirmations AND for error responses scoped to that service.
- **`_mark_subscription_request_confirmed:873`** — redundant re-set inside the confirmation handler (already set by line 642 just before).

`_reset_service_states:950` clears it to `None` (called on every reconnect cycle in `finally:` block and again on fresh connect at line 377).

**Invariants:**
- After reset → `last_response_monotonic is None` AND `confirmed_symbols is empty`.
- After SUBS-confirm → both `last_response_monotonic` is set AND `confirmed_symbols` non-empty (set atomically in `_mark_subscription_request_confirmed`).
- Path 2 check is gated on `chart_state.confirmed_symbols` being non-empty (line 771). **At the moment path 2 is reachable, `last_response_monotonic` is necessarily set.** No `None` window exists between the two becoming consistent.

So the grace anchor cleanly survives:
- **Reconnect:** state cleared → no path 2 reached until SUBS-confirm repopulates confirmed_symbols, at which point grace anchor is fresh (`now`). Grace window = ~92s of suppression after every reconnect, exactly bounding the cold-start gap.
- **Mid-session ADD of a new symbol:** `_mark_subscription_request_confirmed` updates `confirmed_symbols.update(...)` AND bumps `last_response_monotonic` to the ADD-confirm time. Grace restarts. Gives CHART time to deliver the new symbol's first bar. Desired.
- **UNSUBS:** updates `last_response_monotonic` too. If remaining `confirmed_symbols` is non-empty, grace restarts on the remaining cluster (mild but benign — UNSUBS doesn't normally need grace since other symbols' message stream is unaffected). If `confirmed_symbols` becomes empty, path 0 short-circuits → no firing.
- **Steady-state subscription (no churn):** `last_response_monotonic` doesn't update; grace expires after N seconds; normal path-2 detection resumes.

## 5. Stress test against the failure modes the operator asked about

### 5.1 Interaction with `_reset_service_states`

`_reset_service_states` clears both `last_response_monotonic` and `confirmed_symbols` (lines 949–951). The reset is called on every reconnect (line 421 `finally:` and line 377 fresh-connect). After reset:
- Path 0 short-circuits (no confirmed_symbols) → no firing until SUBS-confirm.
- SUBS-confirm sets `confirmed_symbols` AND `last_response_monotonic` atomically.
- First path-2 evaluation after that has fresh grace anchor → grace applies.

**No interaction issue.** Reset is the desired starting point for the grace window.

### 5.2 Interaction with PR #228's interval-aware deadline (path 1)

PR #228's `_chart_exchange_deadline_exceeded` is checked at path 1, **before** the new grace window. If the exchange deadline trips, the function returns True regardless of grace status. Path 1 detects a genuine "TIMESALE exchange-clock has advanced 92s past CHART's last completed bar close" condition — which is a real CHART staleness issue independent of the case-2 cold-start.

**The two paths are orthogonal.** Path 1 catches "CHART completed bars are falling behind"; path 2 (with grace) catches "CHART service is dead but we just subscribed." Neither subsumes the other.

Note: if path 1 fires AND path 2 grace is active, path 1 wins (we reconnect on the exchange-deadline signal). That's correct: an active exchange-deadline trip means CHART's bar timestamps are falling behind, and the grace shouldn't mask that.

### 5.3 Does grace anchor survive a force-resubscribe?

`_apply_subscription_delta(force_resubscribe=True)` is called immediately after every reconnect at line 378. It dispatches new SUBS requests. Schwab responds → line 642 sets `last_response_monotonic`. Grace anchor is fresh.

A force-resubscribe mid-session (e.g., via `set_desired_symbols` changing the desired set) would similarly produce SUBS/ADD/UNSUBS responses, each updating `last_response_monotonic`. Grace restarts on each. Desired.

**One edge case:** a force-resubscribe that produces no SUBS request (e.g., desired set hasn't changed and isn't expired). In that case, no Schwab response arrives, no anchor bump. Grace expires on its natural schedule. That's correct — no subscription change means no need for grace.

### 5.4 What about a genuinely dead CHART feed?

The grace window suppresses path 2 for N=92s after the most recent SUBS-confirm. After that:
- If CHART has delivered at least one message during the grace window → `last_message_monotonic` is set → path 3 takes over. As long as messages keep arriving, no reconnect. If messages go stale > 90s (the existing `_service_stale_after_seconds` for CHART), path 3 falls through to path 2 → reconnect fires.
- If CHART has delivered no messages in grace window AND grace window expires → `last_message_monotonic is None` still → path 2 fires → reconnect.

**Detection delay for a truly dead CHART feed at cold-start:** previously ~8s (immediate cascade). Post-fix: ~92s (one full grace window). This is the price of the fix. On a dead feed, 92s vs 8s is the difference between fast-reconnect-that-doesn't-help vs slow-reconnect-that-doesn't-help — neither recovers a Schwab-side outage. Cost is acceptable.

**Detection of CHART feed going dark mid-session:** unchanged. Once any CHART message has arrived, path 3 governs. Stale-after-90s detection continues to work as today.

### 5.5 Worst-case ADD churn — **this IS the 4 AM cold-start window**

The previous version of this section dismissed perpetual-grace-via-ADD-churn as needing "Schwab CHART broken AND scanner constantly adding symbols." Operator review correctly flagged that **the second condition is the normal 4:00 ET cold-start state** — the scanner is building today's watchlist symbol-by-symbol over the warmup window. ADD churn is not an edge case; it is the manifestation window for the bug being fixed.

The correct reasoning:

**During cold-start ADD churn, path 1 (`_chart_exchange_deadline_exceeded`) is the load-bearing dead-feed guard.** Path 1 fires when the TIMESALE/LEVELONE exchange clock (`latest_other_exchange_ts`) advances 92s past `chart_state.last_completed_bar_close_timestamp`. If CHART is genuinely dead while TIMESALE is alive (the only scenario where ADD churn could mask anything), the exchange clock keeps advancing on TIMESALE messages and path 1's deadline trips — **regardless of grace status, because path 1 is checked before the grace window in `_should_force_reconnect_for_chart_inactivity`**.

The grace window is intentionally permissive during ADD churn because **churn signals legitimate subscription-state activity** — the scanner is actively telling the streamer what to subscribe to, and each subscription change needs its own ~92s for CHART to deliver the first bar. Aggressively forcing reconnects during churn is the cascade we're trying to fix.

**Combined coverage:**

| Condition during ADD churn | Detector |
|---|---|
| CHART delivering bars, lag normal | nothing fires (correct) |
| CHART delivering bars, completed-bar timestamps lagging the live clock | **path 1** fires regardless of grace |
| CHART silent, TIMESALE alive (cascade scenario) | path 2 with grace = grace suppresses (correct: bar gap window after each SUBS) |
| CHART silent, TIMESALE alive, lasting beyond one grace window with no new SUBS | path 2 fires (grace expires) |
| CHART genuinely dead-and-TIMESALE-clock-running mid-churn | **path 1** fires on the exchange-deadline (`latest_other_exchange_ts > chart_close_ts + 92s`) |

The only scenario the grace masks is "CHART silent + TIMESALE silent + constant ADD churn" — but if TIMESALE is silent, `_other_services_showing_life` returns False at line 779 and path 2 doesn't fire either way. So the grace masks nothing that the existing detectors didn't already let pass.

**This reasoning will also appear as a code comment at the grace check** so a future reader can re-derive it without coming back to the design doc.

### 5.6 What about the `socket closed cleanly` flap (the separate 57–66/hr pre-existing pattern)?

That flap is a websocket-level close from Schwab's side, not a path-1/2/3 reconnect. It's caught in the `except websockets.exceptions.ConnectionClosedOK` branch at line 389. **Unaffected by this design.** Continues as today.

## 6. Test cases

Unit tests in `tests/unit/test_schwab_streamer_chart_grace.py` (new file, narrow scope):

1. **`test_grace_window_suppresses_path_2_immediately_after_subs_confirm`** — set up CHART_EQUITY with confirmed_symbols + last_response_monotonic = now − 30s + last_message_monotonic = None + other service alive. `_should_force_reconnect_for_chart_inactivity(now)` returns False (grace active).
2. **`test_grace_window_expires_and_allows_path_2_for_genuinely_silent_chart`** — same setup but last_response_monotonic = now − 120s (past 92s grace). Returns True (grace expired, CHART truly silent).
3. **`test_grace_window_does_not_block_path_1_exchange_deadline`** — grace active AND exchange-deadline tripped (last_completed_bar_close_timestamp old enough that TIMESALE outpaces it by 92s). Returns True (path 1 wins).
4. **`test_grace_window_does_not_block_path_3_msg_stale`** — grace active BUT CHART has a stale-by-100s message (last_message_monotonic = now − 100s, past the 90s `_service_stale_after_seconds` for CHART). Path 3 short-circuits (line 775-778 doesn't return False because not within stale threshold), falls through to grace check. Wait — actually need to re-verify this. Path 3 short-circuits to False only if `last_message_monotonic <= stale_after_seconds`. If `> stale_after_seconds`, it doesn't return False; it falls through. So in this scenario, path 3 falls through to grace; if grace says suppress, we suppress. **Question for the design reviewer: is that correct?** If CHART has had a message but it's now stale beyond 90s, AND the grace window is also active (recent SUBS-confirm), should we suppress? I argue yes — if we just resubscribed (e.g., ADD), the prior message clock might be irrelevant; let the new subscription's bars arrive. But this is debatable.
5. **`test_grace_window_restarts_on_add_subs_confirm`** — initial SUBS-confirm at t=0, second ADD-confirm at t=60. At t=90, grace should still be active (anchor is t=60). At t=160, grace expired.
6. **`test_grace_window_clears_after_reset_service_states`** — populate state, call `_reset_service_states`, assert last_response_monotonic is None. After reset, path 0 short-circuits (no confirmed_symbols) so the question doesn't arise — but the grace anchor IS properly cleared (matches reconnect semantics).
7. **`test_no_grace_means_legacy_path_2_behavior`** — set `settings.schwab_chart_subscription_grace_seconds = 0.01` (effectively disabled). Re-run test 1's setup: should now return True (path 2 fires immediately). Regression hatch for operator to disable the grace if it ever turns out to be wrong.
8. **`test_grace_window_breaks_cold_start_cascade`** — **THE regression guard for this specific fix.** All other tests prove the function returns the right value in isolation; this one proves the actual cycle is broken. Simulate the full cascade sequence:
    1. Start with state populated as if mid-session.
    2. Call `_reset_service_states()` — confirmed_symbols empty, anchors all None.
    3. Call `_should_force_reconnect_for_chart_inactivity(now=t)` — returns False via path 0 (no confirmed_symbols).
    4. Simulate SUBS-confirm at t: `_mark_subscription_request_confirmed` populates `confirmed_symbols = {syms}` AND sets `last_response_monotonic = t`.
    5. Simulate TIMESALE service alive (`last_message_monotonic = t-1`, confirmed non-empty).
    6. At t+1s, t+10s, t+30s, t+60s — call `_should_force_reconnect_for_chart_inactivity` four times: **all must return False** (grace active; without the fix, all four would return True under the path-2 cascade).
    7. At t+45s, simulate first CHART bar arrival: set `chart_state.last_message_monotonic = t+45`.
    8. At t+95s (past grace expiry): call again. Path 3 governs because last_message_monotonic = t+45 is within stale_threshold of t+95 (50s < 90s). Returns False (CHART has recent message).
    9. At t+200s, no new messages: `now - last_message_monotonic = 155 > 90` → path 3 falls through → grace check uses anchor t (200s > 92s grace) → falls through → path 2 fires. Returns True (correct: CHART has gone genuinely silent).
    
    Proves: cascade broken (steps 6 all-False); first-bar handoff to path 3 (step 8); long-term staleness still detected (step 9).

Integration / regression tests:
- **`test_existing_PR_228_regression_guards_pass`** — the PR #228 tests that assert exchange-deadline behavior should be unaffected. Run them.
- **`test_existing_dead_feed_msg_stale_guard_passes`** — any existing test for the 90s msg_stale path should still pass.

Note on test naming: tests live in their own file rather than appending to `test_schwab_streamer_timesale.py` because that file was deleted by PR #241 along with the reverted #237. Starting clean.

## 7. Production verification plan

After code + tests land in PR + admin-merge + sync + restart:

**Stage-one (~10 min post-restart, attended):** standard checks. NRestarts=0, no tracebacks, both PR #238 and the new grace-window code present in source. Bar-flow continuing.

**Stage-two falsifiable prediction (next 04:00 ET / 08 UTC window):**
- `[SCHWAB-CHART-RECONNECT-CAUSE]` events at 08 UTC: **drop from ~370/hr (today's measurement) to under ~20/hr**. Residual 20/hr is a generous ceiling for genuine dead-feed catches (path 2 firing after grace expires on a truly silent CHART) + the existing 90s msg_stale branch.
- `connection loop failed` rate at 08 UTC: similarly drops to <~20/hr.
- `socket closed cleanly` rate (the pre-existing `sent 1000` flap): **unchanged ~65/hr** — different code path, design doesn't touch it. If it changes, that's a signal to investigate, not a fix-v3 result.
- `exchange_deadline_exceeded=True`: **stays 0/hr** (the revert holds; nothing in this design changes path 1).
- Bar-flow during 08-10 UTC: **continues to hold cleanly** — schwab_1m bars/sym/min ≥0.6, persist-lag avg ≤15s. If bar-flow degrades, the grace window is masking real CHART deadness and N is too long.
- PR #238 cluster-reconnect rate at 11 UTC (07 ET pre-mkt open): **stays at ~0-1/hr** (independent code path, unaffected).

**If prediction holds:** fix v3 confirmed, the original PR #228 cold-start residual is closed (case-2 path was the true mechanism, now mitigated). Production-streamer arc finally fully closes. PR #227 DEBUG log can be removed in a follow-up PR. v2 Day-1 collision-check noise floor improves to ~0/hr in the cleanest measurement windows.

**If prediction misses:**
- If `exchange_deadline_exceeded=True` shows up → revert-of-revert scenario; the design has somehow re-enabled the false-positive class. Diagnose.
- If `[SCHWAB-CHART-RECONNECT-CAUSE]` stays >20/hr at 08 UTC → grace window N is too short OR there's a fourth path. Pull the chart_msg_age values to see whether grace expired or never engaged.
- If bar-flow degrades → grace masking real deadness; tune N down OR add a hard upper bound (e.g., always fire if `last_response_monotonic > N_max` even if grace says suppress).

## 8. Things this design does NOT do

- **Does not fix the `socket closed cleanly` 65/hr flap.** Separate workstream (the 2026-05-27 17:00 UTC verdict's "non-chart liveness/recycle trigger" — possibly the 90s msg_stale on a non-CHART service, or Schwab session-limit behavior). Out of scope.
- **Does not fix the polygon_30s persist-lag growth observed 2026-06-01.** Different data path (Polygon WebSocket, not Schwab). Separate workstream.
- **Does not change the strategy-engine REST history-replay path.** That path is what kept bar-flow healthy during today's cascade; we leave it alone.
- **Does not introduce per-symbol state, MAX-of-all timestamps, or any of yesterday's three discarded directions.** This is a single function-level guard at one call site.

## 9. Resolved review decisions

1. **OQ1 — grace + stale path-3 message:** **suppress.** A SUBS-confirm semantically invalidates the prior stream's freshness clock. The implementation comment at the grace-window check carries this reasoning verbatim so a future reader can re-derive it from the code alone.
2. **OQ2 — default N:** **92s** = `CHART_BAR_INTERVAL_SECONDS + max(30, base*4)`, identical to `_chart_exchange_deadline_seconds()`. Single tuning knob shared with PR #228's exchange-deadline.
3. **OQ3 — settings placement:** env-tunable `Settings.schwab_chart_subscription_grace_seconds`, default 0 means "use computed default." Matches PR #238's `schwab_cluster_reconnect_chronic_lag_threshold_secs` pattern.

## 10. Out of scope explicitly

- Any change to `_chart_exchange_deadline_exceeded` (path 1) — that path is silent post-revert, leave it alone.
- Any change to `_record_service_messages_from_payload` — the message-receiving and max-accumulation paths are not implicated in the case-2 mechanism.
- Any change to `_mark_subscription_request_confirmed` — the SUBS-confirm handler is correct; we add a grace check elsewhere, not modify the handler.
- Any change to v2 (`schwab_v2_streamer.py`) — case-2 is in the production streamer only; v2 has its own connection lifecycle.

---

**End of design proposal.** Awaiting operator review before any code is written.
