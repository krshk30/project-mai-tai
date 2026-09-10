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



def _entry_falls_inside_an_existing_cycle(
    spans: list[tuple[str, str, str, object, object]],
    strategy_code: str,
    account_name: str,
    symbol: str,
    entry_time: str,
) -> bool:
    """True when a managed row describes a position an earlier source already produced.

    ⛔ The identity is the INTERVAL, not the timestamp. Two sources describe the same position with
    different entry stamps -- the fill when it happened, the managed row when the OMS saw it -- and
    on the Webull fan-out leg that gap was 13 seconds. Matching on the exact string produced a
    duplicate completed position with no prices and $0.00 P&L.

    ⭐ It stays tight enough for back-to-back trades on one symbol: BNC exited 11:02:21 and re-entered
    11:03:08 on 2026-09-08, and the second entry falls OUTSIDE the first cycle's window, so it is
    still reported as its own trade.
    """
    target = parse_et_timestamp(entry_time)
    for span_strategy, span_account, span_symbol, span_entry, span_exit in spans:
        if span_strategy != strategy_code or span_account != account_name:
            continue
        if span_symbol != symbol:
            continue
        if span_entry <= target <= span_exit:
            return True
    return False


def _find_covering_row(
    completed_rows: list[dict[str, Any]],
    strategy_code: str,
    account_name: str,
    symbol: str,
    entry_time: str,
) -> dict[str, Any] | None:
    """Return an already-built cycle whose [entry, exit] window contains this entry, if any.

    ⛔ The identity is the INTERVAL, not the timestamp. Two passes describe the same position with
    different entry stamps — the fill when it happened, the order when it was marked filled — and on
    the Webull fan-out leg that gap was ~14 seconds.

    ⭐ It stays tight enough for back-to-back trades on one symbol: BNC exited 11:02:21 and
    re-entered 11:03:08 on 2026-09-08, and the second entry falls OUTSIDE the first window.
    """
    target = parse_et_timestamp(entry_time)
    for row in completed_rows:
        if str(row.get("strategy_code", "") or "") != strategy_code:
            continue
        if str(row.get("broker_account_name", "") or "") != account_name:
            continue
        if str(row.get("symbol", "") or "").upper() != symbol:
            continue
        if parse_et_timestamp(row.get("entry_time")) <= target <= parse_et_timestamp(row.get("exit_time")):
            return row
    return None

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
    # ⛔ A closed_today row and a fill-derived cycle describe THE SAME position, and their entry
    # timestamps legitimately differ: the fill is stamped when it happened, the managed row when the
    # OMS observed it. On the Webull fan-out leg that settle lag was 13s on 2026-09-08 (fill
    # 09:50:51, managed row 09:51:04), so the exact-string key below missed and the managed row was
    # appended as a SECOND cycle -- rendered with no prices and $0.00 P&L beside the real one.
    # The interval is the identity: a managed row whose entry falls INSIDE an existing cycle's
    # [entry, exit] window is that cycle, not a new trade.
    existing_spans: list[tuple[str, str, str, object, object]] = []
    open_trades_by_account_symbol: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def append_completed_trade(trade: dict[str, Any]) -> None:
        initial_qty = max(float(trade["initial_qty"]), 0.0001)
        entry_price = float(trade["entry_price"])
        blended_exit = trade["exit_value"] / initial_qty if initial_qty > 0 else 0.0
        total_pnl = trade["exit_value"] - (entry_price * initial_qty)
        pnl_pct = (blended_exit - entry_price) / entry_price * 100 if entry_price > 0 else 0.0
        entry_time = str(trade["entry_time"] or "-")
        exit_time = str(trade["exit_time"] or "-")
        symbol = str(trade["ticker"]).upper()
        trade_account_name = str(trade["broker_account_name"] or broker_account_name)

        # ⛔ A CYCLE ALREADY COVERING THIS INTERVAL IS THIS CYCLE. The fills pass and the filled-order
        # pass both reconstruct positions, and where both exist they describe ONE position twice. The
        # order copy is the worse of the two: a broker_orders row carries no fill price, so it renders
        # `-` entry, `-` exit and $0.00 P&L, stamped at `updated_at` rather than the fill time.
        # Measured live 2026-09-08 — every Webull fan-out leg appeared twice, e.g. BNC 12:09:50
        # -$0.02 beside a phantom 12:10:04 $+0.00 from open-e0c300989641 / close-8a0891514c98, both
        # payload_has_price=NO. Exact-timestamp dedupe missed because the two stamps differ by ~14s.
        # ⭐ ENRICH, NEVER DISCARD. The order pass is not only a fallback: it carries the PATH label
        # the fills lack, which test_collect_completed_trade_cycles_prefers_fills pins. So a covered
        # duplicate fills in what the surviving row is missing and is then dropped.
        covering = _find_covering_row(
            completed_rows, strategy_code, trade_account_name, symbol, entry_time
        )
        if covering is not None:
            candidate_path = str(trade["path"] or "").strip()
            if candidate_path and is_generic_path(covering.get("path")):
                covering["path"] = candidate_path
            # ⛔⭐⭐ THE EXIT REASON IS THE OTHER THING ONLY THE ORDER PASS CARRIES.
            # `recent_fills` rows have no `client_order_id` and no `reason`, so the fills pass -
            # which WINS, because it is the priced one - can only ever derive "Close" from the
            # side. The order row is the only place the `-ocoexit-` marker and the
            # `oms_v2_managed_exit:*` text exist. Enriching `path` but not `summary` is why the
            # operator's Completed Positions table read "Close" on every row on 2026-09-09 while
            # four distinct mechanisms were firing underneath it.
            # ⇒ Same contract as `path`: fill in what the survivor is missing, never overwrite a
            # summary that already names a mechanism.
            candidate_summary = summarize_exit_events(trade["exit_events"], initial_qty)
            if candidate_summary and not is_generic_summary(candidate_summary):
                if is_generic_summary(str(covering.get("summary", "") or "")):
                    covering["summary"] = candidate_summary
            if not entry_price and covering.get("entry_price"):
                pass  # the priced row already wins; nothing to take from an unpriced duplicate
            return

        existing_keys.add((strategy_code, trade_account_name, symbol, entry_time))
        existing_spans.append(
            (
                strategy_code,
                trade_account_name,
                symbol,
                parse_et_timestamp(entry_time),
                parse_et_timestamp(exit_time),
            )
        )
        completed_rows.append(
            {
                "strategy_code": strategy_code,
                "broker_account_name": trade_account_name,
                "symbol": symbol,
                "cycle_key": cycle_key(
                    strategy_code=strategy_code,
                    broker_account_name=trade_account_name,
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
        open_trades_by_account_symbol.clear()
        for item in sorted(events, key=lambda row: parse_et_timestamp(str(row.get(timestamp_key, "") or ""))):
            symbol = str(item.get("symbol", "")).upper()
            event_account_name = str(item.get("broker_account_name", "") or broker_account_name)
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
                open_trades_by_account_symbol.setdefault((event_account_name, symbol), []).append(
                    {
                        "ticker": symbol,
                        "broker_account_name": event_account_name,
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
            open_queue = open_trades_by_account_symbol.get((event_account_name, symbol), [])
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
                        # ⛔ Classified mechanism FIRST; the raw text is only the fallback and
                        # `intent_type.upper()` is the last resort - that last resort is what
                        # printed "Close" on every row.
                        "reason": classify_exit_reason(item) or reason.upper() or intent_type.upper(),
                        "intent_type": intent_type,
                    }
                )
                remaining_to_apply -= applied_qty
                if trade["remaining_qty"] <= 0:
                    append_completed_trade(trade)

    def find_matching_completed_row(
        *,
        symbol: str,
        row_account_name: str,
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
            if str(row.get("broker_account_name", "") or "") != row_account_name:
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
        symbol = str(item.get("ticker", "") or item.get("symbol", "") or "").upper()
        row_account_name = str(item.get("broker_account_name", "") or broker_account_name)
        entry_time = str(item.get("entry_time", "") or "")
        raw_reason = str(item.get("reason", "") or item.get("exit_reason", "") or "").strip()
        if not symbol or not entry_time or looks_like_broker_payload_text(raw_reason):
            continue
        if (strategy_code, row_account_name, symbol, entry_time) in existing_keys:
            continue
        if _entry_falls_inside_an_existing_cycle(
            existing_spans, strategy_code, row_account_name, symbol, entry_time
        ):
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
            row_account_name=row_account_name,
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
                "broker_account_name": row_account_name,
                "symbol": symbol,
                "cycle_key": cycle_key(
                    strategy_code=strategy_code,
                    broker_account_name=row_account_name,
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
    v2_path = display_v2_entry_path(item)
    if v2_path:
        return v2_path
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


def display_v2_entry_path(item: dict[str, Any]) -> str:
    metadata = item.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    payload = item.get("payload", {})
    if isinstance(payload, dict) and isinstance(payload.get("metadata"), dict):
        payload_metadata = payload["metadata"]
    else:
        payload_metadata = {}

    entry_slot = str(
        item.get("entry_slot", "")
        or metadata.get("cw_entry_slot", "")
        or payload_metadata.get("cw_entry_slot", "")
    ).strip().lower()
    path_label = {"first": "Resting", "reclaim": "Reclaim"}.get(entry_slot)
    if not path_label:
        return ""

    provider = str(item.get("broker_provider", "") or "").strip().lower()
    account_name = str(item.get("broker_account_name", "") or "").strip().lower()
    if provider == "schwab" or account_name == "live:schwab_1m_v2":
        venue_label = "Schwab"
    elif provider == "webull" or account_name == "live:orb":
        venue_label = "Webull"
    else:
        venue_label = "Unknown venue"
    return f"{path_label} / {venue_label}"


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


# ⛔⭐⭐ THE EXIT SUMMARY MUST NAME THE MECHANISM, NOT THE ORDER STATUS.
# Operator 2026-09-09: "the exit summary says just close. I don't know whether that is the right
# close ... you have a hard stop or we reach the positive result, our ATR exit - those exits I need
# to know." Every row rendered "Close" because the exit event's `reason` arrived empty and the code
# fell back to `intent_type.upper()`, which is literally "CLOSE". A status is not a reason: on
# 2026-09-09 four DIFFERENT mechanisms all rendered identically as "Close" -
#   11 OCO bracket (broker +5% target / -8% stop) - 8 ATR flip - 3 confirmation - 1 floor.
# ⛔ The `-ocoexit-` suffix is the ONLY attributable exit marker we have: the broker OCO legs carry
# the entry's own client_order_id, while a software close mints a fresh `-close-<uuid>` with no link
# back. So the coid is checked FIRST and is authoritative; the reason text is the fallback.
_EXIT_REASON_LABELS: tuple[tuple[str, str], ...] = (
    ("CW_FLIP", "ATR flip exit"),
    ("CONFIRMATION_EXIT", "Confirmation exit"),
    ("CW_HARD_STOP", "Hard stop"),
    ("CW_FLOOR", "Floor exit"),
    ("CW_TARGET", "Target reached"),
    ("SCALE_", "Scale-out"),
    ("DB_RECONCILE", "Reconciled"),
    ("MANUAL", "Manual close"),
)


_CLASSIFIED_EXIT_LABELS: frozenset[str] = frozenset(
    {label for _needle, label in _EXIT_REASON_LABELS} | {"OCO bracket (target/stop)"}
)


def _present_exit_reason(raw: str) -> str:
    """Title-case a RAW machine reason, but leave an already-classified label alone.

    ⛔ `.title()` on "OCO bracket (target/stop)" yields "Oco Bracket (Target/Stop)" and on
    "ATR flip exit" yields "Atr Flip Exit". The classifier's output is already operator-facing.
    """

    text = str(raw or "").strip()
    if text in _CLASSIFIED_EXIT_LABELS:
        return text
    return text.replace("_", " ").title()


def classify_exit_reason(item: dict[str, Any]) -> str:
    """Human label for WHY a position closed. Empty string when we genuinely cannot tell.

    ⛔ Returns "" rather than a guess. An unattributable exit must READ as unattributable -
    inventing a plausible mechanism is worse than admitting we do not know, because a wrong
    reason stops the investigation that a blank one starts.
    """

    coid = str(item.get("client_order_id", "") or "")
    if "-ocoexit-" in coid:
        # The broker's own OCO pair fired. Which leg it was is NOT recorded on the order, so do
        # not claim "target" or "stop" here - the P&L sign next to it already tells the operator.
        return "OCO bracket (target/stop)"
    reason = str(item.get("reason", "") or "").strip()
    if looks_like_broker_payload_text(reason):
        return ""
    upper = reason.upper()
    for needle, label in _EXIT_REASON_LABELS:
        if needle in upper:
            return label
    return ""


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
        close_reason = _present_exit_reason(close_reason_raw)
        return f"Scaled out {format_qty(scale_qty)}, then closed {format_qty(close_qty)} on {close_reason}"
    if close_events:
        close_reason_raw = str(close_events[-1].get("reason", "") or "final close")
        if looks_like_broker_payload_text(close_reason_raw):
            close_reason_raw = "final close"
        close_reason = _present_exit_reason(close_reason_raw)
        return close_reason
    if scale_qty >= initial_qty - 0.0001:
        return f"Fully scaled out in {len(exit_events)} fills"
    return f"Scaled out {format_qty(scale_qty)}"


def parse_et_timestamp(value: str) -> datetime:
    """Parse either display-ET or ISO-UTC into an ET-aware datetime.

    ⛔ ISO SUPPORT IS LOAD-BEARING, NOT A CONVENIENCE. The dashboard's own rows are display-ET
    ("2026-09-08 09:51:04 AM ET"), but `closed_today` comes from the bot as
    `lot[2].isoformat()` — "2026-09-08T13:51:04.129000+00:00". Without this branch every ISO value
    fell through to datetime.min, so BOTH duplicate checks compared a real timestamp against
    year 1 and never matched. That is why the same position was rendered twice with $0.00 P&L:
    not a settle-lag near-miss, but two sources speaking different time formats.
    """
    if not value:
        return datetime.min.replace(tzinfo=EASTERN_TZ)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        pass
    else:
        # A naive ISO value comes from a UTC-aware datetime that lost its offset in transit; the
        # publishers in this codebase are all UTC. Display-ET strings never reach this branch.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(EASTERN_TZ)
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
