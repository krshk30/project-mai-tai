#!/usr/bin/env python3
"""Watch known production defects without confusing a working guard with recurrence.

Every catalog row states both polarities. A benign guard marker is evidence that the guard ran;
it is never itself a recurrence. Rows without a durable recurrence discriminator stay UNARMED.

The watch reuses ``unexercised_watch.py`` for the ntfy endpoint, delivery result, state loading,
and atomic state writes. There is deliberately no second notification implementation here.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
LOG_DIR = Path("/var/log/project-mai-tai")
DEFAULT_STATE = Path("/home/trader/known_defect_regression_watch/state.json")
DEFAULT_STATUS = Path("/home/trader/known_defect_regression_watch/STATUS.txt")
WATCH_META_KEY = "__watch__"
BOOT_RELEASE_GRACE_SECONDS = 300
SESSION_ROLL_GRACE_MINUTES = 10
WEBULL_429_BURST_COUNT = 10
WEBULL_429_BURST_SECONDS = 60
FRESH_SELL_MAX_BAR_AGE_SECONDS = 180
LIVE_ACCOUNTS = ("live:schwab_1m_v2", "live:orb")

RECURRENCE = "RECURRENCE"
GUARD_WORKING = "GUARD_WORKING"
OBSERVED_CLEAN = "OBSERVED_CLEAN"
UNEXERCISED = "UNEXERCISED"
COULD_NOT_TELL = "COULD_NOT_TELL"
UNARMED = "UNARMED"
DELEGATED = "DELEGATED"


def _load_sibling(name: str):
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(f"known_defect_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# This is the delivery path already proven by the unexercised-condition watcher. Keep the module
# object visible so tests can prove we did not quietly introduce another endpoint or curl wrapper.
delivery = _load_sibling("unexercised_watch")


@dataclass(frozen=True)
class RegressionSpec:
    key: str
    title: str
    mode: str
    evidence: str
    guard_working_shape: str
    recurrence_shape: str
    delegated_to: str = ""


@dataclass(frozen=True)
class TimedLine:
    at: datetime
    text: str


@dataclass(frozen=True)
class Reading:
    key: str
    verdict: str
    evaluated: int
    guard_working: int
    recurrence: int
    recurred: bool
    detail: str


@dataclass
class _SellEpisode:
    symbol: str
    at: datetime
    filled: bool = False
    recurred: bool = False
    consumed_after_fill: bool = False


CATALOG: tuple[RegressionSpec, ...] = (
    RegressionSpec(
        "BOOT1",
        "boot hold releases before restoration or never releases after it",
        "ARMED",
        "V2-BOOT-RESTORE and V2-BOOT-HOLD lines in timestamp order",
        "restoration_complete=1 precedes the release within one boot episode",
        "release precedes completion, or completion remains held beyond five minutes",
    ),
    RegressionSpec(
        "ROLL1",
        "the time-driven 04:00 ET v2 roll never runs",
        "ARMED",
        "V2-SESSION-ROLL boundary line for the current market session",
        "boundary_crossed=True is recorded after the session anchor, including rolled=0",
        "no boundary line exists by 04:10 ET on a market day",
    ),
    RegressionSpec(
        "OWNERROLL1",
        "stale entry ownership survives the time-driven session roll",
        "ARMED",
        "V2-SESSION-ROLL rolled symbols followed through the 04:10 ET grace window",
        "every rolled symbol stays free of old-owner UNKNOWN/RECOVERY markers after the boundary",
        "a rolled symbol resumes V2-FLIP-OWNER-UNKNOWN or V2-FLIP-OWNER-RECOVERY before 04:10 ET",
    ),
    RegressionSpec(
        "SLOTCLEAR1",
        "a fresh SELL leaves the reconstructed first-entry slot consumed",
        "ARMED",
        "fleet-wide V2-ATR-PROBE SELL lines joined to owner fills and slot-consumed refusals",
        "a fresh SELL has no refusal, or the refusal follows a real owner fill",
        "first_slot_already_consumed appears after a fresh SELL and before any owner fill",
    ),
    RegressionSpec(
        "LIQPULL1",
        "the resting liquidity guard cancels before three consecutive thin bars",
        "ARMED",
        "liquidity-floor cancels with streak counts plus state-probe streak resets",
        "a cancel carries a streak of at least three, or a good bar resets a shorter streak",
        "a liquidity-floor cancel carries resting_below_floor_bars below three",
    ),
    RegressionSpec(
        "DISARM1",
        "an armed segment clears without a durable disarm",
        "UNARMED",
        "V2-CW-ARM and V2-CW-DISARM markers",
        "every real arm has one cause-specific disarm",
        "UNARMED: a still-live arm and a silently-cleared arm are not distinguishable from logs alone",
    ),
    RegressionSpec(
        "ORPHAN1",
        "a working resting order outlives its strategy latch",
        "DELEGATED",
        "broker order plus watchlist and market-distance evidence",
        "no stale or far-away unowned working order",
        "orphan_order_check.py returns its red orphan shape",
        "ops/health/orphan_order_check.py",
    ),
    RegressionSpec(
        "CAP1",
        "entry-cap composition admits duplicate logical slots",
        "DELEGATED",
        "fill-time assignment inside live ARM-to-DISARM segments",
        "at most one first and one reclaim slot in the legacy population",
        "v2_entry_fix_watch.py reports composition=BREACH",
        "ops/health/v2_entry_fix_watch.py",
    ),
    RegressionSpec(
        "PHANTOM1",
        "an open managed row is backed by a fresh broker zero",
        "ARMED",
        "stable double-read of open managed rows and fresh account_positions",
        "fresh broker quantity is non-zero for each measured open managed row",
        "managed quantity is positive while fresh persisted broker quantity is zero",
    ),
    RegressionSpec(
        "REDIS1",
        "Redis eviction silently loses a one-shot intent",
        "UNARMED",
        "Redis keyspace, producer sequence, and consumer acknowledgement",
        "snapshot streams self-heal on the next full snapshot",
        "UNARMED: one-shot streams have no durable acknowledgement that can prove a missing event",
    ),
    RegressionSpec(
        "BARGAP1",
        "a restart creates an ATR bar hole",
        "DELEGATED",
        "per-symbol persisted bar continuity spanning the restart",
        "zero restart-spanning gaps with a measured watchlist denominator",
        "bar_gap_watch_cron.sh reports a non-halt restart-spanning gap",
        "ops/health/bar_gap_watch_cron.sh and the durable restart checklist",
    ),
    RegressionSpec(
        "SAW1",
        "the rejected-sell sawtooth returns",
        "UNARMED",
        "broker-origin managed-exit rejects plus a durable exit-episode boundary",
        "the episode stops retrying at its configured ceiling",
        "UNARMED: fixed time buckets can merge separate episodes and cannot prove the old sawtooth returned",
    ),
    RegressionSpec(
        "CHURN1",
        "refresh churn cancels a fillable managed exit",
        "DELEGATED",
        "working managed limit, contemporaneous bid, cancellation, and replacement chain",
        "OMS-P0A-HOLD rests a marketable exit through refresh",
        "v2_entry_fix_watch.py reports p0a=FAILURE",
        "ops/health/v2_entry_fix_watch.py",
    ),
    RegressionSpec(
        "RESERVE1",
        "software close sells shares still reserved by Webull protection",
        "ARMED",
        "live managed-exit orders and broker-origin reverse/short-sale rejects",
        "OMS-EXIT-RELEASE or OMS-EXIT-PAIR-RESOLVED confirms the pair outcome first",
        "a managed Webull sell is broker-rejected as reverse/short while reservations remain",
    ),
    RegressionSpec(
        "W4291",
        "Webull position-mirror 429 flood returns",
        "ARMED",
        "BROKER-SYNC-CENSUS denominator plus timestamped Webull 429 backoff lines",
        "an isolated 429 emits backing-off and later reads continue",
        "ten Webull position-sync 429s occur inside any rolling 60-second window",
    ),
    RegressionSpec(
        "CEILING1",
        "exit rejection continues after the absolute ceiling",
        "UNARMED",
        "OMS-V2-EXIT-REJECT-CEILING plus durable per-episode reject identity",
        "the ceiling marker stands the episode down",
        "UNARMED: DB rows do not carry the in-memory episode boundary needed to count post-ceiling rejects",
    ),
    RegressionSpec(
        "FALSEFLAT1",
        "a false-flat decision deletes all ownership and protection",
        "UNARMED",
        "broker truth, recent owned fill, virtual row, managed row, and protection row",
        "inferred flat inside the fill grace keeps protection",
        "UNARMED: after deletion the durable ownership discriminator is gone, so manual shares are ambiguous",
    ),
    RegressionSpec(
        "VPZERO1",
        "virtual_positions reads zero while an owned broker position is held",
        "ARMED",
        "open managed row joined to fresh positive account_positions and virtual_positions",
        "the strategy virtual quantity remains positive for every measured owned holding",
        "fresh broker quantity and managed quantity are positive while virtual quantity is absent or zero",
    ),
    RegressionSpec(
        "SEED1",
        "DB seed can again proceed unguarded across missing sessions",
        "ARMED",
        "V2-DB-SEED-GAP census, refusal, and calendar-lookup-failure lines",
        "dropped ALL/series SKIPS refuses stale history; census supplies seed evaluations",
        "session-calendar lookup failed and the code treats the gap as contiguous",
    ),
)

SPEC_BY_KEY = {spec.key: spec for spec in CATALOG}


_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:[,.](\d{1,6}))?")
_SESSION_ROLL = re.compile(r"\[V2-SESSION-ROLL\].*boundary_crossed=True.*anchor=(\d+)")
_SESSION_ROLL_SYMBOLS = re.compile(
    r"\[V2-SESSION-ROLL\].*boundary_crossed=True rolled=(\d+) symbols=([^ ]+).*anchor=(\d+)"
)
_SEED_CENSUS = re.compile(r"\[V2-DB-SEED-GAP-CENSUS\] truncations=(\d+) of (\d+)")
_BROKER_CENSUS_WEBULL = re.compile(r"live:orb: ok=(\d+) failed=(\d+) consecutive_now=(\d+)")
_ATR_SELL = re.compile(r"\[V2-ATR-PROBE\]\s+sym=([^ ]+)\s+ts_ms=(\d+).*\bflip=SELL\b")
_FLIP_OWNER_FILL = re.compile(r"\[V2-FLIP-OWNER-FILL\]\s+([^ ]+)\b")
_SLOT_CONSUMED = re.compile(
    r"\[V2-RESTING-SLOT-CONSUMED\]\s+([^ ]+).*"
    r"\breason=first_slot_already_consumed\b"
)
_LIQUIDITY_CANCEL_RAW = re.compile(
    r"\[V2-(?:RESTING-CANCEL|RESTING-EH-DISARM)\].*\breason=liquidity_floor\b"
)
_LIQUIDITY_CANCEL = re.compile(
    r"\[V2-(?:RESTING-CANCEL|RESTING-EH-DISARM)\]\s+([^ ]+).*"
    r"\breason=liquidity_floor\s+resting_below_floor_bars=(\d+)\b"
)
_CW_STATE_PROBE = re.compile(
    r"\[V2-CW-STATE-PROBE\]\s+sym=([^ ]+).*"
    r"\bresting_below_floor_bars=(\d+).*\bresting_active=(True|False)\b"
)


def parse_log_lines(lines: Iterable[str], *, since: datetime, until: datetime) -> list[TimedLine]:
    result: list[TimedLine] = []
    for raw in lines:
        match = _LOG_TS.match(raw)
        if match is None:
            continue
        micros = (match.group(2) or "0").ljust(6, "0")
        at = datetime.strptime(f"{match.group(1)}.{micros}", "%Y-%m-%d %H:%M:%S.%f").replace(
            tzinfo=UTC
        )
        if since <= at <= until:
            result.append(TimedLine(at=at, text=raw.rstrip()))
    return sorted(result, key=lambda line: (line.at, line.text))


def _reading(
    key: str,
    *,
    evaluated: int,
    guard_working: int,
    recurrence: int,
    detail: str,
    unknown: bool = False,
) -> Reading:
    if recurrence > 0:
        verdict = RECURRENCE
    elif unknown:
        verdict = COULD_NOT_TELL
    elif evaluated <= 0:
        verdict = UNEXERCISED
    elif guard_working > 0:
        verdict = GUARD_WORKING
    else:
        verdict = OBSERVED_CLEAN
    return Reading(key, verdict, evaluated, guard_working, recurrence, recurrence > 0, detail)


def evaluate_boot(lines: Sequence[TimedLine], *, now: datetime) -> Reading:
    ready_at: datetime | None = None
    evaluated = guard = bad = 0
    details: list[str] = []
    for line in lines:
        if "[V2-BOOT-HOLD] HELD" in line.text and "restoration_complete=0" in line.text:
            ready_at = None
        elif "[V2-BOOT-RESTORE]" in line.text and "restoration_complete=1" in line.text:
            if ready_at is None:
                evaluated += 1
            ready_at = line.at
        elif "[V2-BOOT-HOLD] released" in line.text:
            if ready_at is None:
                bad += 1
                details.append(f"early_release={line.at.isoformat()}")
            else:
                guard += 1
                ready_at = None
    if ready_at is not None and (now - ready_at).total_seconds() > BOOT_RELEASE_GRACE_SECONDS:
        bad += 1
        details.append(f"completion_still_held_since={ready_at.isoformat()}")
    return _reading(
        "BOOT1",
        evaluated=evaluated,
        guard_working=guard,
        recurrence=bad,
        detail=";".join(details) or "completion must precede release; five-minute release bound",
    )


def session_anchor(now: datetime) -> datetime:
    et = now.astimezone(ET)
    anchor = et.replace(hour=4, minute=0, second=0, microsecond=0)
    if et < anchor:
        anchor -= timedelta(days=1)
    return anchor.astimezone(UTC)


def evaluate_session_roll(
    lines: Sequence[TimedLine], *, now: datetime, market_day: bool
) -> Reading:
    anchor = session_anchor(now)
    due = market_day and now >= anchor + timedelta(minutes=SESSION_ROLL_GRACE_MINUTES)
    if not due:
        return _reading(
            "ROLL1",
            evaluated=0,
            guard_working=0,
            recurrence=0,
            detail=f"not due for market session anchor={anchor.isoformat()}",
        )
    anchor_ms = int(anchor.timestamp() * 1000)
    matches = [
        line
        for line in lines
        if (match := _SESSION_ROLL.search(line.text)) and int(match.group(1)) == anchor_ms
    ]
    return _reading(
        "ROLL1",
        evaluated=1,
        guard_working=int(bool(matches)),
        recurrence=int(not matches),
        detail=f"expected_anchor={anchor.isoformat()} boundary_lines={len(matches)}",
    )


def evaluate_owner_roll(lines: Sequence[TimedLine], *, now: datetime, market_day: bool) -> Reading:
    anchor = session_anchor(now)
    due = market_day and now >= anchor + timedelta(minutes=SESSION_ROLL_GRACE_MINUTES)
    if not due:
        return _reading(
            "OWNERROLL1",
            evaluated=0,
            guard_working=0,
            recurrence=0,
            detail=f"not due for market session anchor={anchor.isoformat()}",
        )
    anchor_ms = int(anchor.timestamp() * 1000)
    boundary: tuple[TimedLine, int, tuple[str, ...]] | None = None
    for line in lines:
        match = _SESSION_ROLL_SYMBOLS.search(line.text)
        if match is None or int(match.group(3)) != anchor_ms:
            continue
        rolled = int(match.group(1))
        symbols = tuple(symbol for symbol in match.group(2).split(",") if symbol != "-")
        boundary = (line, rolled, symbols)
    if boundary is None:
        return _reading(
            "OWNERROLL1",
            evaluated=0,
            guard_working=0,
            recurrence=0,
            detail=f"current boundary evidence missing for anchor={anchor.isoformat()}",
            unknown=True,
        )
    boundary_line, rolled, symbols = boundary
    if rolled != len(symbols):
        return _reading(
            "OWNERROLL1",
            evaluated=rolled,
            guard_working=0,
            recurrence=0,
            detail=f"rolled={rolled} but only {len(symbols)} symbol identities were logged",
            unknown=True,
        )
    observed_until = min(now, anchor + timedelta(minutes=SESSION_ROLL_GRACE_MINUTES))
    recurred = {
        symbol
        for symbol in symbols
        if any(
            boundary_line.at < line.at <= observed_until
            and (
                f"[V2-FLIP-OWNER-UNKNOWN] {symbol} " in line.text
                or f"[V2-FLIP-OWNER-RECOVERY] {symbol} " in line.text
            )
            for line in lines
        )
    }
    return _reading(
        "OWNERROLL1",
        evaluated=rolled,
        guard_working=rolled - len(recurred),
        recurrence=len(recurred),
        detail=(
            f"rolled={rolled} stable={rolled - len(recurred)} "
            f"recurred={','.join(sorted(recurred)) or 'none'} "
            f"observed_until={observed_until.isoformat()}"
        ),
    )


def evaluate_fresh_sell_slot_clear(lines: Sequence[TimedLine]) -> Reading:
    episodes: list[_SellEpisode] = []
    current: dict[str, _SellEpisode] = {}
    for line in lines:
        if match := _ATR_SELL.search(line.text):
            bar_at = datetime.fromtimestamp(int(match.group(2)) / 1000, UTC)
            age_seconds = (line.at - bar_at).total_seconds()
            if not 0 <= age_seconds <= FRESH_SELL_MAX_BAR_AGE_SECONDS:
                continue
            episode = _SellEpisode(symbol=match.group(1), at=line.at)
            episodes.append(episode)
            current[episode.symbol] = episode
            continue
        if match := _FLIP_OWNER_FILL.search(line.text):
            if episode := current.get(match.group(1)):
                episode.filled = True
            continue
        if match := _SLOT_CONSUMED.search(line.text):
            if episode := current.get(match.group(1)):
                if episode.filled:
                    episode.consumed_after_fill = True
                else:
                    episode.recurred = True

    recurred = [episode for episode in episodes if episode.recurred]
    consumed_after_fill = sum(episode.consumed_after_fill for episode in episodes)
    recurrence_detail = ",".join(
        f"{episode.symbol}@{episode.at.isoformat()}" for episode in recurred
    )
    return _reading(
        "SLOTCLEAR1",
        evaluated=len(episodes),
        guard_working=len(episodes) - len(recurred),
        recurrence=len(recurred),
        detail=(
            f"fresh_sells={len(episodes)} clean={len(episodes) - len(recurred)} "
            f"consumed_after_fill={consumed_after_fill} "
            f"recurred={recurrence_detail or 'none'}"
        ),
    )


def evaluate_liquidity_pull(lines: Sequence[TimedLine]) -> Reading:
    evaluated = guard = bad = unmeasured = 0
    resets: list[str] = []
    early: list[str] = []
    last_probe: dict[str, tuple[int, bool]] = {}
    for line in lines:
        if match := _CW_STATE_PROBE.search(line.text):
            symbol = match.group(1)
            count = int(match.group(2))
            active = match.group(3) == "True"
            prior = last_probe.get(symbol)
            if prior is not None and prior[1] and active and prior[0] > 0 and count == 0:
                evaluated += 1
                guard += 1
                resets.append(f"{symbol}:{prior[0]}->0")
            last_probe[symbol] = (count, active)

        if not _LIQUIDITY_CANCEL_RAW.search(line.text):
            continue
        match = _LIQUIDITY_CANCEL.search(line.text)
        if match is None:
            unmeasured += 1
            continue
        symbol = match.group(1)
        count = int(match.group(2))
        evaluated += 1
        if count < 3:
            bad += 1
            early.append(f"{symbol}:{count}@{line.at.isoformat()}")
        else:
            guard += 1

    return _reading(
        "LIQPULL1",
        evaluated=evaluated,
        guard_working=guard,
        recurrence=bad,
        detail=(
            f"streak_decisions={evaluated} valid={guard} early={','.join(early) or 'none'} "
            f"good_bar_resets={','.join(resets) or 'none'} unmeasured_cancels={unmeasured}"
        ),
        unknown=unmeasured > 0,
    )


def evaluate_seed(lines: Sequence[TimedLine]) -> Reading:
    latest_denominator = 0
    truncations = 0
    guard = 0
    bad = 0
    for line in lines:
        if match := _SEED_CENSUS.search(line.text):
            truncations, latest_denominator = map(int, match.groups())
        if "[V2-DB-SEED-GAP]" in line.text and (
            "dropped ALL" in line.text or "the series SKIPS" in line.text
        ):
            guard += 1
        if "[V2-DB-SEED-GAP]" in line.text and "session-calendar lookup failed" in line.text:
            bad += 1
    evaluated = max(latest_denominator, guard + bad)
    return _reading(
        "SEED1",
        evaluated=evaluated,
        guard_working=guard,
        recurrence=bad,
        detail=(
            f"latest_census_truncations={truncations} seed_evaluations={latest_denominator}; "
            f"refusals={guard} fail_open={bad}"
        ),
    )


def _rolling_burst(lines: Sequence[TimedLine], *, seconds: int) -> int:
    peak = 0
    left = 0
    for right, line in enumerate(lines):
        while (line.at - lines[left].at).total_seconds() > seconds:
            left += 1
        peak = max(peak, right - left + 1)
    return peak


def evaluate_webull_429(lines: Sequence[TimedLine]) -> Reading:
    reads = 0
    for line in lines:
        if match := _BROKER_CENSUS_WEBULL.search(line.text):
            reads += int(match.group(1)) + int(match.group(2))
    hits = [line for line in lines if "Webull position sync rate-limited (429)" in line.text]
    peak = _rolling_burst(hits, seconds=WEBULL_429_BURST_SECONDS) if hits else 0
    return _reading(
        "W4291",
        evaluated=reads,
        guard_working=len(hits),
        recurrence=int(peak >= WEBULL_429_BURST_COUNT),
        detail=f"position_reads={reads} backoff_markers={len(hits)} peak_60s={peak}",
        unknown=bool(hits and reads == 0),
    )


def confirmed_exit_releases(lines: Sequence[TimedLine], account: str) -> int:
    return sum(
        ("[OMS-EXIT-RELEASE]" in line.text or "[OMS-EXIT-PAIR-RESOLVED]" in line.text)
        and re.search(rf"\]\s+\S+\s+{re.escape(account)}(?:\s|$)", line.text) is not None
        for line in lines
    )


def evaluate_database(
    metrics: Mapping[str, int], *, oms_logs_readable: bool = True
) -> list[Reading]:
    managed_schwab = metrics["managed_exit_orders_schwab"]
    managed_webull = metrics["managed_exit_orders_webull"]
    releases_schwab = metrics["confirmed_exit_releases_schwab"]
    releases_webull = metrics["confirmed_exit_releases_webull"]
    reserve_rejects_webull = metrics["reserved_share_reject_orders_webull"]
    owned_schwab = metrics["owned_open_rows_schwab"]
    owned_webull = metrics["owned_open_rows_webull"]
    fresh_schwab = metrics["owned_fresh_rows_schwab"]
    fresh_webull = metrics["owned_fresh_rows_webull"]
    virtual_zero_schwab = metrics["virtual_zero_held_rows_schwab"]
    virtual_zero_webull = metrics["virtual_zero_held_rows_webull"]
    managed = managed_schwab + managed_webull
    releases = releases_schwab + releases_webull
    owned = owned_schwab + owned_webull
    fresh = fresh_schwab + fresh_webull
    virtual_zero = virtual_zero_schwab + virtual_zero_webull
    return [
        _reading(
            "RESERVE1",
            evaluated=managed,
            guard_working=releases,
            recurrence=reserve_rejects_webull,
            detail=(
                f"schwab_managed_exits={managed_schwab} releases={releases_schwab}; "
                f"webull_managed_exits={managed_webull} releases={releases_webull} "
                f"reserved_share_reject_orders={reserve_rejects_webull}"
            ),
            unknown=not oms_logs_readable,
        ),
        _reading(
            "VPZERO1",
            evaluated=owned,
            guard_working=max(0, fresh - virtual_zero),
            recurrence=virtual_zero,
            detail=(
                f"schwab_owned={owned_schwab} fresh={fresh_schwab} "
                f"virtual_zero={virtual_zero_schwab}; webull_owned={owned_webull} "
                f"fresh={fresh_webull} virtual_zero={virtual_zero_webull}"
            ),
            unknown=owned_schwab != fresh_schwab or owned_webull != fresh_webull,
        ),
    ]


def evaluate_phantom(report) -> Reading:
    counts = {
        account: {"evaluated": 0, "backed": 0, "phantoms": 0, "unknown": 0}
        for account in LIVE_ACCOUNTS
    }
    for result in report.results:
        account = str(result.row.account)
        if account not in counts:
            continue
        counts[account]["evaluated"] += 1
        if result.verdict == "BACKED":
            counts[account]["backed"] += 1
        elif result.verdict == "CONFIRMED_PHANTOM":
            counts[account]["phantoms"] += 1
        else:
            counts[account]["unknown"] += 1
    split = "; ".join(
        f"{account} evaluated={values['evaluated']} backed={values['backed']} "
        f"phantoms={values['phantoms']} unknown={values['unknown']}"
        for account, values in counts.items()
    )
    return _reading(
        "PHANTOM1",
        evaluated=len(report.results),
        guard_working=report.backed,
        recurrence=report.phantoms,
        detail=(
            f"open_positive_managed_rows={len(report.results)} backed={report.backed} "
            f"confirmed_phantoms={report.phantoms} unknown={report.unknown}; {split}"
        ),
        unknown=bool(report.unknown or report.population_error),
    )


def live_phantom_population(rows: Iterable[object]) -> list[object]:
    return [row for row in rows if str(getattr(row, "account", "")) in LIVE_ACCOUNTS]


def static_readings() -> list[Reading]:
    rows: list[Reading] = []
    for spec in CATALOG:
        if spec.mode == "UNARMED":
            rows.append(Reading(spec.key, UNARMED, 0, 0, 0, False, spec.recurrence_shape))
        elif spec.mode == "DELEGATED":
            rows.append(Reading(spec.key, DELEGATED, 0, 0, 0, False, spec.delegated_to))
    return rows


def failed_evidence_readings(detail: str) -> list[Reading]:
    rows = static_readings()
    rows.extend(
        Reading(spec.key, COULD_NOT_TELL, 0, 0, 0, False, detail)
        for spec in CATALOG
        if spec.mode == "ARMED"
    )
    return sorted(rows, key=lambda row: list(SPEC_BY_KEY).index(row.key))


def unknown_reading(key: str, detail: str) -> Reading:
    return Reading(key, COULD_NOT_TELL, 0, 0, 0, False, detail)


def validate_readings(readings: Sequence[Reading]) -> list[Reading]:
    """Refuse an incomplete or malformed answer from the collector."""
    rows = list(readings)
    expected = set(SPEC_BY_KEY)
    actual = [row.key for row in rows]
    missing = sorted(expected - set(actual))
    unknown = sorted(set(actual) - expected)
    duplicates = sorted(key for key in set(actual) if actual.count(key) > 1)
    if missing or unknown or duplicates:
        raise RuntimeError(
            "reading population mismatch "
            f"missing={','.join(missing) or '-'} "
            f"unknown={','.join(unknown) or '-'} "
            f"duplicates={','.join(duplicates) or '-'}"
        )

    armed_verdicts = {
        RECURRENCE,
        GUARD_WORKING,
        OBSERVED_CLEAN,
        UNEXERCISED,
        COULD_NOT_TELL,
    }
    for row in rows:
        for field in ("evaluated", "guard_working", "recurrence"):
            value = getattr(row, field)
            if type(value) is not int or value < 0:
                raise RuntimeError(f"{row.key}.{field} must be a nonnegative int")
        if type(row.recurred) is not bool:
            raise RuntimeError(f"{row.key}.recurred must be present and boolean")
        if row.recurred != (row.recurrence > 0):
            raise RuntimeError(f"{row.key}.recurred disagrees with recurrence count")
        if (row.verdict == RECURRENCE) != row.recurred:
            raise RuntimeError(f"{row.key}.verdict disagrees with recurred")

        mode = SPEC_BY_KEY[row.key].mode
        allowed = armed_verdicts if mode == "ARMED" else {mode}
        if row.verdict not in allowed:
            raise RuntimeError(f"{row.key}.verdict {row.verdict!r} is invalid for mode {mode}")
    return sorted(rows, key=lambda row: list(SPEC_BY_KEY).index(row.key))


DATABASE_SQL = r"""
BEGIN READ ONLY;
WITH bounds AS (
    SELECT :'window_since'::timestamptz AS since_at
),
managed_orders AS (
    SELECT DISTINCT bo.id, ba.name AS account, bo.symbol
    FROM broker_orders bo
    JOIN broker_accounts ba ON ba.id = bo.broker_account_id
    LEFT JOIN trade_intents ti ON ti.id = bo.intent_id
    CROSS JOIN bounds b
    WHERE bo.side = 'sell'
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
      AND coalesce(bo.submitted_at, bo.updated_at) >= b.since_at
      AND (
          lower(coalesce(bo.payload->>'oms_v2_managed_exit', '')) = 'true'
          OR coalesce(ti.reason, '') LIKE 'oms_v2_managed_exit:%'
      )
),
managed_rejects AS (
    SELECT e.id, e.order_id, mo.account, mo.symbol, e.event_at,
           upper(regexp_replace(trim(coalesce(e.payload->>'reason', '')), '\s+', ' ', 'g')) AS reason
    FROM broker_order_events e
    JOIN managed_orders mo ON mo.id = e.order_id
    WHERE e.event_type = 'rejected' AND e.event_source = 'broker'
),
owned AS (
    SELECT m.id, ba.name AS account, ap.quantity AS broker_qty, ap.source_updated_at,
           coalesce(vp.quantity, 0) AS virtual_qty
    FROM oms_managed_positions m
    JOIN broker_accounts ba ON ba.name = m.broker_account_name
    JOIN strategies s ON s.code = m.strategy_code
    LEFT JOIN account_positions ap
      ON ap.broker_account_id = ba.id AND ap.symbol = m.symbol
    LEFT JOIN virtual_positions vp
      ON vp.broker_account_id = ba.id AND vp.strategy_id = s.id AND vp.symbol = m.symbol
    WHERE m.status = 'open' AND m.current_quantity > 0
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
),
counts AS (
    SELECT
      (SELECT count(*) FROM managed_orders WHERE account='live:schwab_1m_v2')::int
        AS managed_exit_orders_schwab,
      (SELECT count(*) FROM managed_orders WHERE account='live:orb')::int
        AS managed_exit_orders_webull,
      (SELECT count(DISTINCT order_id) FROM managed_rejects
       WHERE account = 'live:orb' AND (
         reason LIKE '%NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K%'
         OR reason LIKE '%ORDER_NOT_SUPPORT_REVERSE_OPTION%'
       ))::int AS reserved_share_reject_orders_webull,
      (SELECT count(*) FROM owned WHERE account='live:schwab_1m_v2')::int
        AS owned_open_rows_schwab,
      (SELECT count(*) FROM owned WHERE account='live:orb')::int AS owned_open_rows_webull,
      (SELECT count(*) FROM owned
       WHERE account='live:schwab_1m_v2' AND broker_qty > 0
         AND source_updated_at >= now() - interval '300 seconds')::int AS owned_fresh_rows_schwab,
      (SELECT count(*) FROM owned
       WHERE account='live:orb' AND broker_qty > 0
         AND source_updated_at >= now() - interval '300 seconds')::int AS owned_fresh_rows_webull,
      (SELECT count(*) FROM owned
       WHERE account='live:schwab_1m_v2' AND broker_qty > 0
         AND source_updated_at >= now() - interval '300 seconds'
         AND virtual_qty <= 0)::int AS virtual_zero_held_rows_schwab,
      (SELECT count(*) FROM owned
       WHERE account='live:orb' AND broker_qty > 0
         AND source_updated_at >= now() - interval '300 seconds'
         AND virtual_qty <= 0)::int AS virtual_zero_held_rows_webull
)
SELECT json_build_object(
    'managed_exit_orders_schwab', managed_exit_orders_schwab,
    'managed_exit_orders_webull', managed_exit_orders_webull,
    'reserved_share_reject_orders_webull', reserved_share_reject_orders_webull,
    'owned_open_rows_schwab', owned_open_rows_schwab,
    'owned_open_rows_webull', owned_open_rows_webull,
    'owned_fresh_rows_schwab', owned_fresh_rows_schwab,
    'owned_fresh_rows_webull', owned_fresh_rows_webull,
    'virtual_zero_held_rows_schwab', virtual_zero_held_rows_schwab,
    'virtual_zero_held_rows_webull', virtual_zero_held_rows_webull
)::text FROM counts;
ROLLBACK;
"""


def _query_database(since: datetime) -> dict[str, int]:
    command = [
        "sudo",
        "-n",
        "-u",
        "postgres",
        "psql",
        "-X",
        "-qAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"window_since={since.isoformat()}",
        "-d",
        "project_mai_tai",
        "-f",
        "-",
    ]
    result = subprocess.run(
        command, input=DATABASE_SQL, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0 or result.stderr.strip():
        raise RuntimeError(result.stderr.strip() or f"psql exited {result.returncode}")
    rows = [line for line in result.stdout.splitlines() if line.lstrip().startswith("{")]
    if len(rows) != 1:
        raise RuntimeError(f"database returned {len(rows)} JSON rows, expected 1")
    parsed = json.loads(rows[0])
    expected = {
        "managed_exit_orders_schwab",
        "managed_exit_orders_webull",
        "reserved_share_reject_orders_webull",
        "owned_open_rows_schwab",
        "owned_open_rows_webull",
        "owned_fresh_rows_schwab",
        "owned_fresh_rows_webull",
        "virtual_zero_held_rows_schwab",
        "virtual_zero_held_rows_webull",
    }
    if set(parsed) != expected or any(
        type(parsed[key]) is not int or parsed[key] < 0 for key in expected
    ):
        raise RuntimeError("database metric set or value types are invalid")
    return parsed


def _read_recent_logs(service: str, since: datetime) -> list[str]:
    command = [
        "sudo",
        "-n",
        "find",
        str(LOG_DIR),
        "-maxdepth",
        "1",
        "-type",
        "f",
        "-name",
        f"{service}.log*",
        "-newermt",
        f"@{int(since.timestamp()) - 300}",
        "-print0",
    ]
    result = subprocess.run(command, capture_output=True, timeout=15, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "log find failed")
    paths = [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]
    if not paths:
        raise RuntimeError(f"no recent {service} logs found")
    rows: list[str] = []
    for path in paths:
        tool = "zcat" if path.suffix == ".gz" else "cat"
        out = subprocess.run(
            ["sudo", "-n", tool, "--", str(path)], capture_output=True, timeout=30, check=False
        )
        if out.returncode != 0:
            raise RuntimeError(
                f"could not read {path}: {out.stderr.decode(errors='replace')[:160]}"
            )
        rows.extend(out.stdout.decode(errors="replace").splitlines())
    return rows


def _load_phantom_report(now: datetime):
    repo = Path(__file__).resolve().parents[2]
    path = repo / "ops" / "health" / "phantom_managed_rows.py"
    spec = importlib.util.spec_from_file_location("known_defect_phantom_rows", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load phantom_managed_rows.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    dsn = delivery._dsn()
    if not dsn:
        raise RuntimeError("database URL is unreadable")
    before, after = module.load_stable_population(dsn)
    before = live_phantom_population(before)
    after = live_phantom_population(after)
    return module.evaluate_stable_population(before, after, now=now)


def _market_day(now: datetime) -> bool:
    from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

    et = now.astimezone(ET)
    return et.weekday() < 5 and et.date() not in US_MARKET_HOLIDAYS


def collect_readings(now: datetime) -> list[Reading]:
    since = session_anchor(now)
    boot_since = since - timedelta(hours=6)
    readings = static_readings()
    try:
        v2_with_boot = parse_log_lines(
            _read_recent_logs("schwab-1m-v2", since=boot_since), since=boot_since, until=now
        )
    except Exception as exc:  # noqa: BLE001 - poison only the rows that use this source
        detail = f"v2 logs unreadable: {type(exc).__name__}: {exc}"
        readings.extend(
            unknown_reading(key, detail)
            for key in (
                "BOOT1",
                "ROLL1",
                "OWNERROLL1",
                "SLOTCLEAR1",
                "LIQPULL1",
                "SEED1",
            )
        )
    else:
        v2 = [line for line in v2_with_boot if line.at >= since]
        readings.extend(
            (
                evaluate_boot(v2_with_boot, now=now),
                evaluate_session_roll(v2, now=now, market_day=_market_day(now)),
                evaluate_owner_roll(v2, now=now, market_day=_market_day(now)),
                evaluate_fresh_sell_slot_clear(v2),
                evaluate_liquidity_pull(v2),
                evaluate_seed(v2),
            )
        )

    oms_readable = True
    try:
        oms = parse_log_lines(_read_recent_logs("oms", since=since), since=since, until=now)
    except Exception as exc:  # noqa: BLE001 - the DB evidence remains independently useful
        oms_readable = False
        oms = []
        readings.append(
            unknown_reading("W4291", f"OMS logs unreadable: {type(exc).__name__}: {exc}")
        )
    else:
        readings.append(evaluate_webull_429(oms))

    try:
        metrics = _query_database(since)
    except Exception as exc:  # noqa: BLE001 - a failed query is unknown, never zero
        detail = f"database census unreadable: {type(exc).__name__}: {exc}"
        readings.extend(unknown_reading(key, detail) for key in ("RESERVE1", "VPZERO1"))
    else:
        for account, suffix in (("live:schwab_1m_v2", "schwab"), ("live:orb", "webull")):
            metrics[f"confirmed_exit_releases_{suffix}"] = confirmed_exit_releases(oms, account)
        readings.extend(evaluate_database(metrics, oms_logs_readable=oms_readable))

    try:
        readings.append(evaluate_phantom(_load_phantom_report(now)))
    except Exception as exc:  # noqa: BLE001 - the stable double-read has its own failure state
        readings.append(
            unknown_reading("PHANTOM1", f"phantom census unreadable: {type(exc).__name__}: {exc}")
        )
    return sorted(readings, key=lambda row: list(SPEC_BY_KEY).index(row.key))


def render(readings: Sequence[Reading], *, now: datetime) -> str:
    lines = [
        f"KNOWN DEFECT REGRESSION WATCH at={now.isoformat()} rows={len(readings)}",
        "RECURRENCE pages. GUARD_WORKING is benign. UNARMED and DELEGATED are never clean zeros.",
    ]
    for row in readings:
        spec = SPEC_BY_KEY[row.key]
        lines.append(
            f"[{row.verdict}] {row.key} evaluated={row.evaluated} "
            f"guard_working={row.guard_working} recurrence={row.recurrence} "
            f"title={spec.title} detail={row.detail}"
        )
    return "\n".join(lines) + "\n"


def _run_watch(
    readings: Sequence[Reading],
    *,
    now: datetime,
    state_path: Path,
    status_path: Path,
    no_page: bool,
    page_fn: Callable[[str, str], bool] = delivery.page,
) -> int:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    state, memory_lost = delivery._load_state(state_path)
    pending: list[Reading] = []
    for row in readings:
        prior = state.get(row.key, {}) if isinstance(state.get(row.key), dict) else {}
        alert_kind = (
            "recurrence"
            if row.verdict == RECURRENCE
            else "could_not_tell"
            if row.verdict == COULD_NOT_TELL
            else ""
        )
        prior_kind = str(prior.get("alert_kind", ""))
        delivered = bool(prior.get("delivered", False)) if alert_kind == prior_kind else False
        if memory_lost and alert_kind:
            # We cannot know whether an existing condition was already announced. Bias to silence
            # and page STATE LOST instead; the condition re-arms only after its verdict clears.
            delivered = True
        state[row.key] = {
            **asdict(row),
            "alert_kind": alert_kind,
            "delivered": delivered,
            "last_run_at": now.isoformat(),
        }
        if alert_kind and not delivered and not memory_lost and not no_page:
            pending.append(row)

    meta = state.get(WATCH_META_KEY, {}) if isinstance(state.get(WATCH_META_KEY), dict) else {}
    state_lost_pending = bool(meta.get("state_lost_pending", False)) or memory_lost
    state[WATCH_META_KEY] = {
        "state_lost_pending": state_lost_pending,
        "last_run_at": now.isoformat(),
    }
    status_path.write_text(render(readings, now=now), encoding="utf-8")
    # The proven ordering is load-bearing: make pending delivery durable before sending anything.
    delivery._write_state(state_path, state)

    if state_lost_pending and not no_page:
        if page_fn(
            "STATE LOST - known defect regression watch",
            "The regression watch lost delivery memory. Historical recurrences were suppressed "
            "rather than replayed. Read STATUS.txt and restore the state file.",
        ):
            state[WATCH_META_KEY]["state_lost_pending"] = False

    for row in pending:
        spec = SPEC_BY_KEY[row.key]
        if row.verdict == RECURRENCE:
            title = f"REGRESSION {row.key} - known defect returned"
            body = (
                f"Known defect recurrence: {row.key}\n{spec.title}\n"
                f"evaluated={row.evaluated} guard_working={row.guard_working} "
                f"recurrence={row.recurrence}\n{row.detail}\n"
                f"Expected guard: {spec.guard_working_shape}\n"
                f"Recurrence: {spec.recurrence_shape}"
            )
        else:
            title = f"CANNOT TELL {row.key} - regression watch is blind"
            body = (
                f"The known-defect watch could not evaluate {row.key}. This is not a clean zero.\n"
                f"{spec.title}\n{row.detail}"
            )
        if page_fn(title, body):
            state[row.key]["delivered"] = True
    delivery._write_state(state_path, state)
    return 2 if any(row.verdict in {RECURRENCE, COULD_NOT_TELL} for row in readings) else 0


def _selftest(page_fn: Callable[[str, str], bool] = delivery.page) -> int:
    delivered = page_fn(
        "SELFTEST known defect regression watch",
        "SELFTEST ONLY. No production defect was observed. This proves the established ntfy "
        "delivery path can still reach the operator.",
    )
    print(f"SELFTEST delivery_confirmed={int(delivered)} live_defect_observed=0")
    return 0 if delivered else 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--no-page", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()

    lock_path = args.state.with_name(args.state.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = datetime.now(UTC)
        try:
            readings = validate_readings(collect_readings(now))
        except Exception as exc:  # noqa: BLE001 - unreadable evidence is never a clean run
            readings = failed_evidence_readings(
                f"evidence collection failed: {type(exc).__name__}: {exc}"
            )
        rc = _run_watch(
            readings,
            now=now,
            state_path=args.state,
            status_path=args.status,
            no_page=args.no_page,
        )
        print(args.status.read_text(encoding="utf-8"), end="")
        return rc


if __name__ == "__main__":
    raise SystemExit(main())
