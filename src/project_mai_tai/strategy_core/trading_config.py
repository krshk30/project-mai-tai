from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class TradingConfig:
    dry_run: bool = True

    default_quantity: int = 100
    max_positions: int = 10
    max_daily_loss: float = -500.0

    stop_loss_cents: float = 0.02
    stop_loss_pct: float = 1.5

    trading_start_hour: int = 7
    trading_end_hour: int = 20
    dead_zone_start: str = "00:00"
    dead_zone_end: str = "00:00"

    confirm_bars: int = 1
    min_score: int = 4
    surge_rate: float = 0.001
    vol_min: int = 10_000
    cooldown_bars: int = 5
    use_ema_gate: bool = True
    p1_require_below_3bars: bool = True

    floor_check_interval_secs: int = 5

    scale_fast4_pct: float = 4.0
    scale_fast4_sell_pct: float = 75.0
    scale_normal2_pct: float = 2.0
    scale_normal2_sell_pct: float = 50.0
    scale_4after2_pct: float = 4.0
    scale_4after2_sell_pct: float = 25.0

    bar_interval_secs: int = 30

    def make_tos_variant(
        self,
        *,
        quantity: int = 100,
        bar_interval_secs: int = 60,
        stop_loss_pct: float = 1.0,
        cooldown_bars: int = 3,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": bar_interval_secs,
                "stop_loss_pct": stop_loss_pct,
                "confirm_bars": 0,
                "min_score": 0,
                "cooldown_bars": cooldown_bars,
                "use_ema_gate": False,
                "p1_require_below_3bars": False,
                "dead_zone_start": "00:00",
                "dead_zone_end": "00:00",
            }
        )
        return TradingConfig(**fields)

    def make_1m_variant(
        self,
        *,
        quantity: int = 100,
        bar_interval_secs: int = 60,
        stop_loss_pct: float = 1.0,
        min_score: int = 4,
        confirm_bars: int = 1,
        cooldown_bars: int = 1,
        dry_run: bool | None = None,
    ) -> "TradingConfig":
        fields = asdict(self)
        fields.update(
            {
                "dry_run": self.dry_run if dry_run is None else dry_run,
                "default_quantity": quantity,
                "bar_interval_secs": bar_interval_secs,
                "stop_loss_pct": stop_loss_pct,
                "min_score": min_score,
                "confirm_bars": confirm_bars,
                "cooldown_bars": cooldown_bars,
            }
        )
        return TradingConfig(**fields)
