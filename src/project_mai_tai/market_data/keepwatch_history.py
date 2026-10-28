"""Complete bounded L11 provider reads; errors never attest empty minutes.

Reader-only projection on installed A2. Bot/owner/startup wiring remains a
separate reviewed increment, not implied feature qualification by this module.
"""
from dataclasses import dataclass
import hashlib
import json
import math
import time

from project_mai_tai.market_data.schwab_v2_rest_client import ChartBar, keepwatch_history_window
from project_mai_tai.strategy_core.session_line_restore import (
    PRODUCER_PROJECTION_SOURCE, SessionCoverage, SessionLineRestoration, history_fingerprint,
)


@dataclass(frozen=True)
class KeepWatchHistory:
    symbol: str
    anchor_ms: int
    through_ms: int
    bars: tuple[ChartBar, ...]
    empty_minutes: tuple[int, ...]
    started_ns: int
    completed_ns: int
    massive_sha256: str
    schwab_sha256: str
    massive_ids: tuple[int, ...] = ()
    schwab_ids: tuple[int, ...] = ()
    # Preserve both parsed responses, even where the legacy merge chose Schwab.
    massive_bars: tuple[ChartBar, ...] = ()
    schwab_bars: tuple[ChartBar, ...] = ()
    massive_values_sha256: str = ""


def parse_massive_session(symbol, anchor_ms, through_ms, payload):
    """One raw page covers at most960 minutes, or refuses without empty proof."""
    if (not isinstance(payload, dict) or payload.get("status") != "OK"
            or payload.get("ticker") != symbol or payload.get("next_url")
            or payload.get("truncated") or payload.get("adjusted") is not True):
        raise ValueError("Massive complete current-session response unproven")
    rows = payload.get("results", [])
    if (not isinstance(rows, list) or len(rows) > 960
            or type(payload.get("resultsCount")) is not int
            or payload["resultsCount"] != len(rows)):
        raise ValueError("Massive count/coverage unproven")
    validator = SessionLineRestoration(symbol, anchor_ms, 0)
    selected = {}
    for row in rows:
        if (not isinstance(row, dict) or type(row.get("t")) is not int
                or type(row.get("v")) not in (int, float)
                or not math.isfinite(row["v"]) or row["v"] < 0):
            raise ValueError("malformed Massive candle")
        stamp = row["t"]
        if not anchor_ms <= stamp <= through_ms or stamp in selected:
            raise ValueError("foreign or duplicate Massive candle")
        bar = ChartBar(symbol, *(float(row[key]) for key in ("o", "h", "l", "c")),
                       row["v"], stamp, producer="massive")
        validator.observe(bar)
        selected[stamp] = bar
    return tuple(selected[stamp] for stamp in sorted(selected))


def read_keepwatch_history(schwab, massive, symbol, anchor_ms, through_ms):
    started_ns = time.time_ns()
    if not keepwatch_history_window(anchor_ms, through_ms, started_ns):
        raise ValueError("KEEPWATCH1 query is not a causal closed session window")
    schwab_bars, proof = schwab.fetch_keepwatch_history(
        symbol, anchor_ms, through_ms, observed_at_ns=started_ns,
    )
    if (proof is None or not proof.complete or proof.start_ms != anchor_ms
            or proof.end_ms != through_ms + 60_000
            or proof.closed_ids != tuple(bar.timestamp_ms for bar in schwab_bars)
            or proof.bars_sha256 != history_fingerprint(schwab_bars)):
        raise ValueError("Schwab complete query proof missing")
    if not massive.api_key:
        raise ValueError("Massive credentials unavailable")
    if massive._rest_client is None:
        from massive import RESTClient
        massive._rest_client = RESTClient(
            api_key=massive.api_key, retries=0, connect_timeout=1, read_timeout=2,
        )
    response = massive._rest_client.get_aggs(
        symbol, 1, "minute", from_=anchor_ms, to=through_ms + 59_999,
        adjusted=True, sort="asc", limit=50_000, raw=True,
    )
    raw = response.data
    massive_bars = parse_massive_session(symbol, anchor_ms, through_ms, json.loads(raw))
    merged = {bar.timestamp_ms: bar for bar in massive_bars}
    merged.update((bar.timestamp_ms, bar) for bar in schwab_bars)
    return KeepWatchHistory(
        symbol, anchor_ms, through_ms, tuple(merged[stamp] for stamp in sorted(merged)),
        tuple(stamp for stamp in range(anchor_ms, through_ms + 1, 60_000) if stamp not in merged),
        started_ns, time.time_ns(), hashlib.sha256(raw).hexdigest(), proof.bars_sha256,
        tuple(bar.timestamp_ms for bar in massive_bars),
        tuple(bar.timestamp_ms for bar in schwab_bars),
        tuple(massive_bars), tuple(schwab_bars), history_fingerprint(massive_bars),
    )


def project_history(history, pins, *, now_ms):
    """Accept an already clock/epoch-checked response into the session owner.

    The legacy merged response and its raw hashes are never overwritten. On a
    first full response Schwab retains the old tie priority; an earlier accepted
    callback pin wins over either response's other producer.
    """
    if history.symbol != pins.symbol or history.anchor_ms != pins.anchor_ms:
        raise ValueError("foreign producer history projection")
    candidates = (*history.schwab_bars, *history.massive_bars)
    raw_merged = {bar.timestamp_ms: bar for bar in history.massive_bars}
    raw_merged.update((bar.timestamp_ms, bar) for bar in history.schwab_bars)
    if ({bar.timestamp_ms for bar in candidates} != {bar.timestamp_ms for bar in history.bars}
            or tuple(bar.timestamp_ms for bar in history.schwab_bars) != history.schwab_ids
            or tuple(bar.timestamp_ms for bar in history.massive_bars) != history.massive_ids
            or history.schwab_ids != tuple(sorted(set(history.schwab_ids)))
            or history.massive_ids != tuple(sorted(set(history.massive_ids)))
            or history_fingerprint(history.schwab_bars) != history.schwab_sha256
            or history_fingerprint(history.massive_bars) != history.massive_values_sha256
            or history_fingerprint(tuple(raw_merged[stamp] for stamp in sorted(raw_merged)))
                != history_fingerprint(history.bars)):
        raise ValueError("producer candidates do not bind the parsed response")
    for producer, rows in (("schwab", history.schwab_bars), ("massive", history.massive_bars)):
        for bar in rows:
            if bar.producer != producer or bar.timestamp_ms > history.through_ms:
                raise ValueError("foreign producer candidate in parsed response")
            pins._validate(bar, now_ms)
    for bar in candidates:
        pins.accept(bar, now_ms=now_ms)
    return tuple(pins.current(stamp) for stamp in sorted({bar.timestamp_ms for bar in history.bars}))


def history_has_candidate(history, bar):
    """A canonical repair must still match an actual candidate in this response."""
    rows = (*history.massive_bars, *history.schwab_bars)
    if not rows:  # Existing non-projected proof fixtures/legacy readers.
        rows = history.bars
    return any(history_fingerprint((candidate,)) == history_fingerprint((bar,))
               for candidate in rows if candidate.timestamp_ms == bar.timestamp_ms)


def history_coverage(history, bars, *, canonical=False):
    """Carry actual merged provenance without claiming historical HTTP clocks."""
    ids = tuple(bar.timestamp_ms for bar in bars)
    return SessionCoverage(
        PRODUCER_PROJECTION_SOURCE if canonical else "keepwatch_massive_schwab_session",
        history.anchor_ms, history.through_ms + 60_000,
        ids, True, history_fingerprint(bars), prefix_complete=True,
        empty_minutes=tuple(stamp for stamp in history.empty_minutes if stamp not in ids),
        raw_source="keepwatch_massive_schwab_session" if canonical else None,
        raw_bars_sha256=history_fingerprint(history.bars) if canonical else None,
        raw_closed_ids=tuple(bar.timestamp_ms for bar in history.bars) if canonical else (),
        raw_provider_values=(("massive_response_bytes", history.massive_sha256, history.massive_ids),
                             ("massive_parsed_values", history.massive_values_sha256, history.massive_ids),
                             ("schwab_parsed_values", history.schwab_sha256, history.schwab_ids))
                            if canonical else (),
    )
