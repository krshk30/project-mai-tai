from __future__ import annotations

from functools import lru_cache

from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MAI_TAI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "project-mai-tai"
    environment: str = "development"
    log_level: str = "INFO"

    control_plane_host: str = "127.0.0.1"
    control_plane_port: int = 8100

    database_url: str = (
        "postgresql+psycopg://mai_tai:change-me@localhost:5432/project_mai_tai"
    )
    redis_url: str = "redis://localhost:6379/0"
    redis_stream_prefix: str = "mai_tai"

    legacy_api_base_url: str | None = None

    broker_default_provider: str = "alpaca"
    alpaca_paper_base_url: str = "https://paper-api.alpaca.markets"

    dashboard_refresh_seconds: int = 5
    service_heartbeat_interval_seconds: int = 15

    @computed_field
    @property
    def control_plane_base_url(self) -> str:
        return f"http://{self.control_plane_host}:{self.control_plane_port}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
