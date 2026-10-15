"""Offline next-session restore from literal read-only box rows; no broker IO."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import inspect
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import UUID

from sqlalchemy import create_engine, insert
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.sqltypes import DateTime, Numeric, Uuid

from project_mai_tai.db.models import (
    AccountPosition, Base, BrokerAccount, DashboardSnapshot, OmsManagedPosition,
    Strategy, TradeIntent, VirtualPosition,
)
from project_mai_tai.fanout_segment_store import FanoutSegmentIdentityStore
from project_mai_tai.falseflip1_runtime import FalseFlipStore
from project_mai_tai.v2_flip_entry_ownership import FlipEntryOwnershipStore, FlipPositionBook
from project_mai_tai.v2_removed_wait import RemovedWaitStore
from scripts import line_repair_preopen_replay as replay
from tests.unit.test_sunday_v2_bar_tick_rate import CANDIDATE, ROWS

MONDAY = datetime(2026, 10, 12, 8, tzinfo=UTC)


def load_rows(session, model, rows):
    for record in rows:
        values = {}
        for column in model.__table__.columns:
            if column.name not in record:
                continue
            value = record[column.name]
            if value is not None and isinstance(column.type, Uuid):
                value = UUID(value)
            elif value is not None and isinstance(column.type, DateTime):
                value = datetime.fromisoformat(value)
            elif value is not None and isinstance(column.type, Numeric):
                value = Decimal(str(value))
            values[column.name] = value
        session.execute(insert(model.__table__).values(**values))


def shift_market_input(original):
    row = deepcopy(original)
    delta = (MONDAY.date() - datetime.fromisoformat(row["day"]).date()).days * 86_400_000
    for key in ("anchor_ms", "boundary_ms", "cutoff_ms"):
        row[key] += delta
    for source in ("preopen", "chart"):
        for bar in row[source].get("results", []):
            bar["t"] += delta
    for bar in row["schwab"]["candles"]:
        bar["datetime"] += delta
    watched = int(datetime.fromisoformat(original["added_at"]).timestamp() * 1000) + delta
    candles = sorted(row["schwab"]["candles"], key=lambda c: c["datetime"])
    available = [c for c in candles if c["datetime"] >= max(
        row["boundary_ms"] + 9 * 60_000, watched - watched % 60_000)]
    return row, available


async def main(args):
    owners = json.loads(Path(args.owners).read_text())
    support = json.loads(Path(args.support).read_text())
    tape = json.loads(Path(args.tape).read_text())
    assert tape["retrospective_retained_tape"] is True
    tape_pairs = {(r["day"], r["symbol"], r["lo"], r["hi"]): r["traded"]
                  for r in tape["rows"]}
    assert len(tape_pairs) == len(tape["rows"])
    assert all(type(value) is bool for value in tape_pairs.values())
    assert owners["active_removed_wait"] == 0
    assert not support["open_managed"] and not support["open_virtual"]
    assert not support["pending_intents"]
    assert not any(Decimal(str(p["quantity"])) for p in support["account_positions"])
    with TemporaryDirectory(prefix="sunday-boot-copy-") as directory:
        engine = create_engine("sqlite:///" + str(Path(directory) / "copy.sqlite"))
        Base.metadata.create_all(engine)
        sf = sessionmaker(engine, expire_on_commit=False)
        with sf() as session:
            load_rows(session, DashboardSnapshot, owners["rows"])
            for model, key in ((BrokerAccount, "broker_accounts"), (Strategy, "strategies"),
                               (AccountPosition, "account_positions"),
                               (OmsManagedPosition, "open_managed"),
                               (VirtualPosition, "open_virtual"), (TradeIntent, "pending_intents")):
                load_rows(session, model, support[key])
            session.commit()
        prior_segments = FanoutSegmentIdentityStore(sf).restore_active(now=MONDAY)
        prior_owners = FlipEntryOwnershipStore(sf).restore_active(now=MONDAY)
        prior_budget = FlipEntryOwnershipStore(sf).restore_retry_budgets(now=MONDAY)
        prior_false = FalseFlipStore(sf).restore(now=MONDAY)
        requests = RemovedWaitStore(sf).restore()
        assert not any((prior_segments, prior_owners, prior_budget, prior_false, requests))
        results = []
        with patch.object(replay, "LIVE_CONTROLS", CANDIDATE):
            for original in ROWS:
                row, available = shift_market_input(original)
                assert available, (original["day"], original["symbol"])
                current = available[0]["datetime"]
                case, _provider = replay.setup(row, current, admission_control=False)
                bot, strategy = case.bot, case.strategy
                bot.session_factory = sf
                delta = row["anchor_ms"] - original["anchor_ms"]

                def recorded_tape(symbol, pairs):
                    # Exact bounded box-query outputs, not an invented empty tape.
                    # Only the recorded market clock is shifted to Monday.
                    found = []
                    for left, right in pairs:
                        key = (original["day"], symbol, left - delta, right - delta)
                        if tape_pairs[key]:
                            found.append((left, right))
                    return tuple(found)

                bot._read_line_gap_trades = recorded_tape

                class Clock(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        value = datetime.fromtimestamp(case.now_ms / 1000, UTC)
                        return value.astimezone(tz) if tz else value.replace(tzinfo=None)

                with case.clock(row["anchor_ms"]), patch(
                    "project_mai_tai.fanout_segment_store.datetime", Clock,
                ):
                    segments = bot._configure_fanout_identity_store()
                    bot._configure_flip_entry_ownership_store(segments)
                    configured = bot._configure_removed_wait_store()
                    if inspect.isawaitable(configured):
                        await configured
                    assert not strategy._removed_wait_requests
                # Subsequent simulated writes remain in the disposable local copy.
                with case.clock(current + 61_000):
                    # Explicit future flat-book control, NOT a Monday broker receipt.
                    strategy.apply_flip_position_book(FlipPositionBook(case.now_ms, True, {}))
                    bot._boot_scanner_selected = {case.symbol}
                    bot._boot_exclusion_sources_readable = True
                    bot._try_complete_boot_state_restoration({case.symbol}, new_symbols={case.symbol})
                    rebuilt = await replay.repair(row, case, current)
                    bot._try_complete_boot_state_restoration({case.symbol}, new_symbols=set())
                    bot._cw_boot_hold_check()
                    gate = strategy._removed_wait_gate_closed(case.symbol)
                    state = strategy.watchlist_state(case.symbol)
                assert not gate
                results.append({"source_day": original["day"], "symbol": case.symbol,
                                "simulated_bar_ms": current, "rebuilt": rebuilt,
                                "state": state.atr_state, "level": state.atr_trail,
                                "boot_complete": bot._boot_state_restoration_complete,
                                "previous_session_gate": gate})
        print(json.dumps({"owner_rows_as_of": owners["as_of"],
                          "support_rows_as_of": support["as_of"],
                          "retrospective_tape_as_of": tape["as_of"],
                          "queried_tape_pairs": len(tape_pairs),
                          "literal_durable_rows": len(owners["rows"]),
                          "simulated_boot": MONDAY.isoformat(), "gating_rows": 0,
                          "restored_previous_session_requests": len(requests),
                          "future_broker_book": "explicit flat control; not live Monday evidence",
                          "future_market": "recorded 25 diagnostics time-shifted only",
                          "rows": results}, indent=2))
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--owners", required=True)
    parser.add_argument("--support", required=True)
    parser.add_argument("--tape", required=True)
    asyncio.run(main(parser.parse_args()))
