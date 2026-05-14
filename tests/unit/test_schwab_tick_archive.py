from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_mai_tai.market_data.models import QuoteTickRecord, TradeTickRecord
from project_mai_tai.market_data.schwab_tick_archive import (
    DEFAULT_MAX_HANDLES,
    SchwabTickArchive,
)


def _record_n_symbols(archive: SchwabTickArchive, n: int, *, prefix: str = "SYM") -> list[Path]:
    paths = []
    base_ns = 1_715_000_000_000_000_000
    for i in range(n):
        record = TradeTickRecord(
            symbol=f"{prefix}{i:03d}",
            price=1.0 + i / 100,
            size=100,
            timestamp_ns=base_ns + i,
            cumulative_volume=None,
            exchange="Q",
            conditions=(),
        )
        paths.append(archive.record_trade(record, recorded_at_ns=base_ns + i))
    return paths


def test_handle_cache_is_bounded_by_max_handles(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path, max_handles=4)
    paths = _record_n_symbols(archive, 10)

    assert len(archive._handles) == 4
    # The 4 most recently written paths should remain cached.
    assert list(archive._handles.keys()) == paths[-4:]
    # All 10 files were written to disk despite the cache cap.
    for path in paths:
        assert path.exists()
        assert path.read_text().count("\n") == 1


def test_repeated_writes_to_same_symbol_do_not_grow_cache(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path, max_handles=4)
    base_ns = 1_715_000_000_000_000_000
    record = TradeTickRecord(
        symbol="AAA",
        price=1.0,
        size=100,
        timestamp_ns=base_ns,
        cumulative_volume=None,
        exchange="Q",
        conditions=(),
    )
    for i in range(20):
        archive.record_trade(record, recorded_at_ns=base_ns + i)

    assert len(archive._handles) == 1
    written_path = next(iter(archive._handles))
    assert written_path.read_text().count("\n") == 20


def test_re_accessing_cached_symbol_marks_it_most_recently_used(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path, max_handles=3)
    a, b, c = _record_n_symbols(archive, 3)
    assert list(archive._handles.keys()) == [a, b, c]

    # Touch A again — it should jump to the MRU end.
    base_ns = 1_715_000_000_000_001_000
    record = TradeTickRecord(
        symbol="SYM000",
        price=1.01,
        size=50,
        timestamp_ns=base_ns,
        cumulative_volume=None,
        exchange="Q",
        conditions=(),
    )
    archive.record_trade(record, recorded_at_ns=base_ns)
    assert list(archive._handles.keys()) == [b, c, a]

    # Add a 4th distinct symbol — B (now the LRU) should be evicted, not A.
    record = TradeTickRecord(
        symbol="SYM999",
        price=2.0,
        size=50,
        timestamp_ns=base_ns + 1,
        cumulative_volume=None,
        exchange="Q",
        conditions=(),
    )
    new_path = archive.record_trade(record, recorded_at_ns=base_ns + 1)
    assert b not in archive._handles
    assert list(archive._handles.keys()) == [c, a, new_path]


def test_eviction_closes_the_evicted_handle(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path, max_handles=1)
    a, b = _record_n_symbols(archive, 2)
    assert list(archive._handles.keys()) == [b]
    # The fact that we appended a single line to A before eviction and the
    # file content is intact proves the handle was flushed (line-buffered)
    # and closed cleanly without losing the write.
    assert a.read_text().count("\n") == 1


def test_writes_after_eviction_append_not_truncate(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path, max_handles=1)
    base_ns = 1_715_000_000_000_000_000
    rec_a1 = TradeTickRecord(
        symbol="AAA",
        price=1.0,
        size=100,
        timestamp_ns=base_ns,
        cumulative_volume=None,
        exchange="Q",
        conditions=(),
    )
    rec_b = TradeTickRecord(
        symbol="BBB",
        price=2.0,
        size=100,
        timestamp_ns=base_ns + 1,
        cumulative_volume=None,
        exchange="Q",
        conditions=(),
    )
    rec_a2 = TradeTickRecord(
        symbol="AAA",
        price=1.5,
        size=200,
        timestamp_ns=base_ns + 2,
        cumulative_volume=None,
        exchange="Q",
        conditions=(),
    )
    path_a = archive.record_trade(rec_a1, recorded_at_ns=base_ns)
    archive.record_trade(rec_b, recorded_at_ns=base_ns + 1)  # evicts A
    archive.record_trade(rec_a2, recorded_at_ns=base_ns + 2)  # reopens A

    lines = [json.loads(line) for line in path_a.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["price"] == 1.0
    assert lines[1]["price"] == 1.5


def test_close_drops_all_cached_handles(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path, max_handles=10)
    _record_n_symbols(archive, 5)
    assert len(archive._handles) == 5
    archive.close()
    assert archive._handles == {}


def test_default_cap_is_sized_above_typical_watchlist(tmp_path: Path) -> None:
    # Production safety: the default must comfortably hold the typical
    # watchlist size (~30 per bot × 3 active bots ≈ 90) plus headroom for
    # symbol rotation throughout the day, while staying well below the Linux
    # soft FD limit (1024). 256 satisfies both.
    archive = SchwabTickArchive(tmp_path)
    assert archive.max_handles == DEFAULT_MAX_HANDLES
    assert 128 <= DEFAULT_MAX_HANDLES <= 512


def test_max_handles_zero_is_coerced_to_one(tmp_path: Path) -> None:
    # Defensive: a misconfigured max_handles=0 would otherwise evict every
    # handle before its write completed.
    archive = SchwabTickArchive(tmp_path, max_handles=0)
    assert archive.max_handles == 1
    paths = _record_n_symbols(archive, 3)
    for path in paths:
        assert path.exists()


def test_record_quote_uses_same_handle_cache(tmp_path: Path) -> None:
    archive = SchwabTickArchive(tmp_path, max_handles=2)
    base_ns = 1_715_000_000_000_000_000
    quote = QuoteTickRecord(
        symbol="AAA",
        bid_price=1.0,
        ask_price=1.01,
        bid_size=100,
        ask_size=100,
    )
    trade = TradeTickRecord(
        symbol="AAA",
        price=1.005,
        size=100,
        timestamp_ns=base_ns + 1,
        cumulative_volume=None,
        exchange="Q",
        conditions=(),
    )
    quote_path = archive.record_quote(quote, recorded_at_ns=base_ns)
    trade_path = archive.record_trade(trade, recorded_at_ns=base_ns + 1)
    assert quote_path == trade_path
    # Both record types share the per-(day, symbol) handle.
    assert len(archive._handles) == 1
    lines = quote_path.read_text().splitlines()
    assert len(lines) == 2
    event_types = [json.loads(line)["event_type"] for line in lines]
    assert event_types == ["quote", "trade"]
