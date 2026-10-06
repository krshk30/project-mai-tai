"""Pure, conservative evidence classification for ROUNDUP startup recovery."""

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Mapping

from project_mai_tai.fanout_identity import fanout_slot_id
from project_mai_tai.strategy_core.v2_entry_sizing import proven_resting_pair


@dataclass(frozen=True)
class LegacyRestingOrder:
    account: str
    symbol: str
    order_id: str
    client_id: str
    broker_id: str
    intent_id: str = ""
    segment: int = 0
    generation: str = ""
    slot: str = ""
    quantity: int = 0
    level: float = 0.0
    phase: str = "unknown"
    stop: float = 0.0
    limit: float = 0.0
    mirror_generation: str = ""
    slot_id: str = ""
    economic_slot: str = ""


def _decimal(value: object) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite evidence")
    return result


def classify_legacy_order(
    order: Mapping, intent: Mapping | None, events: list[Mapping], *,
    account: str, provider: str, filled: Decimal,
) -> LegacyRestingOrder:
    """No latest-symbol selection: every answer belongs to one exact entry row.

    Pre-generation records use their persisted, client-bound fanout attempt as
    the generation. Request prices prove Schwab wire; only broker-origin
    accepted event prices prove Webull wire.
    """
    result = LegacyRestingOrder(
        account=account, symbol=order["symbol"], order_id=str(order["id"]),
        client_id=str(order["client_order_id"] or ""),
        broker_id=str(order["broker_order_id"] or ""),
        intent_id=str(intent["id"]) if intent else "",
    )
    try:
        md = order["payload"]
        if not isinstance(md, dict) or not intent or not isinstance(intent["payload"], dict):
            return result
        imd = intent["payload"].get("metadata")
        if not isinstance(imd, dict):
            return result
        if (order["strategy_id"] != intent["strategy_id"]
                or order["broker_account_id"] != intent["broker_account_id"]
                or order["symbol"] != intent["symbol"]
                or intent["side"] != "buy" or intent["intent_type"] != "open"
                or order["side"] != "buy" or order["order_type"].upper() != "STOP_LIMIT"):
            return result
        keys = ("fanout_segment_id", "fanout_identity_schema", "fanout_slot",
                "fanout_slot_id", "fanout_attempt_id", "cw_entry_slot")
        if any(not md.get(key) or md.get(key) != imd.get(key) for key in keys):
            return result
        if md.get("resting_entry") != "true" or imd.get("resting_entry") != "true":
            return result
        segment = int(md["fanout_segment_id"])
        slot = md["cw_entry_slot"]
        economic_slot = md["fanout_slot"]
        # Historical broker reclaim rests also used the "resting" economic
        # slot. Preserve it; cw_entry_slot independently identifies consumption.
        if slot not in {"first", "reclaim"} or md["fanout_identity_schema"] != "entry_opportunity_v2":
            return result
        if md["fanout_slot_id"] != fanout_slot_id(
                strategy_code="schwab_1m_v2", symbol=result.symbol,
                segment_id=segment, slot=economic_slot):
            return result
        if not result.client_id or md["fanout_attempt_id"] != result.client_id:
            return result
        generation = md.get("rpg_resting_generation") or md["fanout_attempt_id"]
        if md.get("rpg_resting_generation") != imd.get("rpg_resting_generation"):
            return result
        quantity = _decimal(order["quantity"])
        level = _decimal(md.get("cw_flip_level"))
        if quantity <= 0 or quantity != int(quantity) or quantity != _decimal(intent["quantity"]) or level <= 0:
            return result
        if provider not in {"schwab", "webull"}:
            return result
        if (provider == "webull") != (md.get("fanout_leg") == "webull"):
            return result
        result = replace(result, segment=segment, generation=str(generation), slot=slot,
                         quantity=int(quantity), level=float(level), slot_id=md["fanout_slot_id"],
                         economic_slot=economic_slot,
                         mirror_generation=str(md.get("webull_mirror_generation_id") or ""))
        reports = []
        latest_at = None
        latest_types = set()
        for event in events:
            payload = event["payload"]
            if not isinstance(payload, dict):
                return result
            if event["source"] != "broker":
                continue
            if payload.get("client_order_id") != result.client_id:
                return result
            if payload.get("broker_order_id") and str(payload["broker_order_id"]) != result.broker_id:
                return result
            emd = payload.get("metadata", {})
            if not isinstance(emd, dict):
                return result
            if any(key in emd and emd[key] != md.get(key) for key in (*keys, "rpg_resting_generation")):
                return result
            if event.get("at") != latest_at:
                latest_at, latest_types = event.get("at"), set()
            latest_types.add(event["type"])
            reports.append((event["type"], payload, emd))
        status = str(order["status"]).lower()
        if filled > 0 or status in {"filled", "partially_filled"} or any(
                kind in {"filled", "partially_filled"} or _decimal(payload.get("filled_quantity", "0")) > 0
                for kind, payload, _ in reports):
            return replace(result, phase="consumed")
        terminal = {"cancelled", "canceled", "expired", "rejected"}
        if status in terminal:
            # A client-side abandon or a terminal row alone cannot clear a
            # broker order. An exact broker report must explicitly prove zero.
            if reports and len(latest_types) == 1 and reports[-1][0] in terminal and _decimal(
                    reports[-1][1].get("filled_quantity")) == 0:
                return replace(result, phase="clear")
            return result
        if not result.broker_id or status not in {"accepted", "working", "open"}:
            return result
        if reports and reports[-1][0] in terminal:
            return result
        if provider == "schwab":
            wire = {"stop_price": md.get("stop_price"), "limit_price": md.get("limit_price")}
        else:
            wires = []
            for kind, _, emd in reports:
                if kind != "accepted":
                    continue
                if any(emd.get(key) != md[key] for key in keys):
                    return result
                if _decimal(emd.get("webull_wire_quantity")) != quantity:
                    return result
                wire = {"stop_price": emd.get("webull_wire_stop_price"),
                        "limit_price": emd.get("webull_wire_limit_price")}
                if not proven_resting_pair(wire):
                    return result
                wires.append((_decimal(wire["stop_price"]), _decimal(wire["limit_price"])))
            if not wires or len(set(wires)) != 1:
                return result
            wire = dict(zip(("stop_price", "limit_price"), wires[0]))
        if not proven_resting_pair(wire):
            return result
        return replace(result, phase="working", stop=float(wire["stop_price"]), limit=float(wire["limit_price"]))
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return result
