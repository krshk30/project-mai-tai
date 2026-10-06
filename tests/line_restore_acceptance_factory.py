"""Serial, offline acceptance adapter around the real strategy and bot service.

Only wall time and external delivery/persistence are replaced. Coverage is
supplied by the caller; this adapter never certifies a history from bar count.
Do not run multiple cases concurrently: the historical clock is module-scoped.
"""

from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import patch

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


class RecordingEmitter:
    def __init__(self):
        self.intents = []
        self.atr_sell_observations = []
        self.confirmation_exits = []

    async def emit(self, draft):
        self.intents.append(deepcopy(draft))

    async def emit_atr_sell_observation(self, observation):
        self.atr_sell_observations.append(deepcopy(observation))

    async def emit_confirmation_exit(self, evaluation):
        self.confirmation_exits.append(deepcopy(evaluation))


class LineRestoreCase:
    """Feed recorded events without starting a live service or opening a connection."""

    def __init__(self, *, symbol, now_ms, settings_overrides=None):
        self.symbol = symbol.upper()
        self.now_ms = int(now_ms)
        options = {
            "strategy_schwab_1m_v2_line_chart_restoration_enabled": True,
            "strategy_schwab_1m_v2_atr_flip_enabled": True,
            "strategy_schwab_1m_v2_confirmed_window_enabled": True,
            "strategy_schwab_1m_v2_cw_v2_enabled": True,
            "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
            "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
            "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
            "strategy_schwab_1m_v2_webull_resting_mirror_enabled": True,
        }
        overrides = dict(settings_overrides or {})
        unknown = set(overrides) - set(Settings.model_fields)
        if unknown:
            raise ValueError(f"unknown acceptance settings: {sorted(unknown)}")
        if overrides.get("strategy_schwab_1m_v2_line_chart_restoration_enabled") is False:
            raise ValueError("acceptance factory requires Restoration ON")
        options.update(overrides)
        self.settings = Settings(**options)
        with self.clock():
            self.bot = SchwabV2BotService(self.settings)
        self.strategy = self.bot.strategy
        self.strategy._now_ms = lambda: self.now_ms
        self.schwab = RecordingEmitter()
        self.webull = RecordingEmitter()
        self.bot.intent_emitter = self.schwab
        self.bot.webull_intent_emitter = self.webull
        self.confirmation_decisions = {}
        self.confirmation_published = set()
        # Replace the durable outbox boundary, not exit evaluation/publication.
        self.bot._record_confirmation_evaluation = self._record_confirmation
        self.bot._mark_confirmation_published = self.confirmation_published.add
        self.add()

    def _record_confirmation(self, evaluation):
        key = evaluation.entry.fill_id
        created = key not in self.confirmation_decisions
        if created:
            self.confirmation_decisions[key] = deepcopy(evaluation)
        return created, key not in self.confirmation_published

    @contextmanager
    def clock(self, now_ms=None):
        if now_ms is not None:
            self.now_ms = int(now_ms)
        case = self

        class RecordedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = datetime.fromtimestamp(case.now_ms / 1000, UTC)
                return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

        with patch("project_mai_tai.services.schwab_1m_v2_bot.datetime", RecordedDateTime), patch(
            "project_mai_tai.strategy_core.schwab_1m_v2.datetime", RecordedDateTime,
        ):
            yield

    @staticmethod
    def bar(row):
        if isinstance(row, ChartBar):
            return row
        stamp = row["bar_time"]
        if isinstance(stamp, str):
            stamp = datetime.fromisoformat(stamp)
        if stamp.tzinfo is None:
            raise ValueError("recorded bar_time must include a timezone")
        return ChartBar(
            row["symbol"], *(float(row[key]) for key in (
                "open_price", "high_price", "low_price", "close_price",
            )), int(row["volume"]), int(stamp.timestamp() * 1000),
        )

    def add(self, *, now_ms=None):
        with self.clock(now_ms):
            self.bot._watchlist.add(self.symbol)
            self.bot._sync_line_epochs()

    def remove(self, *, now_ms=None):
        with self.clock(now_ms):
            self.strategy.release_and_drop_symbol(self.symbol)
            self.bot._watchlist.discard(self.symbol)
            self.bot._sync_line_epochs()

    def hold(self, *, detected_at_ms, last_bar_age_s, last_print_age_s):
        with self.clock(detected_at_ms):
            return self.strategy.begin_gap_hold(
                self.symbol, detected_at_ms=detected_at_ms,
                last_bar_age_s=last_bar_age_s, last_print_age_s=last_print_age_s,
            )

    async def feed_bar(self, row, *, now_ms=None, phase="replay", source="callback"):
        if phase not in {"live", "replay"} or source not in {"callback", "rest", "streamer"}:
            raise ValueError("invalid acceptance event source or phase")
        bar = self.bar(row)
        if bar.symbol.upper() != self.symbol:
            raise ValueError("foreign symbol in acceptance case")
        if bar.timestamp_ms + 60_000 > (self.now_ms if now_ms is None else now_ms):
            raise ValueError("acceptance bar is not closed at the recorded event time")
        with self.clock(now_ms):
            if source == "rest":
                await self.bot._handle_bar_from_rest(self.symbol, bar)
            elif source == "streamer":
                await self.bot._handle_bar_from_streamer(self.symbol, bar)
            else:
                await self.bot._handle_bar(self.symbol, bar, observation_phase=phase)

    def attest(self, proof):
        self.bot._line_sessions[self.symbol].attest(proof)

    async def rebuild(self, *, now_ms=None):
        with self.clock(now_ms):
            return await self.bot._rebuild_session_line(
                self.symbol, self.bot._line_sessions[self.symbol],
            )

    def snapshot(self):
        state = self.strategy.watchlist_state(self.symbol)
        ledger = self.bot._line_sessions.get(self.symbol)
        return {
            "symbol": self.symbol, "now_ms": self.now_ms,
            "state": state.atr_state, "trail": state.atr_trail,
            "age": state.atr_state_age, "entry_allowed": self.strategy.line_buy_ready(self.symbol),
            "incomplete_reason": ledger.incomplete_reason if ledger else "not_subscribed",
            "epoch": ledger.epoch if ledger else None,
            "buy_count": sum(d.intent_type == "open" and d.side == "buy"
                             for sink in (self.schwab, self.webull) for d in sink.intents),
            "intents": deepcopy(self.schwab.intents + self.webull.intents),
            "atr_sell_observations": deepcopy(self.schwab.atr_sell_observations),
            "confirmation_decisions": deepcopy(list(self.confirmation_decisions.values())),
            "confirmation_exits": deepcopy(self.schwab.confirmation_exits),
        }


def make_line_restore_case(*, symbol, now_ms, settings_overrides=None):
    """Stable entry point for the reviewer-owned scripts/line_restore_acceptance.py."""
    return LineRestoreCase(symbol=symbol, now_ms=now_ms, settings_overrides=settings_overrides)
