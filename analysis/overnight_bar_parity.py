"""Phase A — Bar parity: schwab_1m_v2 stored bars vs Schwab pricehistory.

Compares the bars schwab_1m_v2 persisted to ``strategy_bar_history`` (today's
store is ~99% streamer-built — see persist-lag split) against Schwab's
``pricehistory`` 1-minute candles (the same vendor data TOS renders). The
streamer-built population is the one that can diverge from vendor data, so this
is effectively a bar-ASSEMBLY check on the v2 streamer.

READ-ONLY. No writes, no service interaction beyond authenticated GETs.

Bucket convention (confirmed from code, not assumed):
- ``schwab_v2_rest_client.ChartBar.timestamp_ms`` = pricehistory candle
  ``datetime`` = bar-START, epoch-ms UTC.
- ``schwab_1m_v2_bot._persist_bar`` writes ``bar_time =
  datetime.fromtimestamp(timestamp_ms/1000, UTC)``.
So ``strategy_bar_history.bar_time`` and the pricehistory candle key are the
same instant; alignment is exact by construction for REST bars, and any
off-by-one-minute on streamer bars is a real finding.

Invocation (on the VPS, env sourced for token + DSN):
  sudo bash -c 'set -a; source <(grep -E "^MAI_TAI_" \
      /etc/project-mai-tai/project-mai-tai.env); set +a; \
    PYTHONPATH=/home/trader/project-mai-tai/src \
    /home/trader/project-mai-tai/.venv/bin/python \
    analysis/overnight_bar_parity.py --day 2026-06-11 --out /tmp/phaseA.json'

Re-runnable on any day within Schwab pricehistory reach (intraday minute data is
served for ~the trailing few weeks).
"""
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import psycopg

from project_mai_tai.market_data.schwab_v2_rest_client import SchwabV2RestClient
from project_mai_tai.settings import Settings

STRATEGY_CODE = "schwab_1m_v2"
INTERVAL_SECS = 60

# Flag thresholds (from the audit spec).
CLOSE_ABS_TOL = 0.001          # $
CLOSE_PCT_TOL = 0.0005         # 0.05%
OHLC_FIELDS = ("open", "high", "low", "close")
VOLUME_PCT_TOL = 0.02          # 2% (late prints make small drift legitimate)


@dataclass
class Bar:
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int


def _dsn() -> str:
    raw = os.environ.get("MAI_TAI_DATABASE_URL", "")
    if not raw:
        raise SystemExit("MAI_TAI_DATABASE_URL not in env — source the service env first")
    # psycopg.connect wants a libpq URL, not the SQLAlchemy +psycopg variant.
    return raw.replace("postgresql+psycopg://", "postgresql://")


def load_stored_bars(day: str, rth_start: str, rth_end: str) -> dict[str, dict[int, Bar]]:
    """{symbol: {ts_ms: Bar}} for v2's stored bars in the RTH window."""
    start = f"{day} {rth_start}:00+00"
    end = f"{day} {rth_end}:00+00"
    out: dict[str, dict[int, Bar]] = {}
    with psycopg.connect(_dsn()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT symbol,
                   EXTRACT(EPOCH FROM bar_time) * 1000,
                   open_price, high_price, low_price, close_price, volume
            FROM strategy_bar_history
            WHERE strategy_code = %s AND interval_secs = %s
              AND bar_time >= %s AND bar_time < %s
            ORDER BY symbol, bar_time
            """,
            (STRATEGY_CODE, INTERVAL_SECS, start, end),
        )
        for sym, ts_ms, o, h, low, c, v in cur.fetchall():
            out.setdefault(sym, {})[int(ts_ms)] = Bar(
                int(ts_ms), float(o), float(h), float(low), float(c), int(v)
            )
    return out


def fetch_pricehistory_day(
    client: SchwabV2RestClient, settings: Settings, symbol: str, day: str
) -> dict[int, Bar]:
    """Full-day 1m candles for `symbol` via pricehistory, keyed by ts_ms.

    Explicit startDate/endDate for the target day (the periodType=day&period=1
    gotcha returns the prior session — see schwab_v2_rest_client). Includes
    extended hours so RTH is fully covered.
    """
    day_dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    start_ms = int(day_dt.timestamp() * 1000)
    end_ms = int((day_dt + timedelta(days=1)).timestamp() * 1000)
    params = urlencode(
        {
            "symbol": symbol,
            "periodType": "day",
            "frequencyType": "minute",
            "frequency": 1,
            "startDate": start_ms,
            "endDate": end_ms,
            "needExtendedHoursData": "true",
        }
    )
    url = f"{settings.schwab_base_url.rstrip('/')}{client.PRICE_HISTORY_PATH}?{params}"
    payload = client._authorized_get(url)  # noqa: SLF001 — reuse the real auth path
    candles = payload.get("candles")
    out: dict[int, Bar] = {}
    if isinstance(candles, list):
        for c in candles:
            if not isinstance(c, dict):
                continue
            try:
                ts = int(c.get("datetime", 0) or 0)
            except (TypeError, ValueError):
                continue
            out[ts] = Bar(
                ts,
                float(c.get("open", 0.0) or 0.0),
                float(c.get("high", 0.0) or 0.0),
                float(c.get("low", 0.0) or 0.0),
                float(c.get("close", 0.0) or 0.0),
                int(float(c.get("volume", 0) or 0)),
            )
    return out


def _close_flagged(a: float, b: float) -> bool:
    if abs(a - b) <= CLOSE_ABS_TOL:
        return False
    denom = abs(b) if b else 1.0
    return abs(a - b) / denom > CLOSE_PCT_TOL


def _ohlc_flagged(store: Bar, vendor: Bar) -> list[str]:
    flags = []
    for f in OHLC_FIELDS:
        if _close_flagged(getattr(store, f), getattr(vendor, f)):
            flags.append(f)
    return flags


def _vol_flagged(store: Bar, vendor: Bar) -> bool:
    if vendor.volume == 0:
        return store.volume != 0
    return abs(store.volume - vendor.volume) / vendor.volume > VOLUME_PCT_TOL


def compare_symbol(
    symbol: str, store: dict[int, Bar], vendor: dict[int, Bar], rth_end_ms: int
) -> dict:
    store_ts = set(store)
    vendor_ts = set(vendor)
    common = sorted(store_ts & vendor_ts)
    missing = sorted(vendor_ts - store_ts)   # in vendor, not in store
    extra = sorted(store_ts - vendor_ts)     # in store, not in vendor (assembly risk)

    # Split "missing" by whether the symbol was actually subscribed at that
    # minute. v2's watchlist rotates intraday, so vendor minutes before the
    # first / after the last stored bar are "not watched", NOT a gap. Only
    # missing minutes INSIDE [first_store, last_store] are real coverage gaps.
    # Of those, vendor-volume==0 minutes are legitimate persist-skips
    # (_persist_bar returns early when volume==0). vendor-volume>0 in-window
    # missing = a real bar the streamer/store dropped (the finding to surface).
    sub_lo = min(store_ts) if store_ts else 0
    sub_hi = max(store_ts) if store_ts else 0
    in_window_missing = sorted(t for t in missing if sub_lo <= t <= sub_hi)
    not_watched_missing = len(missing) - len(in_window_missing)

    # v2's watchlist rotates intraday (scanner fade-out / re-confirm), so a
    # symbol's stored bars can have long de-subscription blocks INSIDE
    # [first,last]. Group in-window missing minutes into consecutive runs:
    #   - long runs (> SHORT_RUN_MAX) = de-subscription (expected; not a fault)
    #   - short runs (<= SHORT_RUN_MAX) bordered by stored bars = candidate
    #     real streamer drops / no-print blips. Of those, vendor-vol>0 minutes
    #     are the only true fidelity finding (the streamer missed a printing
    #     minute it was subscribed to). vendor-vol==0 = legit persist-skip.
    SHORT_RUN_MAX = 3
    runs: list[list[int]] = []
    for t in in_window_missing:
        if runs and t - runs[-1][-1] == 60_000:
            runs[-1].append(t)
        else:
            runs.append([t])
    short_run_min = [t for r in runs if len(r) <= SHORT_RUN_MAX for t in r]
    desub_block_min = [t for r in runs if len(r) > SHORT_RUN_MAX for t in r]
    miss_zero_vol = [t for t in in_window_missing if vendor[t].volume == 0]
    real_gap = [t for t in short_run_min if vendor[t].volume > 0]   # the finding
    longest_desub = max((len(r) for r in runs if len(r) > SHORT_RUN_MAX), default=0)

    exact = within_tol = ohlc_flag = vol_flag = 0
    flagged_bars = []
    for ts in common:
        s, v = store[ts], vendor[ts]
        oflags = _ohlc_flagged(s, v)
        vflag = _vol_flagged(s, v)
        if not oflags and s.volume == v.volume:
            exact += 1
            continue
        if not oflags and not vflag:
            within_tol += 1
            continue
        if oflags:
            ohlc_flag += 1
        if vflag:
            vol_flag += 1
        iso = datetime.fromtimestamp(ts / 1000, UTC).strftime("%H:%M")
        flagged_bars.append(
            {
                "ts_utc": iso,
                "fields": oflags + (["volume"] if vflag else []),
                "store": asdict(s),
                "vendor": asdict(v),
            }
        )

    # Off-by-one-minute bucketing probe: a store bar at T whose OHLC matches a
    # vendor bar at T±60s (and T itself is missing from vendor) = misalignment.
    misaligned = []
    for ts in extra:
        s = store[ts]
        for shift in (-60_000, 60_000):
            v = vendor.get(ts + shift)
            if v and not _ohlc_flagged(s, v):
                misaligned.append(
                    {"store_ts": datetime.fromtimestamp(ts / 1000, UTC).strftime("%H:%M"),
                     "matches_vendor_ts": datetime.fromtimestamp((ts + shift) / 1000, UTC).strftime("%H:%M")}
                )
                break

    # The in-flight last bar at fetch time is a legitimate store/vendor diff.
    # After close (RTH end in the past) there should be none; flag if extra
    # bars sit only at the very tail.
    extra_after_rth_end = [t for t in extra if t >= rth_end_ms - 60_000]

    n = len(common)
    ohlc_flag_rate = (ohlc_flag / n) if n else 0.0
    real_gap_rate = len(real_gap) / n if n else 0.0

    # Two axes, kept distinct (the key lesson of this audit):
    #  ASSEMBLY FIDELITY (are the stored bar VALUES correct?) — this is the
    #    data-quality axis the spec's "bar-ASSEMBLY bug" warning targets. FAIL
    #    only on real corruption: timestamp misalignment OR OHLC divergence.
    #  COVERAGE (did v2 store every printing minute it was subscribed to?) — a
    #    separate axis. Gaps here are dominated by watchlist rotation: long
    #    de-subscription blocks (symbol faded out) + isolated short gaps at
    #    fade/re-confirm boundaries. These are NOT bad data; the bars present
    #    are still exact. A high isolated-gap rate is flagged for subscription-
    #    timeline verification, but does NOT make the bar DATA untrustworthy.
    if misaligned or ohlc_flag_rate > 0.02:
        verdict = "FAIL"               # genuine bar-value corruption (none seen)
    elif ohlc_flag or vol_flag or real_gap or miss_zero_vol or desub_block_min or not_watched_missing or extra:
        verdict = "PASS-with-notes"
    else:
        verdict = "PASS"
    coverage_flag = (
        "HIGH-GAP-VERIFY" if real_gap_rate > 0.05
        else "gaps" if (real_gap or desub_block_min)
        else "full"
    )

    return {
        "symbol": symbol,
        "verdict": verdict,
        "coverage_flag": coverage_flag,
        "compared": n,
        "exact": exact,
        "within_tol": within_tol,
        "ohlc_flagged": ohlc_flag,
        "vol_flagged": vol_flag,
        "ohlc_flag_rate": round(ohlc_flag_rate, 4),
        "missing_total": len(missing),
        "missing_not_watched": not_watched_missing,
        "desub_block_minutes": len(desub_block_min),
        "longest_desub_run": longest_desub,
        "missing_zerovol_noprint": len(miss_zero_vol),
        "real_gap_count": len(real_gap),
        "real_gap_rate": round(real_gap_rate, 4),
        "real_gap_minutes_utc": [datetime.fromtimestamp(t/1000, UTC).strftime("%H:%M")
                                 for t in real_gap[:30]],
        "extra_in_store": len(extra),
        "extra_at_tail": len(extra_after_rth_end),
        "misaligned": misaligned,
        "flagged_bars": flagged_bars[:40],
        "flagged_bars_total": len(flagged_bars),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--rth-start", default="13:30")
    ap.add_argument("--rth-end", default="20:00")
    ap.add_argument("--symbols", default="", help="CSV; default = all v2-stored symbols that day")
    ap.add_argument("--min-bars", type=int, default=30, help="skip symbols with fewer stored RTH bars")
    ap.add_argument("--out", default="/tmp/phaseA.json")
    args = ap.parse_args()

    settings = Settings()
    client = SchwabV2RestClient(settings, on_chart_bar=lambda *a: None, on_quote=lambda *a: None)
    if not client.configured:
        raise SystemExit("Schwab token store not configured — source the service env")

    stored = load_stored_bars(args.day, args.rth_start, args.rth_end)
    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols
        else sorted(stored, key=lambda s: -len(stored[s]))
    )
    rth_end_ms = int(
        datetime.strptime(f"{args.day} {args.rth_end}", "%Y-%m-%d %H:%M")
        .replace(tzinfo=UTC).timestamp() * 1000
    )

    results = []
    skipped = []
    for sym in symbols:
        sbars = stored.get(sym, {})
        if len(sbars) < args.min_bars:
            skipped.append({"symbol": sym, "stored_bars": len(sbars)})
            continue
        try:
            vbars_all = fetch_pricehistory_day(client, settings, sym, args.day)
        except Exception as exc:  # noqa: BLE001
            results.append({"symbol": sym, "verdict": "ERROR", "error": str(exc)})
            continue
        # Restrict vendor bars to the same RTH window as the store query.
        rth_start_ms = int(
            datetime.strptime(f"{args.day} {args.rth_start}", "%Y-%m-%d %H:%M")
            .replace(tzinfo=UTC).timestamp() * 1000
        )
        vbars = {t: b for t, b in vbars_all.items() if rth_start_ms <= t < rth_end_ms}
        results.append(compare_symbol(sym, sbars, vbars, rth_end_ms))

    assembly_clean = [r["symbol"] for r in results
                      if r.get("verdict") not in ("ERROR",) and not r.get("misaligned")
                      and r.get("ohlc_flagged", 0) == 0 and r.get("vol_flagged", 0) == 0]
    high_gap = [r["symbol"] for r in results if r.get("coverage_flag") == "HIGH-GAP-VERIFY"]
    summary = {
        "day": args.day,
        "rth": f"{args.rth_start}-{args.rth_end} UTC",
        "symbols_compared": len([r for r in results if r.get("verdict") not in ("ERROR",)]),
        "PASS": [r["symbol"] for r in results if r.get("verdict") == "PASS"],
        "PASS_with_notes": [r["symbol"] for r in results if r.get("verdict") == "PASS-with-notes"],
        "FAIL": [r["symbol"] for r in results if r.get("verdict") == "FAIL"],
        "ERROR": [r["symbol"] for r in results if r.get("verdict") == "ERROR"],
        "assembly_exact_symbols": assembly_clean,
        "coverage_high_gap_verify": high_gap,
        "skipped_low_bar_count": skipped,
        "results": results,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)

    print(f"Phase A — {args.day} RTH {args.rth_start}-{args.rth_end} UTC")
    print(f"  symbols compared: {summary['symbols_compared']}")
    print(f"  verdict: PASS={len(summary['PASS'])}  PASS-with-notes={len(summary['PASS_with_notes'])}"
          f"  FAIL={len(summary['FAIL'])}  ERROR={len(summary['ERROR'])}")
    print(f"  ASSEMBLY exact (0 OHLC/vol/misalign): {len(assembly_clean)}/{summary['symbols_compared']}")
    print(f"  COVERAGE high-gap (verify vs subscription timeline): {high_gap or 'none'}")
    for r in results:
        if r.get("verdict") == "ERROR":
            print(f"  {r['symbol']:6} ERROR {r['error']}")
            continue
        print(f"  {r['symbol']:6} {r['verdict']:16} cov={r['coverage_flag']:16} cmp={r['compared']:4} "
              f"exact={r['exact']:4} ohlcFlag={r['ohlc_flagged']:3} realGap={r['real_gap_count']:3} "
              f"desubBlk={r['desub_block_minutes']:3}(max{r['longest_desub_run']:3}) "
              f"notWatched={r['missing_not_watched']:3} misalign={len(r['misaligned'])}")
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
