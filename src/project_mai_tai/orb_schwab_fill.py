"""ORBFILL1: session-local, read-only Schwab supplementation for ORB MACD."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from project_mai_tai.strategy_core.orb_intrabar import OrbBar

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
MINUTE = timedelta(minutes=1)


def warn_refusal(symbol, reason):
    logger.warning("[ORB-FILL1] symbol=%s result=skipped reason=%s", symbol, reason)


def fill_window(evaluated_at: datetime) -> tuple[datetime, datetime, datetime]:
    local = evaluated_at.astimezone(ET)
    start = local.replace(hour=8, minute=30, second=0, microsecond=0).astimezone(UTC)
    cutoff = evaluated_at.astimezone(UTC).replace(second=0, microsecond=0) - MINUTE
    deadline = local.replace(hour=9, minute=29, second=30, microsecond=0).astimezone(UTC)
    return start, cutoff, deadline


def merge_macd_fill(saved: list[OrbBar], candles: list[dict], start: datetime,
                    cutoff: datetime) -> list[OrbBar]:
    """Saved closes win; quiet minutes carry forward, never backward-fill a seed."""
    prices = {}
    for candle in candles:
        stamp = candle["datetime"]
        close = float(candle["close"])
        if isinstance(stamp, bool) or not isinstance(stamp, int) or stamp % 60_000:
            raise ValueError("invalid Schwab minute")
        time = datetime.fromtimestamp(stamp / 1000, UTC)
        if not start <= time <= cutoff:
            continue
        if not isfinite(close) or close <= 0 or time in prices:
            raise ValueError("invalid or duplicate Schwab close")
        prices[time] = close
    for bar in saved:
        if start <= bar.timestamp <= cutoff:
            prices[bar.timestamp] = bar.close
    result = [bar for bar in saved if bar.timestamp < start]
    last = result[-1].close if result else None
    time = start
    while time <= cutoff:
        if time in prices:
            last = prices[time]
        if last is not None:
            result.append(OrbBar(timestamp=time, open=last, high=last, low=last,
                                 close=last, volume=0))
        time += MINUTE
    return result


class OrbSchwabMacdFill:
    """One bounded HTTP attempt per symbol/day. Never refresh tokens or write bars.

    Called only inside the existing ORB decision worker, not the quote/tick path.
    Failed attempts are retained too; a delayed worker cannot trigger a retry.
    """

    def __init__(self, settings, *, clock=None, fetch=None):
        self.settings = settings
        self.clock = clock or (lambda: datetime.now(UTC))
        self.fetch = fetch or self._fetch
        self._attempts = {}
        self._day = None
        self._lock = Lock()

    def supplement(self, symbol, saved, evaluated_at):
        start, cutoff, deadline = fill_window(evaluated_at)
        if evaluated_at >= deadline or self.clock() >= deadline or cutoff < start:
            warn_refusal(symbol, "fill_deadline")
            raise ValueError("fill_deadline")
        key = symbol, start
        with self._lock:
            if self._day != start.date():
                self._attempts.clear()
                self._day = start.date()
            if key not in self._attempts:
                # Reserve the attempt before touching HTTP, including failures.
                self._attempts[key] = None
                try:
                    timeout = min(2.0, (deadline - self.clock()).total_seconds())
                    if timeout <= 0:
                        raise ValueError("fill_deadline")
                    payload = self.fetch(symbol, start, cutoff, timeout)
                    if (not isinstance(payload, dict) or payload.get("symbol") != symbol
                            or not isinstance(payload.get("candles"), list)
                            or not payload["candles"] or payload.get("empty") is not False
                            or any(payload.get(k) for k in ("next", "nextToken", "truncated"))):
                        raise ValueError("unproven_schwab_fill")
                    # Validate before caching. Never adopt a malformed response.
                    if not merge_macd_fill([], payload["candles"], start, cutoff):
                        warn_refusal(symbol, "empty_scoped_schwab_fill")
                        raise ValueError("empty_scoped_schwab_fill")
                    self._attempts[key] = (cutoff, payload["candles"])
                except Exception as exc:
                    warn_refusal(symbol, "fetch_timeout" if isinstance(exc, TimeoutError)
                                 else "fetch_failed")
            retained = self._attempts[key]
        if retained is None:
            warn_refusal(symbol, "schwab_fill_unavailable")
            raise ValueError("schwab_fill_unavailable")
        if self.clock() >= deadline:
            warn_refusal(symbol, "fill_deadline")
            raise ValueError("fill_deadline")
        fetched_cutoff, candles = retained
        saved_times = {bar.timestamp for bar in saved}
        time = fetched_cutoff + MINUTE
        while time <= cutoff:
            if time not in saved_times:
                warn_refusal(symbol, "cached_cutoff_unproven")
                raise ValueError("cached_cutoff_unproven")
            time += MINUTE
        # Out-of-window response candles cannot become evidence in a later check.
        bounded = [c for c in candles if c["datetime"] <= int(fetched_cutoff.timestamp() * 1000)]
        return merge_macd_fill(saved, bounded, start, cutoff)

    def _fetch(self, symbol, start, cutoff, timeout):
        document = json.loads(Path(self.settings.schwab_token_store_path).expanduser().read_text())
        token = document.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("missing_access_token")
        params = urlencode({"symbol": symbol, "periodType": "day", "frequencyType": "minute",
                            "frequency": 1, "startDate": int(start.timestamp() * 1000),
                            "endDate": int((cutoff + MINUTE).timestamp() * 1000) - 1,
                            "needExtendedHoursData": "true"})
        request = Request(self.settings.schwab_base_url.rstrip("/") +
                          "/marketdata/v1/pricehistory?" + params,
                          headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read(1_048_577))
