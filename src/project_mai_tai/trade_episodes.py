from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from typing import Any

from project_mai_tai.strategy_core.time_utils import EASTERN_TZ


@dataclass(frozen=True)
class CompletedTradeCycle:
    strategy_code: str
    broker_account_name: str
    symbol: str
    cycle_key: str
    path: str
    quantity: float
    entry_time: str
    entry_price: float
    exit_time: str
    exit_price: float
    pnl: float
    pnl_pct: float
    summary: str
    sort_time: str

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


GENERIC_PATHS = {"", "-", "DB_RECONCILE", "RECONCILED"}
GENERIC_SUMMARIES = {"close", "final close", "completed", "-", "reconciled close"}


def collect_completed_trade_cycles(
    *,
    strategy_code: str,
    broker_account_name: str,
    recent_orders: list[dict[str, Any]],
    recent_fills: list[dict[str, Any]],
    closed_today: list[dict[str, Any]] | None = None,
) -> list[CompletedTradeCycle]:
    completed_rows: list[dict[str, Any]] = []
    existing_keys: set[tuple[str, str, str, str]] = set()
    open_trades_by_symbol: dict[str, list[dict[str, Any]]] = {}

    def append_completed_trade(trade: dict[str, Any]) -> None:
        initial_qty = max(float(trade["initial_qty"]), 0.0001)
        entry_price = float(trade["entry_price"])
        blended_exit = trade["exit_value"] / initial_qty if initial_qty > 0 else 0.0
        total_pnl = trade["exit_value"] - (entry_price * initial_qty)
        pnl_pct = (blended_exit - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        entry_time = str(trade["entry_time"] or "-")
        exit_time = str(trade["exit_time"] or "-")
        symbol = str(trade["ticker"]).upper()
        existing_keys.add((strategy_code, broker_account_name, symbol, entry_time))
        completed_rows.append(
            {
                "strategy_code": strategy_code,
                "broker_account_name": broker_account_name,
                "symbol": symbol,
                "cycle_key": cycle_key(
                    strategy_code=strategy_code,
                    broker_account_name=broker_account_name,
                    symbol=symbol,
                    entry_time=entry_time,
                    exit_time=exit_time,
                ),
                "path": str(trade["path"] or "-"),
                "quantity": initial_qty,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "exit_time": exit_time,
                "exit_price": blended_exit,
                "pnl": total_pnl,
                "pnl_pct": pnl_pct,
                "summary": summarize_exit_events(trade["exit_events"], initial_qty),
                "sort_time": str(trade["exit_time"] or trade["entry_time"]),
            }
        )

    def reconstruct_from_events(
        events: list[dict[str, Any]],
        *,
        timestamp_key: str,
        price_key: str,
    ) -> None:
        open_trades_by_symbol.clear()
        for item in sorted(events, key=lambda row: parse_et_timestamp(str(row.get(timestamp_key, "") or ""))):
            symbol = str(item.get("symbol", "")).upper()
            side = str(item.get("side", "")).lower()
            quantity = as_float(item.get("quantity"))
            if not symbol or quantity <= 0:
                continue

            event_time = str(item.get(timestamp_key, "") or "")
            event_price = as_float(item.get(price_key))
            reason = str(item.get("reason", "") or "").strip()
            path = display_order_path(item)

            intent_type = str(item.get("intent_type", "") or "").lower()
            if not intent_type:
                if side == "buy":
                    intent_type = "open"
                elif reason.upper().startswith("SCALE_"):
                    intent_type = "scale"
                else:
                    intent_type = "close"

            if looks_like_broker_payload_text(reason):
                if intent_type == "close":
                    reason = "FINAL_CLOSE"
                elif intent_type == "scale":
                    reason = "SCALE"
                else:
                    reason = ""

            if intent_type == "open" and side == "buy":
                open_trades_by_symbol.setdefault(symbol, []).append(
                    {
                        "ticker": symbol,
                        "path": path,
                        "entry_time": event_time,
                        "entry_price": event_price,
                        "initial_qty": quantity,
                        "remaining_qty": quantity,
                        "exit_value": 0.0,
                        "exit_time": "",
                        "exit_events": [],
                    }
                )
                continue

            if side != "sell" or intent_type not in {"scale", "close"}:
                continue

            remaining_to_apply = quantity
            open_queue = open_trades_by_symbol.get(symbol, [])
            for trade in reversed(open_queue):
                if remaining_to_apply <= 0:
                    break
                trade_remaining = float(trade["remaining_qty"])
                if trade_remaining <= 0:
                    continue
                applied_qty = min(remaining_to_apply, trade_remaining)
                if applied_qty <= 0:
                    continue
                trade["remaining_qty"] -= applied_qty
                trade["exit_value"] += applied_qty * event_price
                trade["exit_time"] = event_time
                trade["exit_events"].append(
                    {
                        "qty": applied_qty,
                        "price": event_price,
                        "reason": reason.upper() or intent_type.upper(),
                        "intent_type": intent_type,
                    }
                )
                remaining_to_apply -= applied_qty
                if trade["remaining_qty"] <= 0:
                    append_completed_trade(trade)

    def find_matching_completed_row(
        *,
        symbol: str,
        entry_time: str,
        exit_time: str,
        quantity: float,
    ) -> dict[str, Any] | None:
        target_entry = parse_et_timestamp(entry_time)
        target_exit = parse_et_timestamp(exit_time)
        best_match: dict[str, Any] | None = None
        best_score: tuple[float, float, int, int] | None = None
        for row in completed_rows:
            if str(row.get("strategy_code", "") or "") != strategy_code:
                continue
            if str(row.get("broker_account_name", "") or "") != broker_account_name:
                continue
            if str(row.get("symbol", "") or "").upper() != symbol:
                continue
            row_entry = parse_et_timestamp(row.get("entry_time"))
            row_exit = parse_et_timestamp(row.get("exit_time"))
            entry_delta = abs((row_entry - target_entry).total_seconds())
            if entry_delta > 5:
                continue
            row_quantity = as_float(row.get("quantity"))
            if quantity > 0 and row_quantity > 0 and abs(row_quantity - quantity) > 0.0001:
                continue
            exit_delta = abs((row_exit - target_exit).total_seconds())
            score = (
                entry_delta,
                exit_delta,
                1 if is_generic_path(row.get("path")) else 0,
                1 if is_generic_summary(str(row.get("summary", "") or "")) else 0,
            )
            if best_score is None or score < best_score:
                best_score = score
                best_match = row
        return best_match

    reconstruct_from_events(
        recent_fills,
        timestamp_key="filled_at",
        price_key="price",
    )

    reconstruct_from_events(
        [
            item
            for item in recent_orders
            if str(item.get("status", "")).lower() == "filled"
        ],
        timestamp_key="updated_at",
        price_key="price",
    )

    for item in closed_today or []:
        symbol = str(item.get("ticker", "") or "").upper()
        entry_time = str(item.get("entry_time", "") or "")
        raw_reason = str(item.get("reason", "") or item.get("exit_reason", "") or "").strip()
        if not symbol or not entry_time or looks_like_broker_payload_text(raw_reason):
            continue
        if (strategy_code, broker_account_name, symbol, entry_time) in existing_keys:
            continue
        exit_time = str(item.get("exit_time", "") or "-")
        raw_path = str(item.get("path", "") or item.get("entry_path", "") or "-").strip() or "-"
        quantity = as_float(
            item.get(
                "original_quantity",
                item.get("original_qty", item.get("quantity", item.get("qty", 0))),
            )
        )
        path = normalize_display_path(raw_path)
        summary = summarize_closed_today_reason(item)
        matched_row = find_matching_completed_row(
            symbol=symbol,
            entry_time=entry_time,
            exit_time=exit_time,
            quantity=quantity,
        )
        if matched_row is not None:
            matched_path = str(matched_row.get("path", "") or "")
            if is_generic_path(path) and not is_generic_path(matched_path):
                path = matched_path
            matched_summary = str(matched_row.get("summary", "") or "")
            if is_generic_summary(summary) and not is_generic_summary(matched_summary):
                summary = matched_summary
        if raw_path.upper() == "DB_RECONCILE" and is_generic_summary(summary):
            summary = "Reconciled close"
        completed_rows.append(
            {
                "strategy_code": strategy_code,
                "broker_account_name": broker_account_name,
                "symbol": symbol,
                "cycle_key": cycle_key(
                    strategy_code=strategy_code,
                    broker_account_name=broker_account_name,
                    symbol=symbol,
                    entry_time=entry_time,
                    exit_time=exit_time,
                ),
                "path": path,
                "quantity": quantity,
                "entry_time": entry_time,
                "entry_price": as_float(item.get("entry_price")),
                "exit_time": exit_time,
                "exit_price": as_float(item.get("exit_price")),
                "pnl": as_float(item.get("pnl")),
                "pnl_pct": as_float(item.get("pnl_pct")),
                "summary": summary,
                "sort_time": str(item.get("exit_time", "") or item.get("closed_at", "") or entry_time),
            }
        )

    return [
        CompletedTradeCycle(**row)
        for row in coalesce_completed_trade_cycles(completed_rows)
    ]


def coalesce_completed_trade_cycles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def parse_time(value: Any) -> datetime:
        return parse_et_timestamp(str(value or ""))

    def is_shadow_row(row: dict[str, Any]) -> bool:
        path = str(row.get("path", "") or "-").strip().upper()
        summary = str(row.get("summary", "") or "").strip()
        return is_generic_path(path) or is_generic_summary(summary)

    def merge_row(primary: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        merged = dict(primary)
        merged_shadow = is_shadow_row(merged)
        incoming_shadow = is_shadow_row(incoming)
        if merged_shadow and not incoming_shadow:
            merged["quantity"] = incoming.get("quantity", merged.get("quantity"))
        if is_generic_path(merged.get("path")) and not is_generic_path(incoming.get("path")):
            merged["path"] = incoming.get("path")
        if as_float(merged.get("entry_price")) <= 0 and as_float(incoming.get("entry_price")) > 0:
            merged["entry_price"] = incoming.get("entry_price")
        if as_float(merged.get("exit_price")) <= 0 and as_float(incoming.get("exit_price")) > 0:
            merged["exit_price"] = incoming.get("exit_price")
        if as_float(merged.get("pnl")) == 0 and abs(as_float(incoming.get("pnl"))) > 0:
            merged["pnl"] = incoming.get("pnl")
            merged["pnl_pct"] = incoming.get("pnl_pct")
        if is_generic_summary(str(merged.get("summary", "") or "")) and not is_generic_summary(
            str(incoming.get("summary", "") or "")
        ):
            merged["summary"] = incoming.get("summary")
        if parse_time(merged.get("sort_time")) < parse_time(incoming.get("sort_time")):
            merged["sort_time"] = incoming.get("sort_time")
        if parse_time(merged.get("exit_time")) < parse_time(incoming.get("exit_time")):
            merged["exit_time"] = incoming.get("exit_time")
            merged["cycle_key"] = incoming.get("cycle_key", merged.get("cycle_key"))
        return merged

    merged_rows: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: (parse_time(item.get("entry_time")), parse_time(item.get("exit_time")))):
        match_index: int | None = None
        for index, existing in enumerate(merged_rows):
            if str(existing.get("strategy_code", "") or "") != str(row.get("strategy_code", "") or ""):
                continue
            if str(existing.get("broker_account_name", "") or "") != str(row.get("broker_account_name", "") or ""):
                continue
            if str(existing.get("symbol", "") or "").upper() != str(row.get("symbol", "") or "").upper():
                continue
            existing_entry = parse_time(existing.get("entry_time"))
            existing_exit = parse_time(existing.get("exit_time"))
            row_entry = parse_time(row.get("entry_time"))
            row_exit = parse_time(row.get("exit_time"))
            quantity_matches = abs(as_float(existing.get("quantity")) - as_float(row.get("quantity"))) <= 0.0001
            shadow_merge = is_shadow_row(existing) or is_shadow_row(row)
            if abs((existing_entry - row_entry).total_seconds()) <= 2 and quantity_matches and abs((existing_exit - row_exit).total_seconds()) <= 2:
                match_index = index
                break
            if shadow_merge and abs((existing_entry - row_entry).total_seconds()) <= 5:
                match_index = index
                break
        if match_index is None:
            merged_rows.append(dict(row))
        else:
            merged_rows[match_index] = merge_row(merged_rows[match_index], row)
    return merged_rows


def cycle_key(
    *,
    strategy_code: str,
    broker_account_name: str,
    symbol: str,
    entry_time: str,
    exit_time: str,
) -> str:
    return "|".join(
        [
            str(strategy_code).strip().lower(),
            str(broker_account_name).strip().lower(),
            str(symbol).strip().upper(),
            str(entry_time).strip(),
            str(exit_time).strip(),
        ]
    )


def summarize_closed_today_reason(item: dict[str, Any]) -> str:
    reason = str(item.get("reason", "") or item.get("exit_reason", "") or "").strip()
    scales_done = [str(scale).strip().upper() for scale in (item.get("scales_done", []) or []) if str(scale).strip()]
    if reason and not looks_like_broker_payload_text(reason):
        clean = reason.replace("_", " ").title()
        if scales_done:
            return f'Scaled first ({", ".join(scales_done)}), then {clean}'
        return clean
    if scales_done:
        return f'Scaled first ({", ".join(scales_done)}), then final close'
    return "Final close"


def display_order_path(item: dict[str, Any]) -> str:
    path = extract_path_value(item.get("path", ""))
    if not path:
        metadata = item.get("metadata", {})
        if isinstance(metadata, dict):
            path = (
                extract_path_value(metadata.get("path"))
                or extract_path_value(metadata.get("confirmation_path"))
                or extract_path_value(metadata.get("decision_path"))
            )
    if not path:
        payload = item.get("payload", {})
        if isinstance(payload, dict):
            metadata = payload.get("metadata", {})
            if isinstance(metadata, dict):
                path = (
                    extract_path_value(metadata.get("path"))
                    or extract_path_value(metadata.get("confirmation_path"))
                    or extract_path_value(metadata.get("decision_path"))
                )
    if path:
        return path
    reason = str(item.get("reason", "") or "").strip()
    if reason.startswith("ENTRY_"):
        return reason.removeprefix("ENTRY_")
    return "-"


def extract_path_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.upper() in {"", "-", "DB_RECONCILE", "RECONCILED"}:
        return ""
    return text


def normalize_display_path(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    if text.upper() == "DB_RECONCILE":
        return "RECONCILED"
    return text


def is_generic_path(value: Any) -> bool:
    return str(value or "").strip().upper() in GENERIC_PATHS


def is_generic_summary(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized in GENERIC_SUMMARIES


def looks_like_broker_payload_text(value: Any) -> bool:
    text = str(value or "").strip()
    if not text.startswith("{"):
        return False
    lower_text = text.lower()
    broker_markers = (
        "orderlegcollection",
        "executionlegs",
        "orderstrategytype",
        "instrumentid",
        "requesteddestination",
        "'session':",
    )
    return any(marker in lower_text for marker in broker_markers)


def summarize_exit_events(exit_events: list[dict[str, Any]], initial_qty: float) -> str:
    if not exit_events:
        return "Completed"
    scale_qty = sum(as_float(event.get("qty")) for event in exit_events if event.get("intent_type") == "scale")
    close_events = [event for event in exit_events if event.get("intent_type") == "close"]
    close_qty = sum(as_float(event.get("qty")) for event in close_events)
    if close_events and scale_qty > 0:
        close_reason_raw = str(close_events[-1].get("reason", "") or "final close")
        if looks_like_broker_payload_text(close_reason_raw):
            close_reason_raw = "final close"
        close_reason = close_reason_raw.replace("_", " ").title()
        return f"Scaled out {format_qty(scale_qty)}, then closed {format_qty(close_qty)} on {close_reason}"
    if close_events:
        close_reason_raw = str(close_events[-1].get("reason", "") or "final close")
        if looks_like_broker_payload_text(close_reason_raw):
            close_reason_raw = "final close"
        close_reason = close_reason_raw.replace("_", " ").title()
        return close_reason
    if scale_qty >= initial_qty - 0.0001:
        return f"Fully scaled out in {len(exit_events)} fills"
    return f"Scaled out {format_qty(scale_qty)}"


def parse_et_timestamp(value: str) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=EASTERN_TZ)
    try:
        return datetime.strptime(value, "%Y-%m-%d %I:%M:%S %p ET").replace(tzinfo=EASTERN_TZ)
    except ValueError:
        try:
            parsed_time = datetime.strptime(value, "%I:%M:%S %p ET")
            current_et = datetime.now(UTC).astimezone(EASTERN_TZ)
            return current_et.replace(
                hour=parsed_time.hour,
                minute=parsed_time.minute,
                second=parsed_time.second,
                microsecond=0,
            )
        except ValueError:
            return datetime.min.replace(tzinfo=EASTERN_TZ)


def format_money(value: float) -> str:
    if value <= 0:
        return "-"
    return f"${value:.2f}"


def format_qty(value: float) -> str:
    if abs(value) < 0.0001:
        return "-"
    if abs(value - round(value)) < 0.0001:
        return str(int(round(value)))
    return f"{value:.2f}"


def as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
