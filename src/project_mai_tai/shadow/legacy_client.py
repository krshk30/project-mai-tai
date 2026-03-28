from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def utcnow() -> datetime:
    return datetime.now(UTC)


class LegacyShadowClient:
    BOT_ENDPOINTS = {
        "macd_30s": "/bot",
        "macd_1m": "/bot1m",
        "tos": "/tosbot",
        "runner": "/runnerbot",
    }

    def __init__(self, base_url: str, *, timeout_seconds: int = 3) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def fetch_snapshot(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._fetch_snapshot_sync)

    def _fetch_snapshot_sync(self) -> dict[str, Any]:
        if not self.base_url:
            return {
                "enabled": False,
                "connected": False,
                "fetched_at": utcnow().isoformat(),
                "scanner": {"confirmed_symbols": []},
                "bots": {},
                "errors": ["legacy base url not configured"],
            }

        errors: list[str] = []
        scanner_payload = self._safe_get_json("/scanner/confirmed", errors)
        bots: dict[str, Any] = {}
        for strategy_code, endpoint in self.BOT_ENDPOINTS.items():
            bot_payload = self._safe_get_json(endpoint, errors)
            bots[strategy_code] = self._normalize_bot_state(bot_payload)

        return {
            "enabled": True,
            "connected": len(errors) < len(self.BOT_ENDPOINTS) + 1,
            "fetched_at": utcnow().isoformat(),
            "scanner": {
                "confirmed_symbols": sorted(
                    {
                        str(stock.get("ticker", "")).upper()
                        for stock in scanner_payload.get("stocks", [])
                        if stock.get("ticker")
                    }
                ),
                "count": int(scanner_payload.get("count", 0) or 0),
            },
            "bots": bots,
            "errors": errors,
        }

    def _safe_get_json(self, path: str, errors: list[str]) -> dict[str, Any]:
        try:
            return self._get_json(path)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{path}: {exc}")
            return {}

    def _get_json(self, path: str) -> dict[str, Any]:
        url = urljoin(f"{self.base_url}/", path.lstrip("/"))
        request = Request(url, headers={"Accept": "application/json"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("utf-8")
        data = json.loads(body)
        if isinstance(data, dict):
            return data
        raise ValueError(f"Expected object response from {url}")

    def _normalize_bot_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        watched = sorted(
            {
                str(symbol).upper()
                for symbol in payload.get("watched_tickers", [])
                if symbol
            }
        )

        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            position = payload.get("position")
            raw_positions = [position] if isinstance(position, dict) else []

        positions: list[dict[str, Any]] = []
        for item in raw_positions:
            symbol = str(item.get("ticker") or item.get("symbol") or "").upper()
            if not symbol:
                continue
            quantity = item.get("quantity", item.get("qty", 0))
            try:
                normalized_quantity = float(quantity)
            except (TypeError, ValueError):
                normalized_quantity = 0.0

            positions.append(
                {
                    "symbol": symbol,
                    "quantity": normalized_quantity,
                }
            )

        recent_actions: list[dict[str, str]] = []
        for entry in payload.get("trade_log", [])[-20:]:
            action = str(entry.get("action", "")).upper()
            symbol = str(entry.get("ticker") or entry.get("symbol") or "").upper()
            if not symbol or action not in {"BUY", "CLOSE", "SCALE"}:
                continue
            recent_actions.append({"symbol": symbol, "action": action})

        return {
            "status": str(payload.get("status", "unknown")),
            "watched_tickers": watched,
            "positions": positions,
            "recent_actions": recent_actions,
            "daily_pnl": payload.get("daily_pnl", 0),
        }
