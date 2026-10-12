"""Bounded paper-to-measurement mapping; never reconstruct missing timestamps."""

from __future__ import annotations


WINDOWS = {"momentum_30s": 30, "momentum_60s": 60,
           "momentum_30s_warn": 30, "momentum_60s_warn": 60}
MS_NS = 1_000_000


def _stamp(value: object) -> bool:
    return type(value) is int and 0 < value < 2**63


def _trade_time(result: dict, summary: dict, event: dict, field: str, status: str) -> None:
    trade = summary.get(field)
    if not isinstance(trade, dict) or not _stamp(trade.get("sip_ts_ms")):
        return
    stem = "entry" if field == "fill" else "exit"
    lower = trade["sip_ts_ms"] * MS_NS
    result.update({f"{stem}_sip_start_ns": lower, f"{stem}_sip_end_ns": lower + MS_NS,
                   f"{stem}_price": trade.get("price"),
                   f"{stem}_timestamp_precision": "integer_sip_ms"})
    # The outer SIP stamp belongs to this callback, NOT necessarily the original
    # signal/fill/target print. Only use it where the actual stored print matches.
    sip = event.get("signal_sip_ns")
    if status == ("FILLED" if field == "fill" else "EXITED") and _stamp(sip):
        if sip // MS_NS == trade["sip_ts_ms"]:
            result[f"{stem}_ns"] = sip
            result[f"{stem}_timestamp_precision"] = "real_sdk_ns"


def paper_metric_rows(event: dict) -> tuple[dict, ...] | None:
    """None means already canonical; an empty tuple is a candidate, not a BUY.

    The caller validates size, producer epoch and observation clocks first.
    Never borrow an epoch, comparison ID, clock assurance or subscription ACK.
    """
    if event.get("source") != "momentum-paper" or "ledger" in event:
        return None
    if event.get("event_type") not in {"decision", "snapshot_entry"}:
        return None
    if "signals" in event:
        records = event["signals"]
        if not isinstance(records, list) or len(records) > 2:
            raise ValueError("paper signal expansion exceeds two ledgers")
    elif "strategy_code" in event and "payload" in event:
        records = [event]
    else:
        return None
    result = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("invalid paper lifecycle row")
        code, logical_id = record.get("strategy_code"), record.get("logical_id")
        summary, status = record.get("payload"), record.get("status")
        if code not in WINDOWS or not isinstance(summary, dict):
            raise ValueError("invalid paper ledger or payload")
        if not isinstance(logical_id, str) or not logical_id or status not in {
            "DETECTED", "FILLED", "EXITED", "FINALIZED", "NO_FILL",
        }:
            raise ValueError("invalid paper lifecycle identity/status")
        sparse = status in {"FILLED", "EXITED"}
        if any(
            (key in summary or not sparse) and summary.get(key) != expected
            for key, expected in (
                ("symbol", event.get("symbol")),
                ("logical_id", logical_id), ("strategy_code", code),
                ("window_seconds", WINDOWS[code]),
            )
        ):
            raise ValueError("paper summary identity mismatch")
        row = {key: value for key, value in event.items()
               if key not in {"signals", "payload", "strategy_code", "logical_id",
                              "signal_sip_ns", "signal_start_ms", "signal_end_ms"}}
        row.update(ledger=code, window_seconds=WINDOWS[code], lineage_id=logical_id,
                   status=status, outcome=summary.get("status", status), pnl=summary.get("pnl"),
                   native_lifecycle_update=sparse,
                   paper_contract_version=1, signal_sip_ns=None, entry_ns=None, exit_ns=None,
                   observeddecision_ns=(summary.get("observed_decision_ns")
                                        if code.endswith("_warn")
                                        else event.get("observed_decision_ns")
                                        if status == "DETECTED" else None))
        detection = summary.get("detect")
        if isinstance(detection, dict):
            ms, sip = detection.get("sip_ts_ms"), event.get("signal_sip_ns")
            if _stamp(ms):
                row.update(signal_basis="integer_ms_bounds", signal_sip_start_ns=ms * MS_NS,
                           signal_sip_end_ns=(ms + 1) * MS_NS)
                if status == "DETECTED" and _stamp(sip) and sip // MS_NS == ms:
                    row.update(signal_basis="raw_exact", signal_sip_ns=sip)
            else:
                start, end = detection.get("start_ms"), detection.get("end_ms")
                if _stamp(start) and _stamp(end) and end - start == 1000:
                    row.update(signal_basis="bucket_sufficient", signal_sip_start_ns=start * MS_NS,
                               signal_sip_end_ns=end * MS_NS)
        _trade_time(row, summary, event, "fill", status)
        _trade_time(row, summary, event, "exit", status)
        result.append(row)
    return tuple(result)
