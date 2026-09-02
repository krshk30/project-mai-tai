#!/usr/bin/env python3
"""Report executable-bid extrema for real V2 first/resting fills.

This is a measurement over broker fills, not an ATR replay. Historical rows use the same retained
placement-log attribution as ``resting_entry_slippage.py``; the small override table records the
previously reconciled rows whose old payloads and rounded placement prices cannot identify a slot.
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

EASTERN = ZoneInfo("America/New_York")
AUDIT_RE = re.compile(
    r"^  (?P<md>\d\d-\d\d) (?P<hms>\d\d:\d\d:\d\d)  "
    r"(?P<symbol>\S+)\s+(?P<slot>first|reclaim|unattributed)\s+"
)

# These were reconciled against the exact service marker or an unambiguous path marker. Keeping
# them explicit is safer than inferring economic slot from STOP_LIMIT order style.
HISTORICAL_OVERRIDES = {
    ("live:schwab_1m_v2", "08-24", "07:32:02", "PMI"): "first",
    ("live:schwab_1m_v2", "08-24", "09:24:30", "PMI"): "reclaim",
    ("live:schwab_1m_v2", "08-24", "10:03:22", "PMI"): "reclaim",
    ("live:schwab_1m_v2", "08-24", "13:59:08", "DAIC"): "reclaim",
    ("live:schwab_1m_v2", "08-25", "11:36:51", "DAIC"): "first",
    ("live:schwab_1m_v2", "08-26", "08:34:28", "DAIC"): "first",
    ("live:schwab_1m_v2", "08-26", "08:49:21", "DAIC"): "reclaim",
    ("live:orb", "08-24", "07:32:04", "PMI"): "first",
    ("live:orb", "08-24", "09:24:32", "PMI"): "reclaim",
    ("live:orb", "08-24", "09:26:59", "BTCT"): "first",
    ("live:orb", "08-24", "10:03:26", "PMI"): "reclaim",
    ("live:orb", "08-25", "12:22:58", "AIXI"): "first",
    ("live:orb", "08-25", "14:12:15", "AIXI"): "first",
    ("live:orb", "08-26", "08:34:31", "DAIC"): "first",
    ("live:orb", "08-26", "08:49:23", "DAIC"): "reclaim",
    ("live:orb", "08-26", "08:55:12", "YYGH"): "first",
    ("live:orb", "08-26", "08:57:44", "CRE"): "first",
    ("live:orb", "08-26", "10:32:40", "YYGH"): "first",
    ("live:orb", "08-26", "13:24:51", "YYGH"): "first",
    ("live:orb", "08-26", "15:53:55", "CRE"): "reclaim",
    ("live:orb", "08-27", "11:15:40", "PPCB"): "reclaim",
}


@dataclass
class Fill:
    fill_id: str
    account: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    at: datetime
    client_order_id: str
    order_type: str
    reason: str
    fill_source: str
    target_price: Decimal | None
    stop_price: Decimal | None
    fanout_slot_id: str = ""
    slot: str = ""
    cw_flip_level: Decimal | None = None
    exits: list[tuple[Decimal, "Fill"]] = field(default_factory=list)

    @property
    def session_day(self) -> str:
        return self.at.astimezone(EASTERN).date().isoformat()


def parse_timestamp(value: str) -> datetime:
    raw = value.strip().replace(" ", "T").replace("Z", "+00:00")
    if re.search(r"[+-]\d\d$", raw):
        raw += ":00"
    try:
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        # Python 3.9 rejects some short fractional-second forms accepted by Postgres.
        bare = value.split("+")[0]
        base, _, fraction = bare.partition(".")
        parsed = datetime.strptime(base, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return parsed.replace(microsecond=int((fraction + "000000")[:6]))


def decimal_or_none(value) -> Decimal | None:
    if value in (None, "", 0, "0"):
        return None
    return Decimal(str(value))


def load_audit_slots(primary: Path, webull: Path) -> dict[tuple[str, str, str, str], str]:
    slots: dict[tuple[str, str, str, str], str] = {}
    for account, path in (("live:schwab_1m_v2", primary), ("live:orb", webull)):
        for line in path.read_text().splitlines():
            match = AUDIT_RE.match(line)
            if match:
                slots[(account, match["md"], match["hms"], match["symbol"])] = match["slot"]
    slots.update(HISTORICAL_OVERRIDES)
    return slots


def load_entry_fills(path: Path, slots) -> list[Fill]:
    result: list[Fill] = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            at = parse_timestamp(row["filled_at"])
            et = at.astimezone(EASTERN)
            slot = slots.get(
                (row["account"], et.strftime("%m-%d"), et.strftime("%H:%M:%S"), row["symbol"])
            )
            if slot is None:
                raise RuntimeError(f"missing slot attribution for fill {row['fill_id']}")
            if slot != "first":
                continue
            result.append(
                Fill(
                    fill_id=row["fill_id"],
                    account=row["account"],
                    symbol=row["symbol"],
                    side="buy",
                    quantity=Decimal("0"),
                    price=Decimal(row["price"]),
                    at=at,
                    client_order_id="",
                    order_type=row["order_type"],
                    reason=row["reason"],
                    fill_source=row["fanout_source"],
                    target_price=None,
                    stop_price=None,
                    fanout_slot_id=row["fanout_slot_id"],
                    slot="first",
                )
            )
    return result


def load_all_fills(session_factory, start: datetime, end: datetime) -> list[Fill]:
    with session_factory() as session:
        rows = session.execute(
            text(
                """
                SELECT f.id::text, ba.name, f.symbol, lower(f.side), f.quantity, f.price,
                       f.filled_at, bo.client_order_id, bo.order_type,
                       COALESCE(ti.reason, ''), COALESCE(f.payload->>'source', ''),
                       f.payload->'metadata'->>'bracket_target_price',
                       f.payload->'metadata'->>'bracket_stop_price',
                       COALESCE(f.payload->'metadata'->>'fanout_slot_id', ''),
                       COALESCE(f.payload->'metadata'->>'cw_entry_slot', ''),
                       f.payload->'metadata'->>'cw_flip_level'
                FROM fills f
                JOIN broker_orders bo ON bo.id=f.order_id
                JOIN broker_accounts ba ON ba.id=f.broker_account_id
                JOIN strategies st ON st.id=f.strategy_id
                LEFT JOIN trade_intents ti ON ti.id=bo.intent_id
                WHERE st.code='schwab_1m_v2'
                  AND ba.name IN ('live:schwab_1m_v2', 'live:orb')
                  AND f.filled_at >= :start AND f.filled_at <= :end
                ORDER BY f.filled_at, f.id
                """
            ),
            {"start": start, "end": end},
        ).all()
    return [
        Fill(
            fill_id=str(row[0]),
            account=str(row[1]),
            symbol=str(row[2]),
            side=str(row[3]),
            quantity=Decimal(str(row[4])),
            price=Decimal(str(row[5])),
            at=row[6].astimezone(UTC),
            client_order_id=str(row[7] or ""),
            order_type=str(row[8] or ""),
            reason=str(row[9] or ""),
            fill_source=str(row[10] or ""),
            target_price=decimal_or_none(row[11]),
            stop_price=decimal_or_none(row[12]),
            fanout_slot_id=str(row[13] or ""),
            slot=str(row[14] or ""),
            cw_flip_level=decimal_or_none(row[15]),
        )
        for row in rows
    ]


def pair_exits(fills: list[Fill]) -> None:
    queues: dict[tuple[str, str, str], deque[tuple[Fill, Decimal]]] = defaultdict(deque)
    for fill in fills:
        key = (fill.session_day, fill.account, fill.symbol)
        if fill.side == "buy":
            queues[key].append((fill, fill.quantity))
            continue
        if fill.side != "sell":
            continue
        remaining = fill.quantity
        queue = queues[key]
        while remaining > 0 and queue:
            entry, open_quantity = queue[0]
            used = min(remaining, open_quantity)
            entry.exits.append((used, fill))
            remaining -= used
            open_quantity -= used
            if open_quantity == 0:
                queue.popleft()
            else:
                queue[0] = (entry, open_quantity)


def group_events(entries: list[Fill]) -> list[list[Fill]]:
    parent = list(range(len(entries)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = root(left), root(right)
        if left != right:
            parent[right] = left

    for left, first in enumerate(entries):
        for right in range(left + 1, len(entries)):
            second = entries[right]
            if first.session_day != second.session_day or first.symbol != second.symbol:
                continue
            seconds = abs((first.at - second.at).total_seconds())
            cross_broker = first.account != second.account and seconds <= 10
            duplicate = (
                first.account == second.account
                and bool(first.fanout_slot_id)
                and first.fanout_slot_id == second.fanout_slot_id
                and seconds <= 30
            )
            if cross_broker or duplicate:
                union(left, right)
    grouped: dict[int, list[Fill]] = defaultdict(list)
    for index, entry in enumerate(entries):
        grouped[root(index)].append(entry)
    return sorted(grouped.values(), key=lambda group: min(fill.at for fill in group))


def exit_trigger(entry: Fill, exit_fill: Fill) -> str:
    if exit_fill.order_type == "oco_exit" or exit_fill.fill_source == "native_oco_child_leg":
        if entry.target_price is not None and entry.stop_price is not None:
            if abs(exit_fill.price - entry.target_price) <= abs(exit_fill.price - entry.stop_price):
                return "native OCO target"
            return "native OCO stop"
        return "native OCO exit"
    if exit_fill.reason.startswith("oms_v2_managed_exit:"):
        return exit_fill.reason.split(":", 1)[1]
    return exit_fill.reason or exit_fill.order_type or "sell fill"


def leg_boundary(entry: Fill) -> tuple[datetime, str, str, str]:
    cutoff = datetime.combine(entry.at.astimezone(EASTERN).date(), time(16), EASTERN).astimezone(UTC)
    exited = sum((quantity for quantity, _ in entry.exits), Decimal("0")) >= entry.quantity
    final_exit = max((fill.at for _, fill in entry.exits), default=None)
    if exited and final_exit is not None and final_exit <= cutoff:
        total = sum((quantity for quantity, _ in entry.exits), Decimal("0"))
        price = sum((quantity * fill.price for quantity, fill in entry.exits), Decimal("0")) / total
        triggers = list(dict.fromkeys(exit_trigger(entry, fill) for _, fill in entry.exits))
        return final_exit, "actual exit", f"{price:.4f}", "+".join(triggers)
    return cutoff, "16:00 cutoff", "", "still open at 16:00"


def extrema(session_factory, entry: Fill, boundary: datetime):
    with session_factory() as session:
        common = {
            "symbol": entry.symbol,
            "start": entry.at,
            "end": boundary,
        }
        quote_filter = """
            symbol=:symbol AND event_ts >= :start AND event_ts <= :end
            AND bid_price > 0 AND ask_price >= bid_price
            AND ((ask_price-bid_price)/bid_price*100) <= 50
        """
        high = session.execute(
            text(
                f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE {quote_filter} "
                "ORDER BY bid_price DESC,event_ts ASC LIMIT 1"
            ),
            common,
        ).first()
        low = session.execute(
            text(
                f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE {quote_filter} "
                "ORDER BY bid_price ASC,event_ts ASC LIMIT 1"
            ),
            common,
        ).first()
        last = session.execute(
            text(
                f"SELECT bid_price,event_ts FROM market_capture_quotes WHERE {quote_filter} "
                "ORDER BY event_ts DESC,id DESC LIMIT 1"
            ),
            common,
        ).first()
        bars = session.execute(
            text(
                """
                SELECT bar_time FROM strategy_bar_history
                WHERE strategy_code='schwab_1m_v2' AND symbol=:symbol AND interval_secs=60
                  AND bar_time >= date_trunc('minute', CAST(:start AS timestamptz))
                  AND bar_time < CASE
                    WHEN CAST(:end AS timestamptz) = date_trunc('minute', CAST(:end AS timestamptz))
                    THEN CAST(:end AS timestamptz)
                    ELSE date_trunc('minute', CAST(:end AS timestamptz)) + interval '1 minute'
                  END
                ORDER BY bar_time
                """
            ),
            common,
        ).scalars().all()
    return high, low, last, bars


def missing_bar_text(entry: Fill, boundary: datetime, bars: list[datetime]) -> str:
    start = entry.at.replace(second=0, microsecond=0)
    end = boundary.replace(second=0, microsecond=0)
    if boundary.second or boundary.microsecond:
        end += timedelta(minutes=1)
    observed = {bar.astimezone(UTC).replace(second=0, microsecond=0) for bar in bars}
    missing: list[datetime] = []
    cursor = start
    while cursor < end:
        if cursor not in observed:
            missing.append(cursor)
        cursor += timedelta(minutes=1)
    if not missing:
        return "none"
    shown = ",".join(value.astimezone(EASTERN).strftime("%H:%M") for value in missing[:8])
    if len(missing) > 8:
        shown += f",+{len(missing)-8} more"
    return f"{len(missing)} missing: {shown}"


def pct(price: Decimal, entry_price: Decimal) -> float:
    return float((price / entry_price - Decimal("1")) * Decimal("100"))


def account_label(account: str) -> str:
    return "Schwab" if account == "live:schwab_1m_v2" else "Webull"


def fmt_et(value: datetime, *, millis: bool = False) -> str:
    pattern = "%H:%M:%S.%f" if millis else "%H:%M:%S"
    rendered = value.astimezone(EASTERN).strftime(pattern)
    return rendered[:-3] if millis else rendered


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entries", type=Path, required=True)
    parser.add_argument("--primary-audit", type=Path, required=True)
    parser.add_argument("--webull-audit", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()

    slots = load_audit_slots(args.primary_audit, args.webull_audit)
    entry_shells = load_entry_fills(args.entries, slots)
    if len(entry_shells) != 106:
        raise RuntimeError(f"expected 106 first/resting fill rows, got {len(entry_shells)}")

    start = min(fill.at for fill in entry_shells).astimezone(EASTERN).replace(
        hour=4, minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    final_day = max(fill.at for fill in entry_shells).astimezone(EASTERN).date()
    end = datetime.combine(final_day, time(16), EASTERN).astimezone(UTC)
    session_factory = build_session_factory(get_settings())
    all_fills = load_all_fills(session_factory, start, end)
    by_id = {fill.fill_id: fill for fill in all_fills}
    entries: list[Fill] = []
    for shell in entry_shells:
        real = by_id.get(shell.fill_id)
        if real is None:
            raise RuntimeError(f"entry fill disappeared from database: {shell.fill_id}")
        real.fanout_slot_id = shell.fanout_slot_id
        real.slot = "first"
        entries.append(real)
    pair_exits(all_fills)
    events = group_events(entries)
    if len(events) != 82:
        raise RuntimeError(f"expected 82 distinct first/resting events, got {len(events)}")

    output_rows: list[dict[str, str]] = []
    markdown_rows: list[str] = []
    for number, event in enumerate(events, 1):
        event.sort(key=lambda fill: (fill.at, fill.account, fill.fill_id))
        duplicate = len(event) > 1 and len({fill.account for fill in event}) == 1
        leg_results = []
        for leg in event:
            boundary, boundary_kind, exit_price, trigger = leg_boundary(leg)
            high, low, last, bars = extrema(session_factory, leg, boundary)
            if boundary_kind == "16:00 cutoff" and last is not None:
                exit_price = f"{Decimal(str(last[0])):.4f} mark"
            max_up = "NA"
            max_up_minutes = "NA"
            max_down = "NA"
            max_down_minutes = "NA"
            if high is not None:
                max_up = f"{pct(Decimal(str(high[0])), leg.price):+.4f}"
                max_up_minutes = f"{(high[1]-leg.at).total_seconds()/60:.3f}"
            if low is not None:
                max_down = f"{pct(Decimal(str(low[0])), leg.price):+.4f}"
                max_down_minutes = f"{(low[1]-leg.at).total_seconds()/60:.3f}"
            missing = missing_bar_text(leg, boundary, bars)
            label = account_label(leg.account)
            leg_results.append(
                {
                    "label": label,
                    "fill_id": leg.fill_id,
                    "entry": fmt_et(leg.at, millis=True),
                    "price": f"{leg.price:.4f}",
                    "up_pct": max_up,
                    "up_minutes": max_up_minutes,
                    "up": f"{max_up}% @ {max_up_minutes}m",
                    "down_pct": max_down,
                    "down_minutes": max_down_minutes,
                    "down": f"{max_down}% @ {max_down_minutes}m",
                    "exit_time": fmt_et(boundary, millis=True),
                    "exit_price": exit_price or "NA",
                    "trigger": trigger,
                    "exit": f"{fmt_et(boundary, millis=True)} / {exit_price or 'NA'} / {trigger}",
                    "boundary": boundary_kind,
                    "missing": missing,
                }
            )
        def join(key: str) -> str:
            return "<br>".join(f"{leg['label']}: {leg[key]}" for leg in leg_results)

        def join_csv(key: str) -> str:
            return "; ".join(f"{leg['label']}: {leg[key]}" for leg in leg_results)
        broker = "+".join(dict.fromkeys(leg["label"] for leg in leg_results))
        if duplicate:
            broker += " [DUPLICATE x2]"
        output_rows.append(
            {
                "event": str(number),
                "date_et": event[0].session_day,
                "symbol": event[0].symbol,
                "entry_time_et": join_csv("entry"),
                "broker": broker,
                "fill_id": join_csv("fill_id"),
                "fill_price": join_csv("price"),
                "duplicate_webull_fill": "yes" if duplicate else "no",
                "max_up_pct": join_csv("up_pct"),
                "max_up_elapsed_minutes": join_csv("up_minutes"),
                "max_down_pct": join_csv("down_pct"),
                "max_down_elapsed_minutes": join_csv("down_minutes"),
                "boundary_kind": join_csv("boundary"),
                "exit_time_et": join_csv("exit_time"),
                "exit_price": join_csv("exit_price"),
                "exit_trigger": join_csv("trigger"),
                "missing_bars": join_csv("missing"),
            }
        )
        markdown_rows.append(
            f"| {number} | {event[0].session_day} | {event[0].symbol} | {join('entry')} | "
            f"{broker} | {join('price')} | {join('up')} | {join('down')} | {join('exit')} | "
            f"{join('boundary')} | {join('missing')} |"
        )

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    with args.csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0]))
        writer.writeheader()
        writer.writerows(output_rows)
    lines = [
        "# Actual V2 First/Resting Entry Extrema",
        "",
        "| # | Date ET | Symbol | Entry time ET | Broker | Fill price | Max up / elapsed | Max down / elapsed | Exit time / price / trigger | Boundary | Missing bars |",
        "|---:|---|---|---|---|---|---|---|---|---|---|",
        *markdown_rows,
        "",
    ]
    args.markdown.write_text("\n".join(lines))
    print(f"events={len(events)} csv_rows={len(output_rows)}")
    print(f"duplicate_events={sum('[DUPLICATE' in row for row in markdown_rows)}")
    print(args.csv)
    print(args.markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
