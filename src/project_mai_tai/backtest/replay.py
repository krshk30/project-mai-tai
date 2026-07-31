"""Backtest REPLAY engine — Phase 1 (ENTRY side) + Phase 2 (EXIT side) + Phase 3 (EXTENDED HOURS).

Replays a historical trading day through the **REAL live entry code**
(`strategy_core.schwab_1m_v2.SchwabV2Strategy`) rather than a re-implementation, then
runs each emitted draft through the **shared emit-gate**
(`strategy_core.entry_gate`) — the exact functions the live bot calls. This is the
durable fix for the chronic "backtest ≠ live" drift (docs/backtest-replay-engine-design.md):
the entry signal, the config, and the emit-gate are SHARED, so they cannot drift by
construction. The only re-implemented surface here is the honest ENTRY fill model and the
tape feed (both small, both bounded by the 07-23 parity reconciliation).

Scope (P1): ENTRY — the honest fill against the tape (resting band / marketable reactive).

Scope (P2): EXIT — continue past the entry fill into the full trade, unified on the LIVE
exit code. The geometry is chosen by the position's OPEN session (docs/schwab-1m-v2-live-spec.md §6):
  * **RTH open → STATIC native OCO** (§6a): target = ref×(1+cw_target%) [+2%], stop =
    ref×(1−cw_hard_stop%) [−5%], anchored off the CW break/**reference** price (`_apply_v2_oco_bracket_entry`
    uses `metadata["entry_price"] or reference_price`, NOT the fill). Modeled as **first-touch on
    the trade tape**: whichever leg the tape reaches first exits; if neither by the 16:00 bell, the
    DAY OCO expires and we **close at the 16:00 price** (what really happened to SKYQ 07-23).
  * **EH open → software CW floor-RIDE** (§6b): the SHARED `cw_exit_decision` is driven tick-by-tick
    over the Schwab LEVELONE bids (the exact fn the OMS `_evaluate_v2_managed_exit` calls), reading
    `oms_v2_cw_*` from Settings. On +target% it ARMS a floor and rides; exits on fallback-to-floor /
    −stop% (pre-arm) / bar-close ATR flip. If NONE of those geometry legs fire, the terminal backstop
    is the **19:55 ET overnight-flatten** (mirrors the live `_v2_overnight_flatten`, reading
    `oms_v2_overnight_flatten_hour_et` / `_minute_et` from Settings): the first bid at/after the
    flatten time closes the position (`exit_reason="overnight-flatten"`), so an EH-opened trade that
    rides the whole session still exits. **The v2 replay exit NEVER touches `ExitEngine`** — that
    divergence (the 07-23 `ExitEngine` vs `cw_exit_decision` drift) is killed here by construction.

Scope (P3): EXTENDED-HOURS ENTRY — fill entries OPENED before 09:30 / after 16:00, so the replay is
faithful for pre/post-market opens too (docs/backtest-replay-engine-design.md P3). The live EH entry is a
**marketable EH-LIMIT at the ask** in BOTH modes, and both run the REAL strategy code here:
  * **reactive-EH**: `_cw_v2_quote` breaks the trigger intrabar (with its EH live-bar guard), then the
    SHARED `entry_gate.route_extended_hours` stamps session=AM/PM + a limit at the ask — the exact bot
    routing. When `oms_v2_eh_entry_enabled` is ON the replay then applies the P-B1 cross-cap/abandon.
  * **resting-EH**: `_eh_resting_cross_check` (P-B2) software-emulates the dead broker stop — on the ATR
    up-cross it emits a marketable EH-LIMIT tagged `eh_resting`; the replay band-caps it to
    min(ask, level×(1+band)) and ABANDONS a gap-through, mirroring the OMS `_apply_v2_eh_resting_entry`.
The EH-limit FILL/ABANDON model (`_eh_entry_reprice`) is a SMALL simulation of the DB/broker-coupled OMS
pre-submit re-price (design doc: the OMS is SIMULATE, not instantiated — same class as the static-OCO
first-touch), reading the SAME Settings + draft metadata so it can't drift on the values. Enable the EH
paths in the replay via `build_replay_settings(eh_enabled=True)` (or the two flags directly); the LIVE
deployed defaults stay OFF, so the default replay is RTH-only exactly like production. An EH-opened
position exits via the same P2 floor-ride geometry (selected by the RTH/EH open). ⚠ EH real-data parity is
DEFERRED: there are NO real EH trades yet (the live EH flags are dormant), so P3 proves the EH MECHANISM on
synthetic fixtures only — the real-fill parity gate is a follow-up once real EH fills exist. The Webull
mirror leg (dual-broker bake-off) is OUT OF SCOPE for P3.

Data: Schwab 1-min bars (`strategy_bar_history`, the LIVE decision source — NOT Polygon,
per the bar-source-defect rule) + Schwab LEVELONE quotes (`market_quote_ticks` provider
'schwab'), both via `backtest.data`. Feed-coverage honesty: a too-sparse feed is a
SKIP-with-reason, never a silent absence.

The ONE strategy infra-dependency that a standalone replay must neutralize is the
strategy's **wall-clock reads** in the resting-entry path (`_now_ms`, `_resting_in_window`,
`_resting_session_is_eh`). `ReplayStrategy` overrides exactly those to read the injected
HISTORICAL clock — no entry logic is re-implemented; only "now" is substituted for the
replayed instant (the no-look-ahead requirement). Everything else runs in the real class.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import Quote as TapeQuote
from project_mai_tai.backtest.data import SchwabBar
from project_mai_tai.backtest.data import Trade as TapeTrade
from project_mai_tai.exit_logic.cw_exit import cw_exit_decision
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.market_data.schwab_v2_rest_client import Quote as StratQuote
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import entry_gate
from project_mai_tai.backtest.watch_start import WatchWindow, watch_start_for
from project_mai_tai.strategy_core.schwab_1m_v2 import SchwabV2Strategy

EASTERN = ZoneInfo("America/New_York")

# Bars are delivered to the strategy at bar CLOSE (minute start ts + 60s), so a quote at t
# only ever sees bars closed <= t (no look-ahead) and the resting live-bar gate (180s) sees a
# realistic ~60s bar age at placement — exactly the live handoff.
BAR_CLOSE_OFFSET_MS = 60_000

# Coverage honesty: fewer Schwab 1-min bars than this in the loaded window = too sparse to
# replay the ATR flip faithfully (the ATR trail needs ~period+1 bars to define, and the flip
# machinery needs a run of them). Report as a SKIP with the count, never a silent no-entry.
MIN_BARS_FOR_REPLAY = 8

# The regular session (ET). The RTH-vs-EH open decides the exit geometry and the native OCO is
# a regular-session construct — this mirrors `oms.service._is_regular_market_session` /
# `_extended_hours_session` (09:30 <= t < 16:00 ET). Kept local so the CI replay stays hermetic
# (no oms.service import); the parity gate pins it against the real fills.
RTH_OPEN_ET = (9, 30)
RTH_CLOSE_ET = (16, 0)


def _is_rth(dt_utc: datetime) -> bool:
    et = dt_utc.astimezone(EASTERN)
    open_et = et.replace(hour=RTH_OPEN_ET[0], minute=RTH_OPEN_ET[1], second=0, microsecond=0)
    close_et = et.replace(hour=RTH_CLOSE_ET[0], minute=RTH_CLOSE_ET[1], second=0, microsecond=0)
    return open_et <= et < close_et


def _schwab_round_price(price: float) -> float:
    """Numeric mirror of `oms.service._schwab_round`: >$1 -> 2dp, <=$1 -> 4dp. The native OCO legs
    are rounded to this tick rule live (firm-rejects otherwise), so the first-touch model rounds the
    target/stop to the SAME levels the broker would actually rest."""
    return round(price, 2) if price > 1.0 else round(price, 4)


# ------------------------------------------------------------------- outputs
@dataclass(frozen=True)
class ReplayEntry:
    symbol: str
    mode: str          # "resting" | "reactive"
    order_type: str    # "STOP_LIMIT" | "market" | "limit"
    signal_ts: datetime  # when the order was placed (resting) / the break fired (reactive)
    fill_ts: datetime    # when it filled on the tape
    level: float         # the ATR line / trigger the entry keyed off
    fill_price: float
    # The CW break/reference price the OCO anchors off (metadata entry_price/reference_price) — the
    # RTH static-OCO target/stop are struck off THIS, not the realized fill (per spec §6a).
    entry_ref: float = 0.0


# The full entry->exit trade — the P2 deliverable. exit_reason is the canonical enum.
@dataclass(frozen=True)
class ReplayTrade:
    symbol: str
    mode: str               # "resting" | "reactive"
    geometry: str           # "rth_static_oco" | "eh_floor_ride"
    entry_ts: datetime
    entry_px: float         # the realized entry FILL (the cost basis for ret_pct)
    entry_ref: float        # the CW break/reference anchor for the OCO legs
    exit_ts: datetime
    exit_px: float
    ret_pct: float
    exit_reason: str        # target | stop | floor | flip | close-at-bell | overnight-flatten


@dataclass(frozen=True)
class ReplaySkip:
    symbol: str
    reason: str
    detail: str = ""


@dataclass
class ReplayResult:
    symbol: str
    session_day_et: str
    n_bars: int
    n_quotes: int
    entries: list[ReplayEntry] = field(default_factory=list)
    skips: list[ReplaySkip] = field(default_factory=list)
    # Resting orders that were placed and worked but never filled on the tape (honest MISS).
    misses: list[ReplaySkip] = field(default_factory=list)
    # Full entry->exit trades (P2). One per filled entry once the exit resolves.
    trades: list[ReplayTrade] = field(default_factory=list)
    # How many armed segments the #618/#619 watch-start cap disqualified. Reported rather than left
    # implicit: a capped segment is an entry the replay did NOT take, and "no entry" must never be
    # indistinguishable from "no signal" (the CLRO silent-absence lesson).
    n_watch_start_capped: int = 0


# ------------------------------------------------------------------- config
# ⭐ FALLBACK ONLY (2026-07-28). These are the live-LOCKED spec values (docs/schwab-1m-v2-live-spec.md
# §8) encoded so an off-VPS / CI replay is faithful WITHOUT an env file. They fill in a field only
# when the base Settings did NOT explicitly set it — i.e. on the VPS the ENV WINS.
#
# ⛔ THEY USED TO OVERRIDE THE ENV, and had silently gone stale. Measured 2026-07-28, live-vs-replay:
#     cw_v2_reclaim_enabled          live True  -> replay False   (reclaim went back ON 07-27)
#     cw_v2_eh_resting_entry_enabled live True  -> replay False   (EH flags ON since 07-24)
#     oms_v2_eh_entry_enabled        live True  -> replay False
# Reclaim off alone drops `max_entries_per_flip` from 2 to 1, so the replay could not even model the
# second entry in a segment. Every backtest was studying a configuration we were not trading, and
# nothing said so. Fallback-not-override makes this self-syncing: whatever production runs, the
# replay runs, and this list only matters where there is no env at all.
LIVE_LOCKED = dict(
    strategy_schwab_1m_v2_enabled=True,
    strategy_schwab_1m_v2_confirmed_window_enabled=True,
    strategy_schwab_1m_v2_cw_v2_enabled=True,
    strategy_schwab_1m_v2_atr_only_mode=True,
    strategy_schwab_1m_v2_atr_flip_enabled=True,
    strategy_schwab_1m_v2_atr_flip_quantity=2,
    strategy_schwab_1m_v2_atr_flip_vol_floor=10000,
    strategy_schwab_1m_v2_atr_flip_period=5,
    strategy_schwab_1m_v2_atr_flip_factor=3.5,
    strategy_schwab_1m_v2_cw_v2_reactive_entry_enabled=True,
    strategy_schwab_1m_v2_cw_v2_reclaim_enabled=False,
    strategy_schwab_1m_v2_cw_v2_resting_entry_enabled=True,
    strategy_schwab_1m_v2_cw_v2_resting_entry_band_pct=0.5,
    strategy_schwab_1m_v2_cw_v2_resting_entry_reprice_pct=0.5,
    strategy_schwab_1m_v2_cw_v2_resting_entry_min_short_bars=3,
    strategy_schwab_1m_v2_cw_v2_resting_entry_max_bar_age_secs=180.0,
    strategy_schwab_1m_v2_cw_v2_resting_entry_flip_grace_secs=30.0,
    # EH ENTRY flags — the LIVE DEPLOYED DEFAULTS (both OFF; the EH flags are dormant until enabled
    # post-4PM / Monday). Encoded explicitly so the default replay is RTH-only exactly like production.
    # `build_replay_settings(eh_enabled=True)` (or a direct override) flips these ON so the real EH entry
    # paths execute — the ONLY switch the operator flips to replay an "EH-enabled" day.
    strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=False,  # P-B2 resting-EH (strategy + OMS share this)
    oms_v2_eh_entry_enabled=False,                               # P-B1 reactive-EH OMS cross-cap/abandon
    strategy_schwab_1m_v2_entry_window_start_hour_et=7,
    strategy_schwab_1m_v2_entry_window_start_minute_et=0,
    strategy_schwab_1m_v2_entry_window_end_hour_et=16,
    strategy_schwab_1m_v2_entry_window_end_minute_et=0,
    # ⭐ PER-SYMBOL WATCH-START CAP (#618/#619, 2026-07-30). Live env carries this TRUE. It gates
    # `_cap_reconstructed_segment`, which disqualifies an armed segment whose flip bar predates the
    # symbol's watchlist join. It fired 76 times in the live v2 log on 2026-07-30 alone, so a replay
    # that runs without it studies a strictly MORE PERMISSIVE bot than the one we trade.
    strategy_schwab_1m_v2_cw_armed_segment_safety_enabled=True,
)


# The EH-enabled overlay: turns BOTH extended-hours entry flags ON so the real EH paths execute (the
# strategy emits `_eh_resting_cross_check` + the OMS re-price simulation applies the P-B1/P-B2 cap). This
# is the SINGLE, explicit switch a replay flips to study a pre/post-market "EH-enabled" day; the LIVE
# deployed defaults (LIVE_LOCKED, both OFF) are unchanged.
EH_ENABLED = dict(
    strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled=True,  # P-B2 resting-EH cross-check + band-cap
    oms_v2_eh_entry_enabled=True,                               # P-B1 reactive-EH cross-cap/abandon
)


# ⛔ FORCED regardless of the env — MODELLING choices, not config drift, so they must win over both
# LIVE_LOCKED and the base.
#
# ⛔⭐ EMPTIED 2026-07-31. It used to force `cw_armed_segment_safety_enabled=False`, reasoning that
# the flag was only the BOOT-HOLD, which the live bot releases after its one-time verify. That was
# true when written. **#618/#619 (2026-07-30) changed what the flag means**: it now ALSO gates
# `_cap_reconstructed_segment`, a per-symbol watch-start test that runs ALL SESSION and suppresses
# entries whose flip predates the symbol's watchlist join. That is steady state, not a startup
# transient — so forcing the flag off silently deleted a live entry gate from every backtest (it
# fired 76 times live on 07-30). Exactly the #592 staleness defect, one dict over.
#
# The genuine modelling choice — released boot-hold — is expressed directly and narrowly as
# `_entries_held = False` in `ReplayStrategy.__init__`, so it no longer rides on a settings flag
# whose meaning can drift underneath it.
REPLAY_FORCED: dict[str, object] = {}


def build_replay_settings(
    base: Settings | None = None, *, eh_enabled: bool = False, **overrides
) -> Settings:
    """Faithful live-regime Settings for the replay.

    Precedence, lowest to highest:
      1. `base` (the env-merged `Settings()` on the VPS = what production actually runs)
      2. LIVE_LOCKED — **only for fields the base did not explicitly set** (off-VPS / CI fallback)
      3. EH_ENABLED  — when `eh_enabled=True`
      4. REPLAY_FORCED — modelling choices (boot-hold released) that must beat the env
      5. `**overrides` — explicit caller/test values, always win

    ⭐ Step 2 is a FALLBACK, not an override (changed 2026-07-28). It used to clobber the env, and
    had gone stale: reclaim and both EH flags were ON live but forced OFF in every replay, so the
    engine was studying a configuration we were not trading. Reclaim off alone drops
    `max_entries_per_flip` from 2 to 1.

    `eh_enabled=True` forces both extended-hours entry flags ON so the replay runs the real EH entry
    paths, regardless of what the base says — the explicit way to study a pre/post-market day. It is
    no longer needed just to MATCH production (the env already carries the live EH state); it is for
    forcing EH on where there is no env, or overriding an env that has it off."""
    merged = dict(base.model_dump()) if base is not None else {}
    # ⭐ FALLBACK, not override. `model_fields_set` is exactly the set the ENV (or an explicit kwarg)
    # supplied, so a value production actually runs is never clobbered by this stale-able list.
    # Off-VPS / CI there is no env, the set is empty, and LIVE_LOCKED applies in full as before.
    env_set = set(base.model_fields_set) if base is not None else set()
    merged.update({k: v for k, v in LIVE_LOCKED.items() if k not in env_set})
    if eh_enabled:
        merged.update(EH_ENABLED)
    merged.update(REPLAY_FORCED)   # modelling choices beat both the env and LIVE_LOCKED
    merged.update(overrides)       # explicit caller overrides still win over everything
    return Settings(**merged)


# ------------------------------------------------------------------- clock-injecting strategy
class ReplayStrategy(SchwabV2Strategy):
    """The REAL entry strategy with the wall-clock reads substituted for the injected historical
    clock. Overrides ONLY the three time sources the resting path reads from `datetime.now(UTC)`;
    all entry logic (ATR flip, wait-3 reactive break, resting place/reprice/cancel, gates) runs
    unchanged in the base class."""

    def __init__(
        self,
        settings: Settings,
        *,
        watch_start_ms: int | None = None,
        watch_windows: list[WatchWindow] | None = None,
    ) -> None:
        super().__init__(settings)
        self._replay_now_ms = 0
        # Steady-state: the live bot releases boot-hold after its verify; the replay starts released.
        # ⛔ This is the ONE genuine modelling choice, and it is deliberately expressed HERE rather
        # than by forcing a settings flag — the flag that used to carry it grew a second meaning
        # (the #618 watch-start cap) and took this override's blast radius with it.
        self._entries_held = False
        # Epoch ms from which this symbol's flips count as OBSERVED-LIVE (its watchlist join).
        # None => fall back to `_boot_ms`, i.e. pre-2026-07-30 behaviour.
        self._replay_watch_start_ms = watch_start_ms
        # Preferred over the scalar: the symbol's full membership windows for the day. Resolving
        # PER-ARM is strictly more faithful than one scalar, because the scanner feed flickers --
        # a symbol can confirm/fade/re-confirm several times in a minute and the bot re-stamps its
        # watch-start on every re-join, so which join an arm is measured against depends on WHEN
        # it armed. `None` = not loaded (fall back); `[]` = loaded, never confirmed (fall back).
        self._replay_watch_windows = watch_windows

    def cap_reconstructed_segment(self, symbol: str) -> bool:
        """Replay mirror of `services/schwab_1m_v2_bot.py::_cap_reconstructed_segment` (#618/#619).

        Disqualify an armed segment whose flip bar predates the point we began WATCHING this symbol,
        so the replay can only enter on a flip it actually saw happen — the same rule the live bot
        applies. Live measured 2026-07-30: APLX flipped 09:16 ET but joined the watchlist 09:38 ET,
        SNDG flipped 09:23 / joined 09:34; both were bought at 10:00 (+23.7% / +18.9% past the
        signal) and both stopped out. Without this, the replay reproduces those entries as if they
        were legitimate and every study built on it reads optimistic.

        ⛔ `<=`, not `<`: a bar timestamp is the bar's OPEN, so a symbol that joined at 09:38:30 was
        not watching when the 09:38 bar opened. Fail-closed, exactly like live.

        ⛔ Bars are still INGESTED — the ATR needs the history. Only the ARM they produce is
        disqualified. Returns True when a segment was capped (for test assertions / reporting).
        """
        if not getattr(self, "_cw_armed_segment_safety_enabled", False):
            return False
        st = self.watchlist_state(symbol)
        max_e = self._cw_v2_max_entries_per_flip
        if self._replay_watch_windows:
            # Measure THIS arm against the membership window it fell in.
            resolved = watch_start_for(self._replay_watch_windows, st.cw_arm_bar_ts)
            # None => the arm predates every CONFIRM of the day: we demonstrably were NOT watching
            # when it flipped, so cap. Fail-closed, matching live.
            watch_start = resolved if resolved is not None else st.cw_arm_bar_ts
        else:
            watch_start = self._replay_watch_start_ms
            if watch_start is None:
                watch_start = self._boot_ms
        if st.cw_armed and 0 < st.cw_arm_bar_ts <= watch_start and st.cw_entries_this_flip < max_e:
            st.cw_entries_this_flip = max_e
            return True
        return False

    def set_clock_ms(self, now_ms: int) -> None:
        self._replay_now_ms = int(now_ms)

    def _now_ms(self) -> int:  # override wall-clock (used by resting live-bar gate + flip grace)
        return self._replay_now_ms

    def _replay_now(self) -> datetime:
        return datetime.fromtimestamp(self._replay_now_ms / 1000.0, UTC)

    def _resting_in_window(self, now: datetime | None = None) -> bool:
        return super()._resting_in_window(now if now is not None else self._replay_now())

    def _resting_session_is_eh(self, now: datetime | None = None) -> bool:
        return super()._resting_session_is_eh(now if now is not None else self._replay_now())


# ------------------------------------------------------------------- adapters
def _to_chartbar(symbol: str, b: SchwabBar) -> ChartBar:
    return ChartBar(symbol=symbol, open=b.open, high=b.high, low=b.low,
                    close=b.close, volume=int(b.volume), timestamp_ms=int(b.ts))


def _to_stratquote(symbol: str, q: TapeQuote) -> StratQuote:
    return StratQuote(
        symbol=symbol,
        bid_price=float(q.bid),
        ask_price=float(q.ask),
        last_price=float(q.last) if q.last is not None else 0.0,
        quote_time_ms=int(q.ts.timestamp() * 1000),
    )


# ------------------------------------------------------------------- EH entry re-price (P3)
@dataclass(frozen=True)
class _EHFill:
    """Outcome of the EH-limit re-price model. `reason_code` == "" => FILL at `fill_price`; else the
    entry ABANDONS (no fill) with the OMS abandon code (`ASK_PAST_BAND` / `ASK_PAST_CROSS_CAP` /
    `NO_FRESH_QUOTE` / `MISSING_SIGNAL`)."""
    fill_price: float
    entry_ref: float
    reason_code: str


def _eh_entry_reprice(md: dict, ask: float, settings: Settings, *, is_resting: bool) -> _EHFill:
    """Simulate the OMS extended-hours pre-submit re-price/band-cap/ABANDON (P3).

    The live EH entry is a marketable EH-LIMIT at the ask; the OMS then re-prices + bounds it just
    before submit. That OMS code (`oms.service._apply_v2_eh_resting_entry` /
    `_apply_v2_eh_reactive_entry`) is DB/broker-coupled, so — per the design doc's "OCO emit /
    stand-down = SIMULATE" and Decision 2 (strategy+exit replay, NOT a full OMS mock) — the replay
    mirrors ONLY its arithmetic, reading the SAME Settings + draft metadata so it cannot drift on the
    values (the parity gate pins the residual, exactly like `_static_oco_first_touch`).

      * **resting (P-B2, `eh_resting=true`)**: cap = level×(1+band); FILL at min(ask, cap) if the ask is
        in the band, else ABANDON `ASK_PAST_BAND` (the RTH broker stop-limit would MISS this gap-through
        too — no chase). `level` = the strategy's resting_level; `band` = its own `resting_band_pct`
        (the setting is the fallback belt).
      * **reactive (P-B1, `oms_v2_eh_entry_enabled` ON)**: cap = signal×(1+max_cross%); FILL at the ask
        if ask ≤ cap, else ABANDON `ASK_PAST_CROSS_CAP`. `signal` = the break level (`entry_price`).
      * **reactive, P-B1 OFF**: byte-identical to the pre-P3 reactive-EH path — the bot's plain
        limit-at-ask stands (fill at the routed `limit_price`, i.e. the ask), no cap/abandon.

    A marketable buy fills at the OFFER, so the modeled fill is the (capped) ask — the same conservative
    no-blind-order / no-chase bias as live (`min(ask, cap)` == ask whenever the ask is inside the cap)."""
    if ask <= 0.0:
        return _EHFill(0.0, 0.0, "NO_FRESH_QUOTE")
    if is_resting:
        level = float(md.get("resting_level") or md.get("entry_price") or 0.0)
        if level <= 0.0:
            return _EHFill(0.0, 0.0, "MISSING_SIGNAL")
        try:
            band_pct = float(md["resting_band_pct"])
        except (KeyError, TypeError, ValueError):
            band_pct = float(getattr(settings, "oms_v2_eh_resting_entry_band_pct", 0.5))
        cap = level * (1.0 + band_pct / 100.0)
        if ask > cap:
            return _EHFill(0.0, level, "ASK_PAST_BAND")
        return _EHFill(min(ask, cap), level, "")
    # reactive
    if not bool(getattr(settings, "oms_v2_eh_entry_enabled", False)):
        # P-B1 OFF: the bot's plain limit-at-ask stands (the pre-P3 behavior). `limit_price` == the ask.
        limit = float(md.get("limit_price") or ask)
        return _EHFill(limit, float(md.get("entry_price") or ask), "")
    signal_px = float(md.get("entry_price") or 0.0)
    if signal_px <= 0.0:
        return _EHFill(0.0, 0.0, "MISSING_SIGNAL")
    max_cross_pct = float(getattr(settings, "oms_v2_eh_entry_max_cross_pct", 1.0))
    cap = signal_px * (1.0 + max_cross_pct / 100.0)
    if ask > cap:
        return _EHFill(0.0, signal_px, "ASK_PAST_CROSS_CAP")
    return _EHFill(min(ask, cap), signal_px, "")


# ------------------------------------------------------------------- exit models
@dataclass
class _RestingOrder:
    stop: float
    limit: float
    place_ts: datetime
    entry_ref: float


def _static_oco_first_touch(
    entry_ref: float,
    tape: list[tuple[datetime, float]],
    *,
    target_pct: float,
    stop_pct: float,
    close_dt: datetime,
    flip_dt: datetime | None = None,
) -> tuple[datetime, float, str]:
    """RTH-open geometry: the broker-native OCO is STATIC (spec §6a). Struck off the CW break/
    reference price (NOT the fill), the child OCO is `SELL LIMIT @ target` + `SELL STOP @ protect`,
    both rounded to the Schwab tick rule — exactly `_apply_v2_oco_bracket_entry`.

    First-touch on the trade tape (prints in [entry, 16:00), time-ordered): the SELL LIMIT fills on
    the first print that reaches the target (>= target); the SELL STOP triggers on the first print
    that reaches the protect (<= stop). Whichever the tape reaches first is the exit (a print is a
    single price, so target/stop are mutually exclusive per print — no same-print ambiguity). If
    NEITHER leg is touched by the 16:00 bell, the DAY OCO expires → **close at the 16:00 price** (the
    last print <= the close).

    ⭐ THIRD LEG — `flip_dt` (the live bar-close ATR SELL-flip). The broker OCO is NOT the only exit
    live: `schwab_1m_v2._maybe_cw_flip_close` fires whenever CW is on, we hold, and a bar CLOSES below
    the ATR trail — and it has **no RTH gate**, so it races the OCO in regular hours too. Omitting it
    made SMCX 2026-07-22 drift to the bell at −2.81% when live would have flip-closed at 14:33
    (operator caught it off a TOS chart). `flip_dt` is the bar-close instant the REAL strategy emitted
    the cw_flip draft; the modeled fill is the FIRST PRINT at/after it, mirroring the live bot→OMS
    handoff (the OMS closes the managed row on the next quote). Target/stop are checked first on a
    given print because those legs rest AT the exchange, while the flip is a software close that has
    to go out on the next tick. Returns (exit_ts, exit_px, reason)."""
    target = _schwab_round_price(entry_ref * (1.0 + target_pct / 100.0))
    stop = _schwab_round_price(entry_ref * (1.0 - stop_pct / 100.0))
    last_ts: datetime | None = None
    last_px: float | None = None
    for ts, px in tape:
        if px >= target:
            return ts, target, "target"     # SELL LIMIT fills at the target
        if px <= stop:
            return ts, stop, "stop"         # SELL STOP triggers, modeled fill at the stop level
        if flip_dt is not None and ts >= flip_dt:
            return ts, px, "flip"           # software cw_flip close -> first print after the bar close
        last_ts, last_px = ts, px
    # Neither leg by the close: the DAY OCO lapses; close at the 16:00 price (last print seen).
    if last_px is None:
        return close_dt, target, "close-at-bell"  # no prints post-entry (degenerate) -> ref-level
    return last_ts or close_dt, last_px, "close-at-bell"


def replay_symbol_day(
    source,
    symbol: str,
    session_day_et: str,
    settings: Settings,
    *,
    window_start_hour_et: int = 4,
    window_end_hour_et: int = 20,
    watch_start_ms: int | None = None,
) -> ReplayResult:
    """Replay one symbol for one ET session day through the real entry code + shared emit-gate.

    Loads Schwab bars/quotes for [start, end) ET, feeds them to `ReplayStrategy` in strict time
    order (bars at close = ts+60s, quotes at ts; no look-ahead), gates reactive drafts through
    `entry_gate.gate_open_intent`, emits resting place/cancel drafts directly (they bypass the
    chokepoint live too), and applies the honest ENTRY fill model:
      * resting STOP_LIMIT (RTH): fills at the first quote whose ask lands in the band [stop, limit]
        (limit = stop*(1+band)), price = that ask; a break that gaps the whole band, or that never
        reaches the stop, => MISS.
      * reactive (RTH): marketable => fills at the break price.
      * EXTENDED-HOURS open (P3, both modes — the gate stamped session=AM/PM): fills via the EH-limit
        model (`_eh_entry_reprice`) — the capped marketable ask (resting: min(ask, level*(1+band));
        reactive: min(ask, signal*(1+max_cross%))), or an honest ABANDON/MISS on gap-through / no fresh
        ask. The resting-EH cross is the REAL `_eh_resting_cross_check`; the reactive-EH break is the REAL
        `_cw_v2_quote` (EH live-bar guard included), routed by the SHARED `route_extended_hours`.
    Returns the replayed entries (+ skips/misses with reasons).
    """
    day = datetime.strptime(session_day_et, "%Y-%m-%d").replace(tzinfo=EASTERN)
    start = day.replace(hour=window_start_hour_et, minute=0, second=0, microsecond=0)
    end = day.replace(hour=window_end_hour_et, minute=0, second=0, microsecond=0)

    bars = source.schwab_bars(symbol, start, end)
    quotes = source.schwab_quotes(symbol, start, end)
    result = ReplayResult(symbol=symbol, session_day_et=session_day_et,
                          n_bars=len(bars), n_quotes=len(quotes))

    if len(bars) < MIN_BARS_FOR_REPLAY:
        result.skips.append(ReplaySkip(
            symbol, "sparse_schwab_feed",
            f"only {len(bars)} Schwab 1-min bars in {window_start_hour_et:02d}:00-"
            f"{window_end_hour_et:02d}:00 ET (< {MIN_BARS_FOR_REPLAY}); too sparse to replay the ATR flip",
        ))
        return result

    # #618/#619: resolve the symbol's watchlist-membership windows from the durable scanner feed
    # unless the caller supplied an explicit scalar. Without this the cap falls back to the window
    # start and is INERT in every real report -- a gate that exists and guards nothing, which is the
    # exact failure this whole change is fixing. A source with no such feed (fixtures) returns None
    # and the fallback applies, keeping the golden gate hermetic.
    day_windows: list[WatchWindow] | None = None
    if watch_start_ms is None and hasattr(source, "watch_windows"):
        try:
            day_windows = source.watch_windows(symbol, day.date())
        except Exception:  # noqa: BLE001 - a research feed must never break the replay
            day_windows = None

    strat = ReplayStrategy(settings, watch_start_ms=watch_start_ms, watch_windows=day_windows)
    # ⛔⭐ `_boot_ms` is WALL-CLOCK-NOW in the live strategy (schwab_1m_v2.py: `datetime.now(UTC)`),
    # because live "boot" genuinely is when we started watching. In a replay of a PAST day that
    # reference is after the entire session, so the watch-start cap's `arm_bar_ts <= watch_start`
    # would be true for EVERY segment and silently cap the whole day to zero entries. Re-point it at
    # the instant this replay started watching, which is the faithful analogue: an arm whose bar
    # predates the loaded window came from seeded/warmup history we did not observe live, and live
    # caps exactly those.
    strat._boot_ms = int(start.timestamp() * 1000)
    qty = strat._atr_qty
    n_capped = 0

    # Merge into a single time-ordered event stream. eff_ts is the instant the event reaches the
    # strategy: bars at close (ts+60s), quotes at their own ts. On a tie, the bar (minute boundary)
    # is delivered before the quote so a quote at t sees the bar closed AT t.
    events: list[tuple[int, int, object]] = []
    for b in bars:
        events.append((int(b.ts) + BAR_CLOSE_OFFSET_MS, 0, b))
    for q in quotes:
        events.append((int(q.ts.timestamp() * 1000), 1, q))
    events.sort(key=lambda e: (e[0], e[1]))

    # Post-entry trade tape for the RTH static-OCO first-touch — the native OCO fills/triggers
    # against the actual prints (spec §6a). Loaded once; sliced to [entry, 16:00) when a fill lands.
    trades: list[TapeTrade] = source.trades(symbol, start, end) if hasattr(source, "trades") else []
    rth_close_dt = day.replace(hour=RTH_CLOSE_ET[0], minute=RTH_CLOSE_ET[1], second=0, microsecond=0)

    # Live exit params (spec §6) from Settings — the SAME values the OMS passes to cw_exit_decision,
    # so the EH floor-ride is the live decision verbatim and RTH OCO legs are struck at live levels.
    cw_target_pct = float(getattr(settings, "oms_v2_cw_target_pct", 2.0))
    cw_stop_pct = float(getattr(settings, "oms_v2_cw_hard_stop_pct", 5.0))
    cw_floor_pct = float(getattr(settings, "oms_v2_cw_floor_pct", 2.0))
    cw_floor_enabled = bool(getattr(settings, "oms_v2_cw_floor_exit_enabled", False))

    # Overnight-flatten backstop endpoint (spec § overnight flatten): the live `_v2_overnight_flatten`
    # closes every still-held managed v2 position at 19:55 ET before the 20:00 fillable gate. Read the
    # hour/minute from Settings (same keys the OMS reads) so the replay endpoint tracks the live clock.
    flatten_hh = int(getattr(settings, "oms_v2_overnight_flatten_hour_et", 19))
    flatten_mm = int(getattr(settings, "oms_v2_overnight_flatten_minute_et", 55))
    overnight_flatten_dt = day.replace(hour=flatten_hh, minute=flatten_mm, second=0, microsecond=0)

    resting: _RestingOrder | None = None
    filled = False           # one entry per symbol
    entry_rec: ReplayEntry | None = None
    geometry = ""            # "rth_static_oco" | "eh_floor_ride"
    exit_done = False
    eh_armed = False         # EH floor-ride: cw_exit_decision floor-armed state
    eh_flip_pending = False  # EH floor-ride: a bar-close ATR SELL-flip fired while holding
    eh_last_bid: tuple[datetime, float] | None = None
    latest_stratquote: dict[str, StratQuote] = {}

    def _open_static_oco(e: ReplayEntry, flip_dt: datetime | None = None) -> None:
        """RTH open -> the broker owns a STATIC OCO, RACED against the live software cw_flip close.
        Resolve by first-touch on the trade tape (`flip_dt` = the bar-close instant the REAL strategy
        emitted the flip draft, or None if it never did)."""
        nonlocal exit_done
        tape = [(t.ts, float(t.price)) for t in trades if e.fill_ts <= t.ts < rth_close_dt]
        exit_ts, exit_px, reason = _static_oco_first_touch(
            e.entry_ref, tape, target_pct=cw_target_pct, stop_pct=cw_stop_pct,
            close_dt=rth_close_dt, flip_dt=flip_dt,
        )
        ret = (exit_px - e.fill_price) / e.fill_price * 100.0 if e.fill_price else 0.0
        result.trades.append(ReplayTrade(
            symbol=symbol, mode=e.mode, geometry="rth_static_oco",
            entry_ts=e.fill_ts, entry_px=e.fill_price, entry_ref=e.entry_ref,
            exit_ts=exit_ts, exit_px=exit_px, ret_pct=ret, exit_reason=reason,
        ))
        exit_done = True

    def _record_fill(e: ReplayEntry) -> None:
        """Record the entry, mark the symbol in-position, and select the exit geometry by the OPEN
        session. RTH resolves the static OCO immediately (broker-arbitrated); EH continues the loop
        so the SHARED cw_exit_decision rides the tape bids."""
        nonlocal filled, entry_rec, geometry
        result.entries.append(e)
        strat.update_position(symbol, qty)
        filled = True
        entry_rec = e
        if _is_rth(e.fill_ts):
            # DO NOT resolve here. The live bar-close cw_flip races the broker OCO in RTH too, so the
            # loop must keep running to hear it from the REAL strategy; resolution happens on the flip
            # bar, or at the end of the loop if no flip ever fires.
            geometry = "rth_static_oco"
        else:
            geometry = "eh_floor_ride"

    def _gate_and_maybe_fill(draft, eff_dt: datetime) -> None:
        """Run a strategy-returned draft (reactive break OR the P-B2 EH resting cross) through the SHARED
        emit-gate; on emit, apply the fill model and record the entry (which selects/opens the exit
        geometry). RTH reactive fills at the marketable break price; an EXTENDED-HOURS open (the gate
        stamped session=AM/PM) fills via the EH-limit model (`_eh_entry_reprice`) — fill at the capped
        ask, or an honest ABANDON/MISS on gap-through / no fresh ask."""
        decision = entry_gate.gate_open_intent(draft, eff_dt, settings, latest_stratquote.get)
        if not decision.emit:
            return
        md = decision.draft.metadata
        order_type = str(md.get("order_type", "market")).lower()
        # The strategy tags the resting-EH cross (`_eh_resting_cross_check`) eh_resting/resting_entry;
        # everything else through here is a reactive break.
        is_resting = (
            str(md.get("eh_resting", "")).lower() == "true"
            or str(md.get("resting_entry", "")).lower() == "true"
        )
        mode = "resting" if is_resting else "reactive"
        # EXTENDED-HOURS entry (P3): the gate stamped session=AM/PM via `route_extended_hours`. Fill via
        # the EH-limit model against the current ask (the SAME feed the gate routed off), or ABANDON.
        if str(md.get("session", "")).upper() in ("AM", "PM"):
            sq = latest_stratquote.get(symbol.upper())
            ask = float(getattr(sq, "ask_price", 0.0) or 0.0) if sq is not None else 0.0
            eh = _eh_entry_reprice(md, ask, settings, is_resting=is_resting)
            if eh.reason_code:
                result.misses.append(ReplaySkip(
                    symbol, "eh_entry_abandoned",
                    f"{mode} EH entry abandoned ({eh.reason_code}) "
                    f"{eff_dt.astimezone(EASTERN):%H:%M:%S} ET ask={ask:.4f} — mirrors OMS "
                    f"_apply_v2_eh_{'resting' if is_resting else 'reactive'}_entry (no chase / no blind order)",
                ))
                return
            level = float(md.get("cw_trigger") or md.get("resting_level") or eh.entry_ref or 0.0)
            _record_fill(ReplayEntry(
                symbol=symbol, mode=mode, order_type="limit",
                signal_ts=eff_dt, fill_ts=eff_dt, level=level,
                fill_price=eh.fill_price, entry_ref=eh.entry_ref,
            ))
            return
        # RTH reactive: marketable at the break price. The OCO anchor is the CW break/reference price
        # (metadata entry_price/reference_price) — the exact field `_apply_v2_oco_bracket_entry` reads.
        level = float(md.get("cw_trigger") or md.get("reference_price") or md.get("entry_price") or 0.0)
        entry_ref = float(md.get("entry_price") or md.get("reference_price") or 0.0)
        fill_price = float(md.get("entry_price") or md.get("reference_price") or 0.0)
        _record_fill(ReplayEntry(
            symbol=symbol, mode=mode, order_type=order_type,
            signal_ts=eff_dt, fill_ts=eff_dt, level=level, fill_price=fill_price, entry_ref=entry_ref,
        ))

    def _finish_eh_exit(exit_ts: datetime, exit_px: float, reason: str) -> None:
        nonlocal exit_done
        e = entry_rec
        assert e is not None
        ret = (exit_px - e.fill_price) / e.fill_price * 100.0 if e.fill_price else 0.0
        result.trades.append(ReplayTrade(
            symbol=symbol, mode=e.mode, geometry="eh_floor_ride",
            entry_ts=e.fill_ts, entry_px=e.fill_price, entry_ref=e.entry_ref,
            exit_ts=exit_ts, exit_px=exit_px, ret_pct=ret, exit_reason=reason,
        ))
        exit_done = True

    for eff_ts, kind, payload in events:
        if exit_done:
            break
        eff_dt = datetime.fromtimestamp(eff_ts / 1000.0, UTC)
        strat.set_clock_ms(eff_ts)

        if kind == 0:  # bar (delivered at close)
            bar = _to_chartbar(symbol, payload)  # type: ignore[arg-type]
            draft = strat.on_bar(symbol, bar)
            # #618/#619 watch-start cap. Live runs this after every replay that can ARM a segment;
            # here every bar is such a replay, and the test is purely `arm_bar_ts <= watch_start`,
            # so running it each bar is equivalent and cannot miss a re-arm (the live 07-27 bug was
            # exactly a re-arm that ran after the one place the cap was called).
            if strat.cap_reconstructed_segment(symbol):
                n_capped += 1
            if not filled:
                # Drain the resting place/cancel drafts the manager queued this bar (bypass the
                # gate, exactly like the bot's direct emit).
                for d in strat.drain_pending_intents():
                    it = getattr(d, "intent_type", "")
                    if it == "cancel":
                        resting = None
                    elif it == "open" and str(d.metadata.get("order_type", "")).upper() == "STOP_LIMIT":
                        resting = _RestingOrder(
                            stop=float(d.metadata["stop_price"]),
                            limit=float(d.metadata["limit_price"]),
                            place_ts=eff_dt,
                            entry_ref=float(
                                d.metadata.get("entry_price")
                                or d.metadata.get("reference_price")
                                or d.metadata["stop_price"]
                            ),
                        )
                # A bar-close reactive draft (rare) also goes through the gate.
                if draft is not None:
                    _gate_and_maybe_fill(draft, eff_dt)
            elif geometry == "eh_floor_ride":
                # EH floor-ride: a bar-close ATR SELL-flip while holding is the trend exit. The REAL
                # strategy returns a cw_flip CLOSE draft (`_maybe_cw_flip_close`, spec §6b). Mirror the
                # bot->OMS handoff: mark flip_pending so the next bid tick closes via cw_exit_decision
                # (precedence target/arm > stop > flip, exactly like the live block). Resting churn
                # while holding is drained + discarded.
                strat.drain_pending_intents()
                if (draft is not None and getattr(draft, "intent_type", "") == "close"
                        and str(getattr(draft, "metadata", {}).get("cw_flip", "")).lower() == "true"):
                    eh_flip_pending = True
            elif geometry == "rth_static_oco":
                # RTH: the broker OCO is resting, but the live software cw_flip close races it
                # (`_maybe_cw_flip_close` has NO RTH gate). Same bot->OMS handoff as the EH branch:
                # the REAL strategy emits the flip draft at the bar close; resolve the OCO with that
                # instant as a third leg (target/stop still win if the tape reached them first).
                strat.drain_pending_intents()
                if (draft is not None and getattr(draft, "intent_type", "") == "close"
                        and str(getattr(draft, "metadata", {}).get("cw_flip", "")).lower() == "true"
                        and entry_rec is not None):
                    _open_static_oco(entry_rec, flip_dt=eff_dt)
            continue

        # quote
        q: TapeQuote = payload  # type: ignore[assignment]
        sq = _to_stratquote(symbol, q)
        latest_stratquote[symbol.upper()] = sq

        if not filled:
            draft = strat.on_quote(symbol, sq)
            if draft is not None:
                _gate_and_maybe_fill(draft, eff_dt)
            # Resting buy-STOP-LIMIT fill: the stop (S) triggers at ask >= S, then it is a LIMIT buy
            # at L = S*(1+band); it fills only if the ask lands in the band [S, L]. Fill @ ask ∈ [S,L];
            # a break that GAPS the whole band does NOT fill — the honest resting-entry miss.
            if not filled and resting is not None and resting.stop <= float(q.ask) <= resting.limit:
                _record_fill(ReplayEntry(
                    symbol=symbol, mode="resting", order_type="STOP_LIMIT",
                    signal_ts=resting.place_ts, fill_ts=eff_dt,
                    level=resting.stop, fill_price=float(q.ask), entry_ref=resting.entry_ref,
                ))
                resting = None
            continue

        if geometry == "eh_floor_ride":
            # EH open -> the SHARED live exit fn drives the floor-ride tick-by-tick over the bids
            # (the exact call `oms.service._evaluate_v2_managed_exit` makes). No ExitEngine anywhere.
            bid = float(q.bid)
            if bid <= 0:
                continue
            eh_last_bid = (eff_dt, bid)
            # Terminal overnight-flatten backstop (mirrors live `_v2_overnight_flatten`): the FIRST bid
            # at/after 19:55 ET closes any still-held EH position. Checked BEFORE the geometry so nothing
            # can exit AFTER the flatten instant (live, the position is already gone by then). The
            # geometry legs (target/floor/stop/flip) only reach this loop on ticks STRICTLY BEFORE the
            # flatten time — an earlier exit sets exit_done and breaks — so an earlier leg always wins;
            # the flatten is the endpoint ONLY when none fired.
            if eff_dt >= overnight_flatten_dt:
                _finish_eh_exit(eff_dt, bid, "overnight-flatten")
                continue
            entry_px = entry_rec.fill_price  # EH ladder anchors off the FILL (managed-row entry_price)
            action, eh_armed = cw_exit_decision(
                entry_px, bid, eh_armed,
                target_pct=cw_target_pct, stop_pct=cw_stop_pct,
                floor_pct=cw_floor_pct, floor_enabled=cw_floor_enabled,
                flip_pending=eh_flip_pending,
            )
            if action in ("arm", "hold"):
                continue
            # exit — the reference price mirrors the live `_evaluate_v2_managed_exit` leg mapping.
            if action == "target":
                _finish_eh_exit(eff_dt, entry_px * (1.0 + cw_target_pct / 100.0), "target")
            elif action == "floor":
                _finish_eh_exit(eff_dt, entry_px * (1.0 + cw_floor_pct / 100.0), "floor")
            elif action == "stop":
                _finish_eh_exit(eff_dt, entry_px * (1.0 - cw_stop_pct / 100.0), "stop")
            else:  # flip -> close at the current bid
                _finish_eh_exit(eff_dt, bid, "flip")

    result.n_watch_start_capped = n_capped
    if n_capped and not result.entries:
        ws_txt = (
            datetime.fromtimestamp(watch_start_ms / 1000.0, UTC).astimezone(EASTERN).strftime("%H:%M:%S")
            if watch_start_ms else "process boot"
        )
        result.skips.append(ReplaySkip(
            symbol, "watch_start_capped",
            f"{n_capped} armed segment(s) disqualified: the ATR flip predates our watch-start "
            f"({ws_txt} ET). Live #618/#619 suppresses these — the flip happened before the scanner "
            f"put this symbol in front of us, so we never saw it happen.",
        ))

    # Any resting order still working at EOD that never crossed = honest MISS.
    if resting is not None and not filled:
        result.misses.append(ReplaySkip(
            symbol, "resting_never_filled",
            f"resting buy-stop-limit [{resting.stop:.4f}, {resting.limit:.4f}] placed "
            f"{resting.place_ts.astimezone(EASTERN):%H:%M:%S} ET never saw an ask in the band "
            f"on the tape (never reached the stop, or gapped through the limit)",
        ))

    # An EH-opened position that never hit floor / -stop / flip AND whose loaded tape ENDS before the
    # 19:55 overnight-flatten time (no bid at/after it to close on): close-at-bell at the last bid seen.
    # The in-loop overnight-flatten is the primary EH backstop; this only catches a tape too short to
    # reach the flatten instant, and bounds the trade honestly rather than letting it ride forever.
    if entry_rec is not None and geometry == "eh_floor_ride" and not exit_done and eh_last_bid is not None:
        ts_, bid_ = eh_last_bid
        _finish_eh_exit(ts_, bid_, "close-at-bell")

    # RTH open whose cw_flip never fired: resolve the static OCO on target/stop/bell alone. (When the
    # flip DID fire the trade is already resolved in-loop with flip_dt set.)
    if entry_rec is not None and geometry == "rth_static_oco" and not exit_done:
        _open_static_oco(entry_rec)

    return result


# ------------------------------------------------------------------- reconciliation (Deliverable 3)
@dataclass(frozen=True)
class RealEntry:
    symbol: str
    entry_price: float
    entry_time_et: datetime
    entry_path: str


def fetch_real_v2_entries(session_factory, session_day_et: str) -> list[RealEntry]:
    """The REAL v2 entries for a day from `oms_managed_positions` (the ground truth to reconcile)."""
    from sqlalchemy import text

    with session_factory() as s:
        rows = s.execute(
            text(
                "SELECT symbol, entry_price, entry_time, entry_path FROM oms_managed_positions "
                "WHERE strategy_code='schwab_1m_v2' "
                "AND (entry_time AT TIME ZONE 'America/New_York')::date = :d "
                "ORDER BY entry_time"
            ),
            {"d": session_day_et},
        ).all()
    return [
        RealEntry(sym, float(px), et.astimezone(EASTERN), str(path or ""))
        for sym, px, et, path in rows
    ]


@dataclass(frozen=True)
class RealExit:
    symbol: str
    exit_price: float
    exit_time_et: datetime


def fetch_real_v2_exit(session_factory, symbol: str, session_day_et: str) -> RealExit | None:
    """Best-effort REAL exit fill for a v2 symbol on a day, from the broker SELL fills
    (`broker_orders` + `broker_order_events`). The last FILL's price/time is the realized exit.
    Returns None if no priced sell fill is found (the ground truth then lives only in the broker
    UI / logs — surfaced as unavailable, never faked)."""
    from sqlalchemy import text

    _price_keys = ("avg_price", "average_price", "filled_avg_price", "fill_price", "price")
    with session_factory() as s:
        rows = s.execute(
            text(
                "SELECT e.event_at, e.payload FROM broker_order_events e "
                "JOIN broker_orders bo ON bo.id = e.order_id "
                "JOIN strategies st ON st.id = bo.strategy_id "
                "WHERE st.code='schwab_1m_v2' AND bo.symbol=:sym AND lower(bo.side)='sell' "
                "AND (e.event_at AT TIME ZONE 'America/New_York')::date = :d "
                "ORDER BY e.event_at"
            ),
            {"sym": symbol, "d": session_day_et},
        ).all()
    last: RealExit | None = None
    for event_at, payload in rows:
        if not isinstance(payload, dict):
            continue
        px = next((payload[k] for k in _price_keys if payload.get(k) not in (None, "", 0, "0")), None)
        try:
            px_f = float(px)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if px_f > 0:
            last = RealExit(symbol, px_f, event_at.astimezone(EASTERN))
    return last


def reconcile_day(
    source, session_day_et: str, settings: Settings, real: list[RealEntry], *, session_factory=None
) -> str:
    """Replay every real-entry symbol for the day and reconcile the replayed **full trade**
    (entry -> exit -> ret/reason) vs the real fills. Returns a human-readable report. Honest about
    feed coverage and about any real-exit the broker tables can't price."""
    lines: list[str] = []
    lines.append(f"=== BACKTEST REPLAY — FULL-TRADE PARITY — {session_day_et} ===")
    lines.append(f"real v2 entries: {len(real)}")
    for r in real:
        res = replay_symbol_day(source, r.symbol, session_day_et, settings)
        real_exit = (
            fetch_real_v2_exit(session_factory, r.symbol, session_day_et)
            if session_factory is not None else None
        )
        lines.append("")
        lines.append(
            f"[{r.symbol}] REAL: {r.entry_path} @ {r.entry_price:.4f} "
            f"{r.entry_time_et:%H:%M:%S} ET | Schwab feed: {res.n_bars} bars, {res.n_quotes} quotes"
        )
        if real_exit is not None:
            rret = (real_exit.exit_price - r.entry_price) / r.entry_price * 100.0
            lines.append(
                f"           REAL exit @ {real_exit.exit_price:.4f} "
                f"{real_exit.exit_time_et:%H:%M:%S} ET | real ret {rret:+.2f}%"
            )
        else:
            lines.append("           REAL exit: (unavailable from broker tables — compare vs logs)")
        for sk in res.skips:
            lines.append(f"    SKIP  {sk.reason}: {sk.detail}")
        for m in res.misses:
            lines.append(f"    MISS  {m.reason}: {m.detail}")
        if not res.entries:
            lines.append("    REPLAY: (no entry)")
        for e in res.entries:
            dp = (e.fill_price - r.entry_price) / r.entry_price * 100.0
            lines.append(
                f"    REPLAY entry {e.mode}/{e.order_type} @ {e.fill_price:.4f} "
                f"fill {e.fill_ts.astimezone(EASTERN):%H:%M:%S} ET (ref {e.entry_ref:.4f}) "
                f"| Δ vs real entry {dp:+.2f}%"
            )
        for t in res.trades:
            gap = ""
            if real_exit is not None:
                gap = f" | Δ exit_px vs real {(t.exit_px - real_exit.exit_price):+.4f}"
            lines.append(
                f"    REPLAY exit  [{t.geometry}] @ {t.exit_px:.4f} "
                f"{t.exit_ts.astimezone(EASTERN):%H:%M:%S} ET reason={t.exit_reason} "
                f"| replay ret {t.ret_pct:+.2f}%{gap}"
            )
    return "\n".join(lines)


def main() -> None:  # pragma: no cover - CLI wrapper (exercised via the VPS reconciliation)
    import argparse

    from project_mai_tai.backtest.data import DbMarketDataSource
    from project_mai_tai.db.session import build_session_factory
    from project_mai_tai.settings import get_settings

    ap = argparse.ArgumentParser(description="Backtest REPLAY — P2 full-trade parity reconciliation")
    ap.add_argument("date", help="session day, ET, YYYY-MM-DD")
    ap.add_argument("symbols", nargs="*", help="optional symbol filter (default: all real v2 entries)")
    args = ap.parse_args()

    sf = build_session_factory(get_settings())
    settings = build_replay_settings(base=get_settings())
    source = DbMarketDataSource(sf)
    real = fetch_real_v2_entries(sf, args.date)
    if args.symbols:
        keep = {s.upper() for s in args.symbols}
        real = [r for r in real if r.symbol.upper() in keep]
    print(reconcile_day(source, args.date, settings, real, session_factory=sf))


if __name__ == "__main__":  # pragma: no cover
    main()
