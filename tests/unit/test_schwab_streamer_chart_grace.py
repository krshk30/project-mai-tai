"""Unit tests for the CHART_EQUITY case-2 grace window (fix v3, 2026-06-01).

Covers the behavior added in `_should_force_reconnect_for_chart_inactivity`:
a grace window after the most recent SUBS/ADD/UNSUBS confirmation suppresses
the case-2 short-circuit (CHART silent + other services alive) for ~one CHART
bar interval + slack, giving CHART time to deliver its first bar after a
subscription change.

Design doc: docs/schwab-chart-grace-window-design.md
"""

from __future__ import annotations

from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.settings import Settings


class _StubAuthAdapter:
    """Minimal stub so SchwabStreamerClient can be constructed without real Schwab credentials."""

    def __init__(self) -> None:
        self.access_token: str | None = None


def _make_client(grace_override: float = 0.0) -> SchwabStreamerClient:
    """Build a streamer client with a stub auth adapter and an optional grace override."""
    settings = Settings(
        schwab_stream_symbol_stale_after_seconds=8.0,
        schwab_chart_subscription_grace_seconds=grace_override,
    )
    return SchwabStreamerClient(settings, auth_adapter=_StubAuthAdapter())  # type: ignore[arg-type]


def _populate_subscribed_chart(client: SchwabStreamerClient, *, last_response_at: float, last_message_at: float | None = None) -> None:
    """Mark CHART_EQUITY as confirmed (post SUBS-confirm) with the given response/message timestamps."""
    chart = client._service_states[SchwabStreamerClient.CHART_EQUITY_SERVICE]
    chart.confirmed_symbols.add("AAPL")
    chart.last_response_monotonic = last_response_at
    chart.last_message_monotonic = last_message_at


def _populate_timesale_alive(client: SchwabStreamerClient, *, last_message_at: float) -> None:
    """Mark TIMESALE_EQUITY as alive (confirmed_symbols non-empty + recent message)."""
    ts = client._service_states[SchwabStreamerClient.TIMESALE_EQUITY_SERVICE]
    ts.confirmed_symbols.add("AAPL")
    ts.last_message_monotonic = last_message_at


# -----------------------------------------------------------------------------
# Test 1 — grace suppresses path 2 immediately after SUBS-confirm.
# -----------------------------------------------------------------------------
def test_grace_window_suppresses_path_2_immediately_after_subs_confirm() -> None:
    client = _make_client()
    t = 1000.0
    # SUBS-confirm at t-30s, no CHART messages yet, TIMESALE alive.
    _populate_subscribed_chart(client, last_response_at=t - 30.0, last_message_at=None)
    _populate_timesale_alive(client, last_message_at=t - 1.0)

    # Without grace, path 2 would fire (CHART silent + TIMESALE alive).
    # With grace (default 92s), it must NOT fire — 30s < 92s grace.
    assert client._should_force_reconnect_for_chart_inactivity(t) is False


# -----------------------------------------------------------------------------
# Test 2 — grace expires; path 2 fires for genuinely silent CHART.
# -----------------------------------------------------------------------------
def test_grace_window_expires_and_allows_path_2_for_genuinely_silent_chart() -> None:
    client = _make_client()
    t = 1000.0
    # SUBS-confirm at t-120s (past 92s grace), no CHART messages yet, TIMESALE alive.
    _populate_subscribed_chart(client, last_response_at=t - 120.0, last_message_at=None)
    _populate_timesale_alive(client, last_message_at=t - 1.0)

    # Grace has expired (120s > 92s); CHART is genuinely silent; path 2 must fire.
    assert client._should_force_reconnect_for_chart_inactivity(t) is True


# -----------------------------------------------------------------------------
# Test 3 — path 1 (exchange-deadline) wins over grace.
# -----------------------------------------------------------------------------
def test_grace_window_does_not_block_path_1_exchange_deadline() -> None:
    client = _make_client()
    t = 1000.0
    # Set up grace as active (SUBS-confirm 30s ago, well within 92s grace).
    _populate_subscribed_chart(client, last_response_at=t - 30.0, last_message_at=None)
    _populate_timesale_alive(client, last_message_at=t - 1.0)

    # Now force path 1 to fire: CHART has a stale completed-bar close,
    # TIMESALE's exchange clock outpaces it past the 92s deadline.
    chart = client._service_states[SchwabStreamerClient.CHART_EQUITY_SERVICE]
    chart.last_completed_bar_close_timestamp = 500.0
    ts = client._service_states[SchwabStreamerClient.TIMESALE_EQUITY_SERVICE]
    ts.last_exchange_timestamp = 500.0 + client._chart_exchange_deadline_seconds() + 10.0

    # Path 1 trips before grace is even evaluated; reconnect fires.
    assert client._should_force_reconnect_for_chart_inactivity(t) is True


# -----------------------------------------------------------------------------
# Test 4 — grace overrides a stale path-3 message clock (OQ1 resolution).
# Operator-resolved 2026-06-01: SUBS-confirm semantically invalidates the
# prior stream's freshness clock — new subscription's bars haven't had their
# chance to arrive. Suppress reconnect.
# -----------------------------------------------------------------------------
def test_grace_overrides_stale_path_3_message_after_fresh_subs_confirm() -> None:
    client = _make_client()
    t = 1000.0
    # CHART had a message but it's now stale (100s old > 90s threshold).
    # However, a fresh SUBS-confirm landed 30s ago (well within 92s grace).
    _populate_subscribed_chart(client, last_response_at=t - 30.0, last_message_at=t - 100.0)
    _populate_timesale_alive(client, last_message_at=t - 1.0)

    # Path 3: last_message_at=t-100s, stale > 90s → path 3 falls through.
    # Path 2 would fire, but grace suppresses it because of fresh SUBS-confirm.
    # Per OQ1 resolution: suppress (let new SUBS deliver fresh bars).
    assert client._should_force_reconnect_for_chart_inactivity(t) is False


# -----------------------------------------------------------------------------
# Test 5 — grace restarts on ADD/SUBS-confirm.
# -----------------------------------------------------------------------------
def test_grace_window_restarts_on_add_subs_confirm() -> None:
    client = _make_client()
    _populate_timesale_alive(client, last_message_at=0.0)

    # Initial SUBS-confirm at t=0.
    _populate_subscribed_chart(client, last_response_at=0.0, last_message_at=None)
    # At t=80s, original grace still active (80s < 92s).
    assert client._should_force_reconnect_for_chart_inactivity(80.0) is False

    # ADD-confirm at t=60s bumps the anchor.
    chart = client._service_states[SchwabStreamerClient.CHART_EQUITY_SERVICE]
    chart.last_response_monotonic = 60.0

    # At t=140s, anchor=60 → 80s elapsed since ADD → still within grace.
    _populate_timesale_alive(client, last_message_at=139.0)
    assert client._should_force_reconnect_for_chart_inactivity(140.0) is False

    # At t=160s, anchor=60 → 100s elapsed since ADD → grace expired.
    _populate_timesale_alive(client, last_message_at=159.0)
    assert client._should_force_reconnect_for_chart_inactivity(160.0) is True


# -----------------------------------------------------------------------------
# Test 6 — _reset_service_states clears the grace anchor.
# -----------------------------------------------------------------------------
def test_grace_window_clears_after_reset_service_states() -> None:
    client = _make_client()
    _populate_subscribed_chart(client, last_response_at=100.0, last_message_at=110.0)
    _populate_timesale_alive(client, last_message_at=120.0)

    chart_before = client._service_states[SchwabStreamerClient.CHART_EQUITY_SERVICE]
    assert chart_before.last_response_monotonic == 100.0
    assert chart_before.confirmed_symbols == {"AAPL"}

    client._reset_service_states()
    chart_after = client._service_states[SchwabStreamerClient.CHART_EQUITY_SERVICE]

    # Both anchor and confirmed_symbols cleared atomically (the invariant the
    # design relies on: post-reset, path 0 short-circuits until SUBS-confirm
    # repopulates both).
    assert chart_after.last_response_monotonic is None
    assert chart_after.last_message_monotonic is None
    assert chart_after.confirmed_symbols == set()
    # And the function returns False via path 0 in this state.
    assert client._should_force_reconnect_for_chart_inactivity(200.0) is False


# -----------------------------------------------------------------------------
# Test 7 — setting grace to effectively-zero restores legacy path-2 behavior.
# Regression hatch for operator to disable the fix in production via env.
# -----------------------------------------------------------------------------
def test_no_grace_means_legacy_path_2_behavior() -> None:
    client = _make_client(grace_override=0.01)
    t = 1000.0
    # Same setup as test 1: SUBS-confirm 30s ago, CHART silent, TIMESALE alive.
    _populate_subscribed_chart(client, last_response_at=t - 30.0, last_message_at=None)
    _populate_timesale_alive(client, last_message_at=t - 1.0)

    # Grace is 0.01s (effectively disabled). 30s elapsed >> 0.01s grace →
    # grace expired → path 2 fires (legacy behavior pre-fix-v3).
    assert client._should_force_reconnect_for_chart_inactivity(t) is True


# -----------------------------------------------------------------------------
# Test 8 — THE regression guard. Proves the cascade cycle is broken, not just
# that the function returns the right value in isolation. Simulates the full
# sequence: reset → SUBS-confirm → multiple grace-window checks (all False) →
# first CHART bar → path 3 governs → eventual silence → path 2 fires.
# -----------------------------------------------------------------------------
def test_grace_window_breaks_cold_start_cascade() -> None:
    client = _make_client()
    t = 1000.0

    # Step 1-2: pre-state populated (as if mid-session), then reset.
    _populate_subscribed_chart(client, last_response_at=500.0, last_message_at=510.0)
    _populate_timesale_alive(client, last_message_at=520.0)
    client._reset_service_states()

    # Step 3: post-reset, path 0 short-circuits (no confirmed_symbols).
    assert client._should_force_reconnect_for_chart_inactivity(t) is False

    # Step 4: SUBS-confirm at t. Populate CHART confirmed + anchor.
    chart = client._service_states[SchwabStreamerClient.CHART_EQUITY_SERVICE]
    chart.confirmed_symbols.add("AAPL")
    chart.last_response_monotonic = t
    chart.last_message_monotonic = None

    # Step 5: TIMESALE arrives immediately tick-by-tick.
    _populate_timesale_alive(client, last_message_at=t - 1.0)

    # Step 6: at t+1, t+10, t+30, t+60 — without grace, path 2 would fire each
    # time (the cascade). WITH grace, all four must return False.
    for offset in (1.0, 10.0, 30.0, 60.0):
        now = t + offset
        # Refresh TIMESALE liveness (it never goes silent during cold-start).
        _populate_timesale_alive(client, last_message_at=now - 1.0)
        assert (
            client._should_force_reconnect_for_chart_inactivity(now) is False
        ), f"path-2 cascade should be suppressed by grace at t+{offset}s"

    # Step 7: first CHART bar arrives at t+45s. Backdate by re-asserting the
    # invariant that step 6's iterations didn't introduce: last_message_monotonic
    # is set to t+45.
    chart.last_message_monotonic = t + 45.0

    # Step 8: at t+95s, grace has expired (95s > 92s default), but path 3 now
    # governs: last_message at t+45, now t+95, age 50s, within 90s stale_threshold.
    _populate_timesale_alive(client, last_message_at=t + 94.0)
    assert client._should_force_reconnect_for_chart_inactivity(t + 95.0) is False

    # Step 9: at t+200s with no new CHART messages: last_message at t+45, age
    # 155s > 90s → path 3 falls through. Grace anchor still at t, so 200s
    # elapsed > 92s → grace expired. Path 2 fires correctly: CHART has gone
    # genuinely silent post-grace.
    _populate_timesale_alive(client, last_message_at=t + 199.0)
    assert (
        client._should_force_reconnect_for_chart_inactivity(t + 200.0) is True
    ), "genuine post-grace CHART silence must still be detected"


# -----------------------------------------------------------------------------
# Test 9 (bonus, not in design doc) — verify _chart_subscription_grace_seconds
# computes the expected default and honors the override.
# -----------------------------------------------------------------------------
def test_chart_subscription_grace_seconds_default_and_override() -> None:
    # Default settings: base=8s → 60 + max(30, 32) = 92.
    client_default = _make_client()
    assert client_default._chart_subscription_grace_seconds() == 92.0

    # Override to 150s.
    client_override = _make_client(grace_override=150.0)
    assert client_override._chart_subscription_grace_seconds() == 150.0
