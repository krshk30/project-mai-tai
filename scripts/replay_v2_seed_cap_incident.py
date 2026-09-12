"""Replay a v2 seed-cap entry incident against its real production tape.

The ordinary v2 backtest is intentionally not used here: it applies a simplified replay cap and
models one entry per symbol. This harness reproduces the live service boundary instead:

1. Feed the real ``strategy_bar_history`` rows available before the symbol's watch-start while the
   strategy clock stays fixed at that watch-start, matching DB seed replay.
2. Invoke ``SchwabV2BotService._cap_reconstructed_segment`` itself.
3. Feed the remaining real bars as live bars with fresh, empty position-book evidence.
4. Compare current behavior with the exact pre-fix idle-SELL behavior.

After a fixture has been captured, the review command is deliberately only symbol plus date:

    PYTHONPATH=src .venv/bin/python scripts/replay_v2_seed_cap_incident.py TNON 2026-09-11

On the production box, ``--capture`` reads the running v2 process environment, the read-only bar
table, scanner history, and the v2 log, then writes a secret-free fixture for CI. It never writes
to the production database and never calls a broker.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar
from project_mai_tai.services.schwab_1m_v2_bot import DB_SEED_BAR_LIMIT, SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import SymbolState
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "backtest" / "fixtures"
V2_LOG_GLOB = "/var/log/project-mai-tai/schwab-1m-v2.log*"
V2_UNIT = "project-mai-tai-schwab-1m-v2.service"
BAR_CLOSE_OFFSET_MS = 60_000
EASTERN = ZoneInfo("America/New_York")

REQUIRED_LIVE_SETTINGS: dict[str, bool] = {
    "strategy_schwab_1m_v2_flip_owned_first_entry_enabled": True,
    "strategy_schwab_1m_v2_confirmation_account_neutral_discovery_enabled": True,
    "strategy_schwab_1m_v2_cw_v2_reclaim_enabled": False,
    "strategy_schwab_1m_v2_hold_confirm_enabled": False,
    "strategy_schwab_1m_v2_cw_v2_enabled": True,
    "strategy_schwab_1m_v2_cw_v2_resting_entry_enabled": True,
    "strategy_schwab_1m_v2_cw_armed_segment_safety_enabled": True,
    "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
}


@dataclass(frozen=True)
class TapeBar:
    timestamp_ms: int
    open: str
    high: str
    low: str
    close: str
    volume: int
    source: str

    def as_chart_bar(self, symbol: str) -> ChartBar:
        return ChartBar(
            symbol=symbol,
            open=float(self.open),
            high=float(self.high),
            low=float(self.low),
            close=float(self.close),
            volume=self.volume,
            timestamp_ms=self.timestamp_ms,
        )


@dataclass(frozen=True)
class IncidentTape:
    schema_version: int
    symbol: str
    session_date_et: str
    cap_at_ms: int
    cap_stage: str
    settings_source: str
    settings: dict[str, Any]
    bar_source: str
    bars: tuple[TapeBar, ...]
    expected_sell_bar_ms: int
    expected_decision_minute_et: str
    live_seed_cap_minute_et: str
    live_suppression_minute_et: str


@dataclass(frozen=True)
class ReplayOutcome:
    mode: str
    seed_cap_applied: bool
    seed_arm_bar_ms: int
    sell_bar_ms: int
    decision_bar_ms: int
    decision_event_ms: int
    action: str
    level: float
    marker: str


class _MessageHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _PreFixStrategyMixin:
    """Exact #933 regression: an ownerless idle SELL did not release the seed-cap latches."""

    def _end_flip_owner_on_sell(self, state: SymbolState) -> None:
        if not self._flip_owned_first_entry_enabled:
            return
        if state.flip_owner_phase == "idle":
            return
        super()._end_flip_owner_on_sell(state)


def _replay_strategy_class(*, pre_fix: bool):
    from project_mai_tai.backtest.replay import ReplayStrategy

    if not pre_fix:
        return ReplayStrategy

    class PreFixReplayStrategy(_PreFixStrategyMixin, ReplayStrategy):
        pass

    return PreFixReplayStrategy


def fixture_path(symbol: str, session_date_et: str) -> Path:
    compact_day = session_date_et.replace("-", "")
    return FIXTURE_DIR / f"{symbol.upper()}_{compact_day}_v2_seed_cap.json"


def load_tape(path: Path) -> IncidentTape:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["bars"] = tuple(TapeBar(**row) for row in payload["bars"])
    return IncidentTape(**payload)


def write_tape(path: Path, tape: IncidentTape) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(tape), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _assert_production_settings(settings: dict[str, Any]) -> None:
    wrong = {
        key: (expected, settings.get(key))
        for key, expected in REQUIRED_LIVE_SETTINGS.items()
        if settings.get(key) is not expected
    }
    if wrong:
        detail = ", ".join(
            f"{key}=expected:{expected}/actual:{actual}"
            for key, (expected, actual) in sorted(wrong.items())
        )
        raise AssertionError(f"incident replay is not using the production regime: {detail}")


def _new_strategy(tape: IncidentTape, *, pre_fix: bool):
    strategy_cls = _replay_strategy_class(pre_fix=pre_fix)
    strategy = strategy_cls(Settings(_env_file=None, **tape.settings))
    strategy.configure_fanout_identity_persistence(lambda *_args: None)
    strategy.configure_flip_entry_ownership(lambda *_args: None, restore_readable=True)
    strategy._entries_held = False
    return strategy


def replay_tape(tape: IncidentTape, *, pre_fix: bool) -> ReplayOutcome:
    _assert_production_settings(tape.settings)
    strategy = _new_strategy(tape, pre_fix=pre_fix)
    symbol = tape.symbol.upper()
    state = strategy.watchlist_state(symbol)
    strategy.set_clock_ms(tape.cap_at_ms)

    strategy_logger = logging.getLogger("project_mai_tai.strategy_core.schwab_1m_v2")
    service_logger = logging.getLogger("project_mai_tai.services.schwab_1m_v2_bot")
    handler = _MessageHandler()
    old_levels = (strategy_logger.level, service_logger.level)
    strategy_logger.setLevel(logging.INFO)
    service_logger.setLevel(logging.INFO)
    strategy_logger.addHandler(handler)
    service_logger.addHandler(handler)
    try:
        seed_bars = [
            bar
            for bar in tape.bars
            if bar.timestamp_ms + BAR_CLOSE_OFFSET_MS <= tape.cap_at_ms
        ]
        live_bars = [
            bar
            for bar in tape.bars
            if bar.timestamp_ms + BAR_CLOSE_OFFSET_MS > tape.cap_at_ms
        ]
        if not seed_bars or not live_bars:
            raise AssertionError("tape must contain bars on both sides of the seed cap")

        for row in seed_bars:
            strategy.on_observed_bar(
                symbol,
                row.as_chart_bar(symbol),
                observation_phase="replay",
            )

        if strategy.drain_pending_intents():
            raise AssertionError("seed replay queued an entry before the cap; incident shape changed")
        if not state.cw_armed or state.cw_arm_bar_ts <= 0:
            raise AssertionError("real seed bars did not reconstruct the armed segment")
        seed_arm_bar_ms = int(state.cw_arm_bar_ts)

        bot = object.__new__(SchwabV2BotService)
        bot.strategy = strategy
        bot._watch_start_ms = {symbol: tape.cap_at_ms}
        bot._cap_reconstructed_segment(symbol, stage=tape.cap_stage)
        seed_cap_applied = bool(state.cw_resting_taken and state.cw_reclaim_taken)
        if not seed_cap_applied:
            raise AssertionError("real seed bars did not reproduce the two-latch seed cap")

        latest_sell_bar_ms = 0
        decision_bar_ms = 0
        decision_event_ms = 0
        action = "none"
        level = 0.0
        marker = ""
        seen_messages = len(handler.messages)

        for row in live_bars:
            event_ms = row.timestamp_ms + BAR_CLOSE_OFFSET_MS
            strategy.set_clock_ms(event_ms)
            strategy.apply_flip_position_book(
                FlipPositionBook(
                    observed_at_ms=event_ms,
                    readable=True,
                    legs_by_symbol={},
                )
            )
            prior_atr_state = state.atr_state
            was_resting = state.resting_active
            strategy.on_observed_bar(
                symbol,
                row.as_chart_bar(symbol),
                observation_phase="live",
            )
            if prior_atr_state == "long" and state.atr_state == "short":
                latest_sell_bar_ms = row.timestamp_ms

            new_messages = handler.messages[seen_messages:]
            seen_messages = len(handler.messages)
            suppressed = next(
                (
                    message
                    for message in new_messages
                    if f"[V2-RESTING-SLOT-CONSUMED] {symbol} " in message
                    and "reason=first_slot_already_consumed" in message
                ),
                "",
            )
            placed = not was_resting and state.resting_active
            strategy.drain_pending_intents()
            strategy.drain_webull_direct_intents()

            if latest_sell_bar_ms and (suppressed or placed):
                decision_bar_ms = row.timestamp_ms
                decision_event_ms = event_ms
                action = "suppressed" if suppressed else "placed"
                level = float(state.atr_trail or state.resting_level or 0.0)
                marker = suppressed or next(
                    (
                        message
                        for message in new_messages
                        if "[V2-RESTING-" in message and f"] {symbol} " in message
                    ),
                    "resting state changed from inactive to active",
                )
                break
    finally:
        strategy_logger.removeHandler(handler)
        service_logger.removeHandler(handler)
        strategy_logger.setLevel(old_levels[0])
        service_logger.setLevel(old_levels[1])

    return ReplayOutcome(
        mode="unfixed" if pre_fix else "fixed",
        seed_cap_applied=seed_cap_applied,
        seed_arm_bar_ms=seed_arm_bar_ms,
        sell_bar_ms=latest_sell_bar_ms,
        decision_bar_ms=decision_bar_ms,
        decision_event_ms=decision_event_ms,
        action=action,
        level=level,
        marker=marker,
    )


def prove_tape(tape: IncidentTape) -> tuple[ReplayOutcome, ReplayOutcome]:
    unfixed = replay_tape(tape, pre_fix=True)
    fixed = replay_tape(tape, pre_fix=False)
    decision_minute = _et_minute(fixed.decision_event_ms)
    assert unfixed.seed_cap_applied and fixed.seed_cap_applied
    assert unfixed.action == "suppressed", unfixed
    assert fixed.action == "placed", fixed
    assert unfixed.decision_bar_ms == fixed.decision_bar_ms
    assert unfixed.sell_bar_ms == fixed.sell_bar_ms == tape.expected_sell_bar_ms
    assert decision_minute == tape.expected_decision_minute_et
    assert _et_minute(unfixed.decision_event_ms) == tape.live_suppression_minute_et
    return unfixed, fixed


def _et_minute(timestamp_ms: int) -> str:
    if timestamp_ms <= 0:
        return "none"
    return datetime.fromtimestamp(timestamp_ms / 1000.0, UTC).astimezone(EASTERN).strftime("%H:%M")


def _running_process_settings(unit: str = V2_UNIT) -> tuple[Settings, int]:
    pid_result = subprocess.run(
        ["systemctl", "show", unit, "-p", "MainPID", "--value"],
        check=True,
        capture_output=True,
        text=True,
    )
    pid = int(pid_result.stdout.strip())
    if pid <= 0:
        raise RuntimeError(f"{unit} has no running MainPID")
    environ_path = Path(f"/proc/{pid}/environ")
    if os.geteuid() == 0:
        raw = environ_path.read_bytes()
    else:
        raw = subprocess.run(
            ["sudo", "-n", "cat", str(environ_path)],
            check=True,
            capture_output=True,
        ).stdout
    process_env = {
        key.decode(): value.decode()
        for item in raw.split(b"\0")
        if item and b"=" in item
        for key, value in (item.split(b"=", 1),)
    }
    kwargs = {
        key.removeprefix("MAI_TAI_").lower(): value
        for key, value in process_env.items()
        if key.startswith("MAI_TAI_")
    }
    return Settings(_env_file=None, **kwargs), pid


def _read_log_lines(glob_pattern: str) -> list[str]:
    import glob

    lines: list[str] = []
    for raw_path in sorted(glob.glob(glob_pattern)):
        path = Path(raw_path)
        try:
            if path.suffix == ".gz":
                with gzip.open(path, "rt", encoding="utf-8", errors="replace") as fh:
                    lines.extend(fh)
            else:
                lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines())
        except PermissionError as exc:
            raise RuntimeError(f"cannot read {path}; run the capture command with sudo") from exc
    return lines


def _log_datetime_et(line: str) -> datetime:
    match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[,.]\d+)?", line)
    if not match:
        raise ValueError(f"log line has no UTC timestamp: {line[:120]}")
    observed = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return observed.astimezone(EASTERN)


def _log_minute_et(line: str) -> str:
    return _log_datetime_et(line).strftime("%H:%M")


def _live_markers(symbol: str, session_date_et: str, log_glob: str) -> tuple[list[tuple[int, str]], str]:
    caps: list[tuple[int, str]] = []
    suppressions: list[str] = []
    cap_pattern = re.compile(
        rf"\[V2-CW-SEED-CAP\]\s+{re.escape(symbol)}\b.*watch_start=(\d+).*stage=([^\s)]+)"
    )
    suppression_token = f"[V2-RESTING-SLOT-CONSUMED] {symbol} "
    for line in _read_log_lines(log_glob):
        cap = cap_pattern.search(line)
        if cap:
            watch_start = int(cap.group(1))
            cap_day = datetime.fromtimestamp(watch_start / 1000.0, UTC).astimezone(EASTERN)
            if cap_day.strftime("%Y-%m-%d") == session_date_et:
                caps.append((watch_start, cap.group(2)))
        if suppression_token in line and "reason=first_slot_already_consumed" in line:
            try:
                observed_et = _log_datetime_et(line)
                if observed_et.strftime("%Y-%m-%d") == session_date_et:
                    suppressions.append(observed_et.strftime("%H:%M"))
            except ValueError:
                continue
    if not caps:
        raise RuntimeError(f"no live seed-cap marker for {symbol} on {session_date_et}")
    if not suppressions:
        raise RuntimeError(f"no live first-slot suppression marker for {symbol} on {session_date_et}")
    return sorted(set(caps)), suppressions[0]


def _query_bars(
    settings: Settings,
    symbol: str,
    session_date_et: str,
    cap_at_ms: int,
) -> tuple[TapeBar, ...]:
    day = datetime.strptime(session_date_et, "%Y-%m-%d").replace(tzinfo=EASTERN)
    end = day.replace(hour=16, minute=0, second=0, microsecond=0)
    cap_at = datetime.fromtimestamp(cap_at_ms / 1000.0, UTC)
    seed_cutoff = datetime.fromtimestamp((cap_at_ms - BAR_CLOSE_OFFSET_MS) / 1000.0, UTC)
    session_factory = build_session_factory(settings)
    with session_factory() as session:
        # `_seed_strategy_bars_from_db` reads the last 250 persisted bars without a day boundary.
        # The 06:51 TNON cap therefore used the prior session: today's first stored bar was 07:00.
        # Reconstruct that exact population instead of turning a calendar-day slice into a fake
        # seed. The post-cap query then supplies the real live bars that reached the decision.
        seed_rows = session.execute(
            text(
                "SELECT CAST(extract(epoch from bar_time)*1000 AS bigint), "
                "open_price::text, high_price::text, low_price::text, close_price::text, "
                "volume, source FROM strategy_bar_history "
                "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=:symbol "
                "AND bar_time<=:seed_cutoff ORDER BY bar_time DESC LIMIT :seed_limit"
            ),
            {
                "symbol": symbol,
                "seed_cutoff": seed_cutoff,
                "seed_limit": DB_SEED_BAR_LIMIT,
            },
        ).all()
        live_rows = session.execute(
            text(
                "SELECT CAST(extract(epoch from bar_time)*1000 AS bigint), "
                "open_price::text, high_price::text, low_price::text, close_price::text, "
                "volume, source FROM strategy_bar_history "
                "WHERE strategy_code='schwab_1m_v2' AND interval_secs=60 AND symbol=:symbol "
                "AND bar_time>=:cap_at AND bar_time<:end ORDER BY bar_time"
            ),
            {"symbol": symbol, "cap_at": cap_at, "end": end},
        ).all()
    rows = sorted((*seed_rows, *live_rows), key=lambda row: int(row[0]))
    return tuple(
        TapeBar(
            timestamp_ms=int(ts),
            open=str(open_price),
            high=str(high_price),
            low=str(low_price),
            close=str(close_price),
            volume=int(volume or 0),
            source=str(source),
        )
        for ts, open_price, high_price, low_price, close_price, volume, source in rows
    )


def capture_tape(
    symbol: str,
    session_date_et: str,
    *,
    log_glob: str = V2_LOG_GLOB,
) -> IncidentTape:
    symbol = symbol.upper()
    settings, pid = _running_process_settings()
    resolved_settings = {
        key: value
        for key, value in settings.model_dump(mode="json").items()
        if key.startswith("strategy_schwab_1m_v2_")
    }
    _assert_production_settings(resolved_settings)
    caps, live_suppression_minute = _live_markers(symbol, session_date_et, log_glob)

    last_error: BaseException | None = None
    # A symbol can churn through the watchlist and be capped more than once before entry hours.
    # The incident is owned by the last cap before the live suppression, not an older equivalent
    # seed population that happened to reproduce it. Evaluate newest first and record that cap.
    for cap_at_ms, cap_stage in reversed(caps):
        bars = _query_bars(settings, symbol, session_date_et, cap_at_ms)
        if not bars:
            last_error = RuntimeError(
                f"strategy_bar_history has no {symbol} rows around cap {cap_at_ms}"
            )
            continue
        provisional = IncidentTape(
            schema_version=1,
            symbol=symbol,
            session_date_et=session_date_et,
            cap_at_ms=cap_at_ms,
            cap_stage=cap_stage,
            settings_source=f"/proc/{pid}/environ",
            settings=resolved_settings,
            bar_source="strategy_bar_history:schwab_1m_v2:60",
            bars=bars,
            expected_sell_bar_ms=0,
            expected_decision_minute_et=live_suppression_minute,
            live_seed_cap_minute_et=_et_minute(cap_at_ms),
            live_suppression_minute_et=live_suppression_minute,
        )
        try:
            unfixed = replay_tape(provisional, pre_fix=True)
            fixed = replay_tape(provisional, pre_fix=False)
            if (
                unfixed.action == "suppressed"
                and fixed.action == "placed"
                and unfixed.decision_bar_ms == fixed.decision_bar_ms
                and _et_minute(fixed.decision_event_ms) == live_suppression_minute
            ):
                return replace(provisional, expected_sell_bar_ms=fixed.sell_bar_ms)
        except (AssertionError, RuntimeError) as exc:
            last_error = exc
    raise AssertionError(
        f"no live seed-cap candidate reproduced {symbol} {session_date_et}; last={last_error}"
    )


def _print_outcome(outcome: ReplayOutcome) -> None:
    print(
        f"{outcome.mode.upper():7} cap={int(outcome.seed_cap_applied)} "
        f"sell_bar={_et_minute(outcome.sell_bar_ms + BAR_CLOSE_OFFSET_MS)}ET "
        f"decision={_et_minute(outcome.decision_event_ms)}ET action={outcome.action} "
        f"level={outcome.level:.4f}"
    )
    print(f"         {outcome.marker}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol")
    parser.add_argument("date", help="ET session date, YYYY-MM-DD")
    parser.add_argument(
        "--capture",
        action="store_true",
        help="read the running v2 process, production DB and logs, then write the golden fixture",
    )
    parser.add_argument("--fixture", type=Path, help="fixture path (default derives from symbol/date)")
    parser.add_argument("--log-glob", default=V2_LOG_GLOB)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    symbol = args.symbol.upper()
    path = args.fixture or fixture_path(symbol, args.date)
    if args.capture:
        tape = capture_tape(symbol, args.date, log_glob=args.log_glob)
        write_tape(path, tape)
        print(f"CAPTURED {len(tape.bars)} real bars -> {path}")
    else:
        tape = load_tape(path)
    if tape.symbol != symbol or tape.session_date_et != args.date:
        raise AssertionError("fixture identity does not match the requested symbol/date")
    unfixed, fixed = prove_tape(tape)
    print(
        f"SOURCE  {tape.bar_source} bars={len(tape.bars)} "
        f"settings={tape.settings_source}"
    )
    _print_outcome(unfixed)
    _print_outcome(fixed)
    print("PASS: the real tape suppresses before the fix and places on the same bar after it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
