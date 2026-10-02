"""Read-only replication of qtime.py, retaining the cohort and quote evidence.

Run via stdin on the box. Credentials stay there; no broker/Redis write is used.
Each HTTP response is bounded to 4 MiB. Pagination or an incomplete response is
UNMEASURED, never classified as an unchanged quote.
"""
import collections
import datetime as dt
import glob
import gzip
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def main():
    cutoff = os.stat("/tmp/qtime.py").st_mtime
    key = None
    with open("/etc/project-mai-tai/project-mai-tai.env") as handle:
        for line in handle:
            if line.startswith("MAI_TAI_MASSIVE_API_KEY="):
                key = line.strip().split("=", 1)[1].strip("\"'")
    if not key:
        raise SystemExit("UNMEASURED: Massive credential unavailable")
    pattern = re.compile(
        r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d+) .*"
        r"symbol=(\S+) account=live:schwab_1m_v2 .*reason=stale_quote.*quote_age_ms=(\d+)"
    )
    rows = []
    for path in sorted(glob.glob("/var/log/project-mai-tai/schwab-1m-v2.log*")):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as handle:
            for number, line in enumerate(handle, 1):
                if "STOP-ASK-PRICE-CHECK" not in line:
                    continue
                match = pattern.match(line)
                if not match:
                    continue
                stamp = dt.datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=dt.timezone.utc
                ).timestamp() + int(match[2]) / 1000
                age = int(match[4])
                if stamp <= cutoff and age <= 60_000:
                    rows.append(dict(check=stamp, symbol=match[3], age_ms=age,
                                     path=path, line=number))
    # Deliberately reproduce the reviewer's sorted-file order, not a new cohort.
    cohort = rows[-120:]
    counts = collections.Counter()
    exact_counts = collections.Counter()
    evidence = []
    for index, row in enumerate(cohort):
        quote_time = row["check"] - row["age_ms"] / 1000
        params = urllib.parse.urlencode({
            "timestamp.gte": int((quote_time - 60) * 1e9),
            "timestamp.lte": int(row["check"] * 1e9),
            "order": "asc", "limit": 50_000, "sort": "timestamp", "apiKey": key,
        })
        try:
            request = f"https://api.polygon.io/v3/quotes/{row['symbol']}?{params}"
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("response_exceeds_4MiB")
            body = json.loads(raw)
            if body.get("next_url") or body.get("status") not in {"OK", "DELAYED"}:
                raise ValueError("incomplete_response")
            quotes = [{k: q[k] for k in ("sip_timestamp", "bid_price", "ask_price",
                                        "bid_size", "ask_size") if k in q}
                      for q in body.get("results", [])]
            changes = []
            previous = None
            for quote in quotes:
                prices = (quote["bid_price"], quote["ask_price"])
                if previous != prices:
                    changes.append(quote["sip_timestamp"] / 1e9)
                previous = prices
            for margin, counter, field in ((1, counts, "reviewer_class"),
                                           (0, exact_counts, "zero_margin_class")):
                any_after = any(q["sip_timestamp"] / 1e9 > quote_time + margin
                                for q in quotes)
                price_after = any(stamp > quote_time + margin for stamp in changes)
                classification = ("no_update" if not any_after else
                                  "price_changed" if price_after else "size_only")
                counter[classification] += 1
                row[field] = classification
            row.update(quotes=quotes, response_bytes=len(raw),
                       response_sha256=hashlib.sha256(raw).hexdigest(),
                       prior_quote_present=any(q["sip_timestamp"] / 1e9 <= quote_time
                                               for q in quotes))
        except (urllib.error.URLError, ValueError, KeyError, TimeoutError) as exc:
            row["error"] = type(exc).__name__  # Never print a URL containing credentials.
            counts["UNMEASURED"] += 1
            exact_counts["UNMEASURED"] += 1
        evidence.append(row)
        if (index + 1) % 20 == 0:
            print(f"verified {index + 1}/{len(cohort)}", file=sys.stderr, flush=True)
        time.sleep(0.1)
    print(json.dumps(dict(
        cutoff_utc=dt.datetime.fromtimestamp(cutoff, dt.timezone.utc).isoformat(),
        cohort_selection="sorted log path, source line order, last 120 age<=60000ms before cutoff",
        count=len(cohort), reviewer_one_second_margin=dict(counts),
        exact_zero_margin=dict(exact_counts),
        missing_prior_quote=sum(not row.get("prior_quote_present", False) for row in evidence),
        evidence=evidence,
    ), sort_keys=True))


if __name__ == "__main__":
    main()
