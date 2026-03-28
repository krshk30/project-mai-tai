from __future__ import annotations

from collections.abc import Callable, Mapping
import logging

from project_mai_tai.strategy_core.config import MomentumConfirmedConfig
from project_mai_tai.strategy_core.models import MarketSnapshot, ReferenceData
from project_mai_tai.strategy_core.snapshot_utils import (
    compute_rvol,
    get_current_hod,
    get_current_vwap,
    get_minutes_since_4am,
)

logger = logging.getLogger(__name__)


class MomentumConfirmedScanner:
    def __init__(self, config: MomentumConfirmedConfig):
        self.config = config
        self._tracking: dict[str, dict[str, object]] = {}
        self._confirmed: list[dict[str, object]] = []
        self._catalyst_source: Callable[[str], Mapping[str, object]] | object | None = None

    def set_catalyst_engine(self, catalyst_engine: Callable[[str], Mapping[str, object]] | object) -> None:
        self._catalyst_source = catalyst_engine

    def process_alerts(
        self,
        fired_alerts: list[dict[str, object]],
        reference_data: Mapping[str, ReferenceData] | None = None,
        snapshot_lookup: Mapping[str, MarketSnapshot] | None = None,
    ) -> list[dict[str, object]]:
        newly_confirmed: list[dict[str, object]] = []

        for alert in fired_alerts:
            ticker = str(alert.get("ticker", ""))
            alert_type = str(alert.get("type", ""))
            price = float(alert.get("price", 0) or 0)
            volume = int(alert.get("volume", 0) or 0)
            time_str = str(alert.get("time", ""))
            bid = float(alert.get("bid", 0) or 0)
            ask = float(alert.get("ask", 0) or 0)
            bid_size = int(alert.get("bid_size", 0) or 0)
            ask_size = int(alert.get("ask_size", 0) or 0)
            float_shares = int(alert.get("float", 0) or 0)

            if ticker not in self._tracking:
                self._tracking[ticker] = {
                    "has_volume_spike": False,
                    "first_spike_time": "",
                    "first_spike_price": 0.0,
                    "first_spike_volume": 0,
                    "squeezes": [],
                    "confirmed": False,
                    "confirmed_at": "",
                    "confirmed_price": 0.0,
                }

            track = self._tracking[ticker]
            if track["confirmed"]:
                continue

            if alert_type == "VOLUME_SPIKE" and not track["has_volume_spike"]:
                track["has_volume_spike"] = True
                track["first_spike_time"] = time_str
                track["first_spike_price"] = price
                track["first_spike_volume"] = volume
                logger.debug("[CONFIRMED] %s — volume spike recorded @ $%.2f", ticker, price)

            if "SQUEEZE" in alert_type and track["has_volume_spike"]:
                details = alert.get("details", {})
                change_pct = details.get("change_pct", 0) if isinstance(details, dict) else 0
                squeeze = {
                    "time": time_str,
                    "price": price,
                    "volume": volume,
                    "change_pct": change_pct,
                    "type": alert_type,
                    "bid": bid,
                    "ask": ask,
                    "bid_size": bid_size,
                    "ask_size": ask_size,
                }
                track["squeezes"].append(squeeze)

                if len(track["squeezes"]) == 1:
                    passed, _reason = self._check_common_filters(squeeze, float_shares)
                    if passed and self._has_bullish_news(ticker):
                        self._confirm_ticker(
                            ticker,
                            track,
                            squeeze,
                            float_shares,
                            reference_data,
                            snapshot_lookup,
                            "PATH_A_NEWS",
                            newly_confirmed,
                        )
                        continue

                if len(track["squeezes"]) >= 2:
                    sq1 = track["squeezes"][-2]
                    sq2 = track["squeezes"][-1]

                    passed, reason = self._check_common_filters(sq2, float_shares)
                    if not passed:
                        logger.debug("[CONFIRMED] %s — PATH B rejected: %s", ticker, reason)
                        continue

                    if sq2["price"] <= sq1["price"]:
                        continue
                    if sq2["volume"] < sq1["volume"]:
                        continue

                    self._confirm_ticker(
                        ticker,
                        track,
                        sq2,
                        float_shares,
                        reference_data,
                        snapshot_lookup,
                        "PATH_B_2SQ",
                        newly_confirmed,
                    )

        return newly_confirmed

    def get_confirmed(self, min_change_pct: float = 0) -> list[dict[str, object]]:
        if min_change_pct <= 0:
            return list(self._confirmed)
        return [stock for stock in self._confirmed if float(stock.get("change_pct", 0)) >= min_change_pct]

    def get_all_confirmed(self) -> list[dict[str, object]]:
        return list(self._confirmed)

    def get_top_n(
        self,
        n: int = 5,
        min_change_pct: float = 0,
        min_score: float | None = None,
    ) -> list[dict[str, object]]:
        threshold = self.config.rank_min_score if min_score is None else min_score
        candidates = [stock for stock in self._confirmed if float(stock.get("change_pct", 0)) >= min_change_pct]
        if not candidates:
            return []

        for stock in candidates:
            stock["rank_score"] = self._calculate_score(stock, candidates)

        ranked = sorted(candidates, key=lambda candidate: float(candidate.get("rank_score", 0)), reverse=True)
        if threshold > 0:
            ranked = [stock for stock in ranked if float(stock.get("rank_score", 0)) >= threshold]
        return ranked[:n]

    def update_live_prices(self, snapshot_lookup: Mapping[str, MarketSnapshot]) -> None:
        for stock in self._confirmed:
            ticker = str(stock.get("ticker", ""))
            snapshot = snapshot_lookup.get(ticker)
            if snapshot is None:
                continue
            price = get_current_hod(snapshot)
            if price > 0:
                stock["price"] = price
            prev_close = float(stock.get("prev_close", 0) or 0)
            if price > 0 and prev_close > 0:
                stock["change_pct"] = ((price - prev_close) / prev_close) * 100
            volume = snapshot.minute.accumulated_volume if snapshot.minute and snapshot.minute.accumulated_volume else 0
            if snapshot.day and snapshot.day.volume and snapshot.day.volume > 0:
                volume = snapshot.day.volume
            if volume > 0:
                stock["volume"] = volume

    def allow_reconfirmation(self, ticker: str) -> None:
        if ticker in self._tracking:
            self._tracking[ticker]["confirmed"] = False
            self._tracking[ticker]["has_volume_spike"] = False
            self._tracking[ticker]["squeezes"] = []
            logger.info("[CONFIRMED] %s — reset for re-confirmation", ticker)

    def reset(self) -> None:
        self._tracking.clear()
        self._confirmed.clear()
        logger.info("Momentum Confirmed scanner reset")

    def _check_common_filters(self, squeeze: dict[str, object], float_shares: int) -> tuple[bool, str]:
        volume = int(squeeze["volume"])

        if volume < self.config.confirmed_min_volume:
            return False, f"volume too low: {volume:,}"

        if float_shares > 0 and volume > 0:
            vol_float_ratio = volume / float_shares
            if vol_float_ratio < 0.20:
                return False, f"volume/float ratio too low: {vol_float_ratio:.1%} (need ≥20%)"

        if float_shares > 0 and float_shares > self.config.confirmed_max_float:
            return False, f"float too large: {float_shares:,}"

        return True, ""

    def _has_bullish_news(self, ticker: str) -> bool:
        if self._catalyst_source is None:
            return False

        try:
            if callable(self._catalyst_source):
                catalyst = self._catalyst_source(ticker)
            else:
                catalyst = self._catalyst_source.get_catalyst(ticker)
        except Exception:
            return False

        sentiment = str(catalyst.get("sentiment", ""))
        confidence = float(catalyst.get("ai_confidence", 0) or 0)
        if sentiment == "bullish" and confidence >= 0.8:
            logger.info("[CONFIRMED] %s — PATH A: AI BULLISH (%.0f%%)", ticker, confidence * 100)
            return True

        if sentiment == "bullish":
            logger.debug("[CONFIRMED] %s — PATH A bullish but low confidence (%.0f%%)", ticker, confidence * 100)
        else:
            logger.debug("[CONFIRMED] %s — PATH A rejected: %s (%.0f%%)", ticker, sentiment, confidence * 100)
        return False

    def _confirm_ticker(
        self,
        ticker: str,
        track: dict[str, object],
        squeeze: dict[str, object],
        float_shares: int,
        reference_data: Mapping[str, ReferenceData] | None,
        snapshot_lookup: Mapping[str, MarketSnapshot] | None,
        path: str,
        newly_confirmed: list[dict[str, object]],
    ) -> None:
        track["confirmed"] = True
        track["confirmed_at"] = squeeze["time"]
        track["confirmed_price"] = squeeze["price"]

        self._confirmed = [stock for stock in self._confirmed if stock.get("ticker") != ticker]
        confirmed = self._build_confirmed_entry(
            ticker=ticker,
            track=track,
            squeeze=squeeze,
            float_shares=float_shares,
            reference_data=reference_data,
            snapshot_lookup=snapshot_lookup,
        )
        confirmed["confirmation_path"] = path
        self._confirmed.append(confirmed)
        newly_confirmed.append(confirmed)

        path_label = "NEWS+1SQ" if "PATH_A" in path else "2 SQUEEZES"
        logger.info(
            "[CONFIRMED] ✅ %s — %s @ $%.2f | vol=%s | float=%s | squeezes=%s",
            ticker,
            path_label,
            float(squeeze["price"]),
            int(squeeze["volume"]),
            float_shares,
            len(track["squeezes"]),
        )

    def _build_confirmed_entry(
        self,
        ticker: str,
        track: dict[str, object],
        squeeze: dict[str, object],
        float_shares: int,
        reference_data: Mapping[str, ReferenceData] | None = None,
        snapshot_lookup: Mapping[str, MarketSnapshot] | None = None,
    ) -> dict[str, object]:
        bid = float(squeeze.get("bid", 0) or 0)
        ask = float(squeeze.get("ask", 0) or 0)
        spread = round(ask - bid, 4) if ask > 0 and bid > 0 else 0
        mid = (ask + bid) / 2 if ask > 0 and bid > 0 else 0
        spread_pct = round((spread / mid) * 100, 2) if mid > 0 else 0

        ref = reference_data.get(ticker) if reference_data else None
        avg_daily_volume = ref.avg_daily_volume if ref else 0.0
        shares_outstanding = ref.shares_outstanding if ref else float_shares
        minutes = get_minutes_since_4am()
        rvol = compute_rvol(float(squeeze["volume"]), avg_daily_volume, minutes) if avg_daily_volume > 0 else 0

        prev_close = 0.0
        hod = float(squeeze["price"])
        vwap = 0.0
        change_pct = 0.0
        if snapshot_lookup and ticker in snapshot_lookup:
            snapshot = snapshot_lookup[ticker]
            if snapshot.day and snapshot.day.close:
                prev_close = snapshot.day.close
                if prev_close > 0:
                    change_pct = round((float(squeeze["price"]) - prev_close) / prev_close * 100, 2)
            hod = get_current_hod(snapshot)
            vwap = get_current_vwap(snapshot)

        return {
            "ticker": ticker,
            "confirmed_at": track["confirmed_at"],
            "entry_price": track["confirmed_price"],
            "price": squeeze["price"],
            "change_pct": change_pct,
            "volume": squeeze["volume"],
            "rvol": round(rvol, 2),
            "shares_outstanding": shares_outstanding,
            "bid": bid,
            "ask": ask,
            "bid_size": squeeze.get("bid_size", 0),
            "ask_size": squeeze.get("ask_size", 0),
            "spread": spread,
            "spread_pct": spread_pct,
            "hod": hod,
            "vwap": vwap,
            "prev_close": prev_close,
            "avg_daily_volume": avg_daily_volume,
            "first_spike_time": track["first_spike_time"],
            "first_spike_price": track["first_spike_price"],
            "squeeze_count": len(track["squeezes"]),
            "data_age_secs": 0,
            "confirmation_path": "",
        }

    def _calculate_score(self, stock: dict[str, object], all_candidates: list[dict[str, object]]) -> float:
        if not all_candidates:
            return 0.0

        volumes = [float(candidate.get("volume", 0) or 0) for candidate in all_candidates]
        floats = [float(candidate.get("shares_outstanding", 0) or 0) for candidate in all_candidates]
        rvols = [float(candidate.get("rvol", 0) or 0) for candidate in all_candidates]
        changes = [float(candidate.get("change_pct", 0) or 0) for candidate in all_candidates]
        spreads = []
        for candidate in all_candidates:
            bid = float(candidate.get("bid", 0) or 0)
            ask = float(candidate.get("ask", 0) or 0)
            spreads.append((ask - bid) * 100 if bid > 0 and ask > 0 else 999)
        vf_ratios = []
        for candidate in all_candidates:
            fl = float(candidate.get("shares_outstanding", 0) or 0)
            vol = float(candidate.get("volume", 0) or 0)
            vf_ratios.append(vol / fl if fl > 0 else 0)

        n = len(all_candidates)

        def rank_score_asc(value: float, all_values: list[float]) -> float:
            sorted_values = sorted(all_values)
            rank = sorted_values.index(value) if value in sorted_values else 0
            return (rank / max(n - 1, 1)) * 100

        def rank_score_desc(value: float, all_values: list[float]) -> float:
            sorted_values = sorted(all_values, reverse=True)
            rank = sorted_values.index(value) if value in sorted_values else 0
            return (rank / max(n - 1, 1)) * 100

        vol = float(stock.get("volume", 0) or 0)
        fl = float(stock.get("shares_outstanding", 0) or 0)
        rvol = float(stock.get("rvol", 0) or 0)
        change = float(stock.get("change_pct", 0) or 0)
        bid = float(stock.get("bid", 0) or 0)
        ask = float(stock.get("ask", 0) or 0)
        spread = (ask - bid) * 100 if bid > 0 and ask > 0 else 999
        vf_ratio = vol / fl if fl > 0 else 0

        score = (
            rank_score_asc(vol, volumes) * 0.20
            + rank_score_desc(fl, floats) * 0.20
            + rank_score_asc(rvol, rvols) * 0.20
            + rank_score_asc(change, changes) * 0.20
            + rank_score_desc(spread, spreads) * 0.10
            + rank_score_asc(vf_ratio, vf_ratios) * 0.10
        )
        return round(score, 1)
