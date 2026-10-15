"""Offline all-print symbol/second envelope, not observed Massive A transport."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, time as dt_time
from decimal import Decimal
import hashlib
import json
import math
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

NS = 1_000_000_000
DAYS = ("2026-09-18", "2026-09-24", "2026-10-02", "2026-10-05", "2026-10-06",
        "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12")
MAX_JSON = 64 * 1024 * 1024
MAX_MANIFEST = 4 * 1024 * 1024


def pause_if_requested(path):
    if path and path.exists():
        print("PAUSED coordinator", flush=True)
        while path.exists():
            time.sleep(.1)
        print("RESUMED coordinator", flush=True)


def digest_file(path, pause=None):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            pause_if_requested(pause)
            chunk = stream.read(4 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def bounded_json(path, bound=MAX_JSON):
    with path.open("rb") as stream:
        raw = stream.read(bound+1)
    if len(raw) > bound:
        raise ValueError(f"input JSON bound: {path}")
    return json.loads(raw)


def window(day, start="04:00"):
    date = datetime.fromisoformat(day).date()
    zone = ZoneInfo("America/New_York")
    beginning = datetime.combine(date, dt_time.fromisoformat(start), zone)
    end = datetime.combine(date, dt_time(9, 30), zone)
    return int(beginning.timestamp())*NS, int(end.timestamp())*NS


def summarize_counts(counts):
    if not counts or any(type(value) is not int or value < 0 for value in counts):
        raise ValueError("invalid observed second grid")
    ordered = sorted(counts)
    peak = ordered[-1]
    return dict(peak_symbols_per_second=peak,
                p999_symbols_per_second=ordered[math.ceil(.999*len(ordered))-1],
                percentile_method="nearest_rank_ceil(0.999*N); ALL verified window seconds",
                seconds=len(counts), seconds_with_observed_prints=sum(value > 0 for value in counts),
                symbol_second_events=sum(counts), peak_second_offsets=[i for i, value in enumerate(counts) if value == peak])


def finish(day, data, start, end, *, partial):
    counts = data["counts_per_second"]
    if len(counts) != (end-start)//NS:
        raise ValueError("source output time grid differs")
    names = data.pop("downloaded_symbols")
    if len(names) != data["symbols_downloaded"] or len(names) != len(set(names)):
        raise ValueError("source denominator mismatch")
    data["downloaded_symbols_sha256"] = hashlib.sha256(
        json.dumps(sorted(names), separators=(",", ":")).encode()).hexdigest()
    data.update(summarize_counts(counts), day=day, evidence_kind="DERIVED_ALL_TAPE_PRINT_SYMBOL_SECOND_LOAD",
                window_start_ns=start, window_end_ns_exclusive=end,
                window_et="04:10-09:30" if partial else "04:00-09:30",
                complete_requested_input_scope=not partial,
                partial_observed_lower_bound=partial,
                physical_market_completeness="UNVERIFIED; observed file/query coverage only",
                eligibility="ALL observed prints; NO condition/symbol/price/Momentum filter",
                transport_rate="UNMEASURED; symbol-second envelope is not actual WS updates/frames")
    if partial:
        data["partial_bound_semantics"] = (
            "lower bound for matched 04:10-09:30 universe/window; p999 not compared to wider 04:00 window"
        )
    sample = data.get("template_actual_print")
    if sample:
        second = sample["sip_ns"]//NS
        data["derived_A_shape_template"] = dict(
            ev="A", sym=sample["symbol"], s=second*1000, e=(second+1)*1000,
            o=sample["price"], h=sample["price"], l=sample["price"], c=sample["price"],
            v=int(Decimal(str(sample["size"]))),
        )
        data["template_semantics"] = "one actual raw print projected to A-shaped stress template, NOT vendor qualifying OHLC"
    for field in ("first_sip_ns", "last_sip_ns", "first_window_sip_ns", "last_window_sip_ns"):
        if data.get(field) == 0:
            data[field] = None
    return data


def flat_day(day, root, binary, pause):
    path = root / "replay/provider-flatfiles" / f"{day}.csv.gz"
    receipt_path = path.with_suffix("").with_suffix(".get.json")
    receipt = bounded_json(receipt_path)["receipts"][0]
    before = path.stat()
    if (receipt.get("complete") is not True or receipt.get("http_status") != 200
            or receipt.get("bytes") != before.st_size or receipt.get("day") != day):
        raise ValueError("full flatfile GET/length receipt incomplete")
    print(json.dumps(dict(day=day, phase="hash_full_download", bytes=before.st_size)), flush=True)
    digest = digest_file(path, pause)
    if digest != receipt["sha256"]:
        raise ValueError("full flatfile source SHA mismatch")
    start, end = window(day)
    print(json.dumps(dict(day=day, phase="all_symbols_gzip_scan")), flush=True)
    command = [str(binary), str(path), str(start), str(end)]
    if pause:
        command.append(str(pause))
    result = subprocess.run(command, stdout=subprocess.PIPE, check=True, timeout=7200)
    if len(result.stdout) > 32*1024*1024:
        raise ValueError("scanner output bound")
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("source changed during scan")
    data = json.loads(result.stdout)
    data.update(source_path=str(path), source_sha256=digest, source_bytes=after.st_size,
                receipt_path=str(receipt_path), receipt_sha256=digest_file(receipt_path, pause),
                denominator="all distinct tickers anywhere in downloaded complete daily raw object",
                requested_universe="entire stocks trades_v1 object, no Momentum eligibility restriction")
    return finish(day, data, start, end, partial=False)


def partial_day(root, pause):
    seal = root / "data/oct12_after16_sealed_partial"
    manifest_path = seal / "retained-input-SHA256SUMS"
    with manifest_path.open("rb") as stream:
        manifest_raw = stream.read(MAX_MANIFEST+1)
    if len(manifest_raw) > MAX_MANIFEST:
        raise ValueError("sealed manifest bound")
    manifest = {}
    for line in manifest_raw.decode().splitlines():
        digest, name = line.split("  ", 1)
        if name in manifest or len(digest) != 64:
            raise ValueError("sealed manifest duplicate/invalid digest")
        manifest[name] = digest
    start, end = window("2026-10-12", "04:10")
    counts = [0]*((end-start)//NS)
    symbols, printed = set(), set()
    source_rows = pm_rows = 0
    first = last = pm_first = pm_last = None
    sample = None
    sources = []
    histories = sorted(Path(name) for name in manifest if name.endswith(".history.jsonl"))
    if not histories or len(histories) > 20000:
        raise ValueError("sealed source file count bound")
    for index, path in enumerate(histories):
        pause_if_requested(pause)
        receipt_path = path.with_suffix("").with_suffix(".receipt.json")
        for item in (path, receipt_path):
            if str(item) not in manifest or digest_file(item, pause) != manifest[str(item)]:
                raise ValueError("sealed partial source/receipt hash mismatch")
        receipt = bounded_json(receipt_path)
        if (receipt.get("complete") is not True or receipt.get("pagination_exhausted") is not True
                or receipt.get("day") != "2026-10-12" or receipt["start_ns"] > start
                or receipt["end_ns"] < end-1 or receipt["sha256"] != manifest[str(path)]):
            raise ValueError("partial requested interval incomplete")
        data = bounded_json(path)
        symbol = data["symbol"]
        if symbol in symbols or symbol != receipt["symbol"] or data.get("complete") is not True:
            raise ValueError("duplicate/invalid sealed symbol")
        symbols.add(symbol)
        rows = data["rows"]
        if len(rows) != receipt["raw_count"]:
            raise ValueError("partial row count differs from receipt")
        source_rows += len(rows)
        seconds = set()
        for ordinal, row in enumerate(rows):
            if ordinal % 8192 == 0:
                pause_if_requested(pause)
            stamp = row[0]
            if type(stamp) is not int or not receipt["start_ns"] <= stamp <= receipt["end_ns"]:
                raise ValueError("partial row outside requested coverage")
            first = stamp if first is None else min(first, stamp)
            last = stamp if last is None else max(last, stamp)
            if start <= stamp < end:
                pm_rows += 1
                printed.add(symbol)
                seconds.add((stamp-start)//NS)
                pm_first = stamp if pm_first is None else min(pm_first, stamp)
                pm_last = stamp if pm_last is None else max(pm_last, stamp)
                if sample is None and Decimal(str(row[1])).is_finite() and Decimal(str(row[1])) > 0:
                    sample = dict(symbol=symbol, sip_ns=stamp, price=str(row[1]), size=row[2])
        for second in seconds:
            counts[second] += 1
        sources.append(dict(path=str(path), sha256=manifest[str(path)], receipt_sha256=manifest[str(receipt_path)]))
        if index % 500 == 0:
            print(json.dumps(dict(day="2026-10-12", phase="sealed_rest_scan", files=index+1)), flush=True)
    summary = bounded_json(seal / "attempt-summary.json")
    if len(symbols) != summary["verified_symbols"] or source_rows != summary["verified_prints"]:
        raise ValueError("sealed union denominator mismatch")
    data = dict(counts_per_second=counts, downloaded_symbols=sorted(symbols), symbols_downloaded=len(symbols),
                window_printed_symbols=len(printed), source_rows_available=source_rows, window_raw_prints=pm_rows,
                first_sip_ns=first, last_sip_ns=last, first_window_sip_ns=pm_first, last_window_sip_ns=pm_last,
                template_actual_print=sample, source_manifest_path=str(manifest_path),
                source_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(), sources=sources,
                eligible_requested_universe=summary["eligible_symbols"],
                missing_eligible_intervals=len(summary["missing_symbols"]),
                denominator="verified completed/reused REST symbol jobs only; empty queried files counted as downloaded",
                requested_universe="MOMRAW eligibility/prior-close restricted universe, NOT whole market",
                unknown_excluded_scope="04:00-04:10; missing jobs; prior-close<1/excluded symbols NOT zero")
    return finish("2026-10-12", data, start, end, partial=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pause-file", type=Path)
    parser.add_argument("--days", nargs="+", choices=DAYS, default=DAYS)
    parser.add_argument("--tested-head", required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    summaries = []
    for day in args.days:
        pause_if_requested(args.pause_file)
        result = partial_day(args.source_root, args.pause_file) if day == "2026-10-12" else flat_day(
            day, args.source_root, args.binary, args.pause_file)
        result.update(tested_head=args.tested_head, scanner_sha256=digest_file(args.binary),
                      coordinator_sha256=digest_file(Path(__file__)))
        path = args.output / f"{day}.counts.json"
        path.write_text(json.dumps(result, separators=(",", ":"), allow_nan=False)+"\n")
        summary = {key: value for key, value in result.items()
                   if key not in {"counts_per_second", "sources", "peak_second_offsets"}}
        summary.update(output_path=str(path), output_sha256=digest_file(path))
        summaries.append(summary)
        print(json.dumps(dict(day=day, phase="COMPLETE", peak=result["peak_symbols_per_second"],
                              p999=result["p999_symbols_per_second"], symbols=result["symbols_downloaded"])), flush=True)
        (args.output / "summary.json").write_text(json.dumps(summaries, indent=2)+"\n")
        with (args.output / "rates.csv").open("w", newline="") as stream:
            columns = ("day", "symbols_downloaded", "window_printed_symbols", "seconds",
                       "peak_symbols_per_second", "p999_symbols_per_second", "window_et",
                       "partial_observed_lower_bound", "requested_universe", "eligibility")
            writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(summaries)


if __name__ == "__main__":
    main()
