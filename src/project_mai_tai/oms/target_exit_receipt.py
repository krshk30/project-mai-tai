"""Software-target attempt telemetry only; never order or ownership authority."""


def target_exit_receipt(*, account, symbol, metadata, guards, request=None,
                        reports=(), received_at=None, error_type=None, blocked_guard=None):
    submitted = metadata.get("cw_submitted_at") if request is not None else None
    matching = [r for r in reports if request is not None
                and r.client_order_id == request.client_order_id and r.symbol == request.symbol
                and r.side == request.side and r.intent_type == request.intent_type]
    api_report = next((r for r in matching if r.origin != "client"
        and r.event_type == "accepted"
        and r.metadata.get("cw_target_api_ack_client_order_id") == request.client_order_id
        and r.metadata.get("cw_target_api_ack_evidence") in {
            "schwab_http_201_location", "webull_matching_client_order_id"}
        and r.metadata.get("cw_target_api_received_at")), None)
    ack = next((r for r in matching if r.origin == "broker" and r.event_type in {
        "accepted", "rejected", "filled", "partially_filled"}), api_report)
    if request is None:
        outcome = "not_submitted"
    elif error_type:
        outcome = "cancelled" if error_type == "CancelledError" else "error"
    elif ack is not None:
        outcome = "broker_rejected" if ack.event_type == "rejected" else "broker_accepted"
    elif any(r.origin == "client" and r.event_type in {"rejected", "aborted"} for r in matching):
        outcome = "client_refusal"
    elif matching:
        outcome = "unclassified_report"
    else:
        outcome = "unmatched_report" if reports else "transport_return"
    received = received_at.isoformat(timespec="microseconds") if received_at else None
    # ACK is OMS receipt of bound positive evidence, never a historical report timestamp.
    ack_at = received if ack is not None else None
    report_at = getattr(ack, "reported_at", None)
    decision = metadata.get("cw_decided_at")
    return dict(version=1, event_type="software_target_attempt", account=account, symbol=symbol,
        client_order_id=request.client_order_id if request else None,
        retry_of=metadata.get("reserve1_retry_of"), session=metadata.get("session"),
        order_type=request.order_type if request else None, limit_price=metadata.get("limit_price") if request else None,
        quantity=str(request.quantity) if request else None, decision_at=decision, submit_at=submitted,
        broker_ack_at=ack_at, broker_ack_definition="local_oms_receipt_of_bound_broker_ack_evidence",
        broker_ack_evidence=ack.metadata.get("cw_target_api_ack_evidence", "broker_origin_report") if ack else None,
        adapter_api_received_at=next((r.metadata.get("cw_target_api_received_at") for r in matching
            if r.metadata.get("cw_target_api_received_at")), None),
        adapter_api_ack_at=api_report.metadata["cw_target_api_received_at"] if api_report else None,
        adapter_api_time_definition="local_adapter_receipt_of_actual_api_response_not_exchange_time",
        broker_report_at=report_at.isoformat(timespec="microseconds") if report_at else None,
        broker_report_time_definition="adapter_supplied_reported_at_not_ack_receipt",
        adapter_wire_submit_at=ack.metadata.get("webull_wire_submitted_at_utc") if ack else None,
        broker_ack_event_type=ack.event_type if ack else None, transport_return_at=received,
        outcome=outcome, error_type=error_type, blocked_guard=blocked_guard, guards=guards,
        decision_to_submit_ms=_elapsed(decision, submitted), submit_to_ack_ms=_elapsed(submitted, ack_at),
        report_types=[dict(event_type=r.event_type, origin=r.origin) for r in reports[:4]], report_count=len(reports))


def _elapsed(begin, end):
    from datetime import datetime
    try:
        value = (datetime.fromisoformat(end)-datetime.fromisoformat(begin)).total_seconds()*1000
        return round(value, 3) if value >= 0 else None
    except (ValueError, TypeError):
        return None  # Unknown/skewed timing is not zero.
