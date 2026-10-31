"""Pure C1 15:36/15:41 line, route and sizing boundary; no I/O or live hooks."""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import hashlib
import math
import re
from zoneinfo import ZoneInfo


ET = ZoneInfo("America/New_York")
MINUTE = 60000000000
REFERENCES = {"2026-10-28": (("ELPW", "09:02"), ("POM", "10:51"), ("POM", "11:44"),
                              ("CHSN", "12:20"), ("CHSN", "13:31"), ("FTFT", "15:39"))}
# C1 20:09:08 corrected its human reference. Historical raw5/6 remains valid
# history; 15:32 was replacement of a waiting order, not a completed-bar flip.
SUPERSEDED_REFERENCES = {"2026-10-28": (("FTFT", "15:32"),)}
ROUTE = re.compile(r"\[ATR-COUNTRY-SELECTED\] symbol=([A-Z][A-Z0-9.\-]{0,15}) "
                   r"segment=(\d+) route=(schwab|webull) reason=(\S+) status=(\S+) "
                   r"as_of=(\S+) notional=(600|300)(?:\s|$)")


def recompute(bars, *, nanos):
    from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail

    ordered = sorted(bars, key=lambda row: (nanos(row["bar_time"]), str(row["id"])))
    starts = [nanos(row["bar_time"]) for row in ordered]
    if any(value % MINUTE for value in starts) or len(set(starts)) != len(starts):
        raise ValueError("unique_ascending_minute_bars_required")
    arguments = []
    for row, start in zip(ordered, starts):
        values = [Decimal(str(row[key])) for key in ("open_price", "high_price", "low_price", "close_price")]
        if (any(not value.is_finite() or value <= 0 for value in values)
                or type(row["volume"]) is not int or row["volume"] < 0):
            raise ValueError("finite_positive_ohlcv_required")
        arguments.append(Bar(start // 1000000, *(float(value) for value in values), row["volume"]))
    rows = compute_atr_trail(arguments, seed="sma5", period=5, factor=3.5)
    return [dict(original=row, oracle=result, start_ns=start, available_ns=start + MINUTE,
                 source_created_at=row["created_at"],
                 label="line recomputed from stored bars",
                 clock="DERIVED completed-minute simulation clock; native arrival UNKNOWN")
            for row, result, start in zip(ordered, rows, starts)]


def parity(day, symbol, lines, *, asof_ns, nanos):
    refs = REFERENCES.get(day)
    if refs is None:
        return dict(status="UNKNOWN", expected=None, checked=0,
                    issues=["UNKNOWN_C1_REFERENCES_FOR_DAY"], comparisons=[])
    comparisons = []
    for name, label in refs:
        if name != symbol:
            continue
        expected = nanos(datetime.fromisoformat(day + "T" + label).replace(tzinfo=ET))
        if day == '2026-10-28' and symbol == 'FTFT':
            def at(clock):
                return nanos(datetime.fromisoformat(day + 'T' + clock).replace(tzinfo=ET))
            buys = [row for row in lines if row['oracle']['flip'] == 'BUY'
                    and row['available_ns'] <= asof_ns]
            candidates = [row for row in buys if at('15:38') <= row['start_ns'] <= at('15:40')]
            forbidden = [row for row in buys if at('14:37') <= row['start_ns'] <= at('14:39')]
            status = ('UNKNOWN_PENDING_REFERENCE' if asof_ns < at('15:41') else
                      'MATCH' if candidates and not forbidden else 'MISMATCH')
            fact = lambda row: dict(bar_start_ns=row['start_ns'], available_ns=row['available_ns'],
                                    bar_id=str(row['original']['id']))
            comparisons.append(dict(symbol=name, reference=label, reference_direction='BUY',
                reference_authority='C1 corrected 2026-10-28 20:09:08; not independent native verification',
                reference_ns=expected, status=status, matches=[fact(row) for row in candidates],
                superseded_reference='15:32', corrected_rule='BUY bar15:38-15:40 AND no BUY bar14:37-14:39',
                forbidden_matches=[fact(row) for row in forbidden]))
            continue
        candidates = [row for row in lines if row["oracle"]["flip"] == "BUY"
                      and abs(row["available_ns"] - expected) <= MINUTE]
        status = "MATCH" if candidates else "UNKNOWN_PENDING_REFERENCE" if expected + MINUTE > asof_ns else "MISMATCH"
        comparisons.append(dict(symbol=name, reference=label, reference_direction="BUY",
            reference_authority="C1-reported; not independent native verification",
            reference_ns=expected, status=status,
            matches=[dict(bar_start_ns=row["start_ns"], available_ns=row["available_ns"],
                          bar_id=str(row["original"]["id"])) for row in candidates]))
    return dict(status="REPORTED", expected=len([item for item in refs if item[0] == symbol]),
                checked=len(comparisons), comparisons=comparisons)


def parity_summary(day, reports):
    expected = REFERENCES.get(day)
    comparisons = [item for report in reports for item in report["comparisons"]]
    found = {(item["symbol"], item["reference"]) for item in comparisons}
    if len(found) != len(comparisons):
        raise ValueError("duplicate_original_C1_reference_comparison")
    if expected is not None:
        comparisons.extend(dict(symbol=symbol, reference=clock, reference_direction="BUY",
                                status="UNKNOWN_REFERENCE_SOURCE_ABSENT", matches=[])
                           for symbol, clock in expected if (symbol, clock) not in found)
    mismatches = sum(item["status"] == "MISMATCH" for item in comparisons)
    unresolved = sum(item["status"].startswith("UNKNOWN") for item in comparisons)
    # C1 18:01 Oct28 supersedes the earlier one-mismatch allowance. A miss is
    # evidence for C1 to rule on; it never grants automatic qualification.
    status = ("UNKNOWN" if expected is None or unresolved else
              "BLOCKED" if mismatches else "ALL_C1_BUY_REFERENCES_MATCH")
    return dict(status=status, expected=None if expected is None else len(expected),
                mismatches=mismatches, unresolved=unresolved, comparisons=comparisons,
                blocks_qualification=expected is None or unresolved > 0 or mismatches > 0)


def route_fact(packet, *, day, symbol, asof_ns, nanos):
    unknown = dict(status="UNKNOWN", broker=None, notional=None,
                   issues=["UNKNOWN_ACTUAL_DAY_COUNTRY_ROUTE"])
    if packet is None:
        return unknown
    try:
        source = packet["source"]
        if (packet["schema"] != "ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1"
                or packet["trading_day"] != day or nanos(source["asof"]) > asof_ns
                or not isinstance(source["artifact"], str) or not source["artifact"]
                or not source["artifact"].startswith("/")
                or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
                or not isinstance(source.get("epoch"), (str, dict)) or not source["epoch"]
                or not isinstance(packet["lines"], list) or len(packet["lines"]) > 30000):
            return unknown
        facts = []
        for row in packet["lines"]:
            text = row["text"]
            if (not isinstance(text, str) or len(text) > 4096
                    or "\n" in text.rstrip("\r\n") or "\r" in text.rstrip("\r\n")
                    or text.count("[ATR-COUNTRY-SELECTED]") != 1):
                return unknown
            match = ROUTE.search(text)
            if match is None or match[1] != symbol:
                continue
            name, segment, broker, reason, status, country_asof, notional = match.groups()
            segment_ns = int(segment) * 1000000
            if (datetime.fromtimestamp(segment_ns // 1000000000, timezone.utc).astimezone(ET).date().isoformat() != day
                    or segment_ns > asof_ns or nanos(row["recorded_at"]) > asof_ns
                    or type(row["line_number"]) is not int or row["line_number"] <= 0
                    or nanos(row["recorded_at"]) > nanos(source["asof"])
                    or segment_ns > nanos(row["recorded_at"])
                    or notional != ("600" if broker == "schwab" else "300")
                    or broker == "schwab" and (reason != "sec_us" or status != "READY" or country_asof == "UNKNOWN")):
                return unknown
            if broker == "schwab" and nanos(country_asof) > nanos(row["recorded_at"]):
                return unknown
            facts.append(dict(symbol=name, segment=segment, broker=broker, notional=notional,
                              reason=reason, status=status, country_asof=country_asof,
                              line_number=row["line_number"], recorded_at=row["recorded_at"],
                              selected_text_sha256=hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()))
        if not facts:
            return unknown
        if len({(row["broker"], row["notional"]) for row in facts}) != 1:
            return dict(unknown, issues=["UNKNOWN_CONFLICTING_DAY_COUNTRY_ROUTES"])
        return dict(status="RECORDED_DAY_ROUTE", broker=facts[0]["broker"],
                    notional=facts[0]["notional"], facts=facts, source=source,
                    historical_native_asof="UNKNOWN", issues=[])
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return unknown


def size(route, entry_price):
    price = Decimal(str(entry_price))
    if not price.is_finite() or price <= 0:
        raise ValueError("positive_finite_sim_entry_price_required")
    if route.get("status") != "RECORDED_DAY_ROUTE":
        return dict(quantity=1, usd_known=False, notional=None,
                    sizing_status="UNKNOWN_UNITS_NORMALIZED_ONE_UNIT_MODEL",
                    percent_basis="normalized price return; modeled one unit, route and actual units unresolved")
    notional = Decimal(route["notional"])
    quantity = int((notional / price).to_integral_value(rounding=ROUND_FLOOR))
    return dict(quantity=quantity, usd_known=True, notional=str(notional),
                sizing_status="RECORDED_DAY_ROUTE_NOTIONAL_" + str(notional) + "_USD",
                percent_basis="sized modeled allocation return on actual modeled entry cost")


def percent(fills, exits):
    if not fills or not exits or any(row.get("price") is None for row in exits):
        return None
    if any(type(row.get("quantity")) is not int or row["quantity"] <= 0
           or not Decimal(str(row["price"])).is_finite() or Decimal(str(row["price"])) <= 0
           for row in (*fills, *exits)):
        return None
    cost = sum((Decimal(str(row["price"])) * row["quantity"] for row in fills), Decimal(0))
    if cost <= 0 or sum(row["quantity"] for row in fills) != sum(row["quantity"] for row in exits):
        return None
    revenue = sum((Decimal(str(row["price"])) * row["quantity"] for row in exits), Decimal(0))
    value = (revenue - cost) / cost * 100
    return value if value.is_finite() and math.isfinite(float(value)) else None
