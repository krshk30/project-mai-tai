"""Offline real Config/env seam; no database or migration execution."""
from pathlib import Path
import runpy
from types import SimpleNamespace

from alembic import context
from alembic.config import Config
import pytest
import sqlalchemy
from sqlalchemy.engine import URL, make_url

from project_mai_tai import settings


def nonce_url():
    return URL.create("postgresql+psycopg", username="controlled",
        database="controlled_nonce", query={"host": "/private/tmp/controlled/socket", "port": "55521"})


def test_actual_config_roundtrip_preserves_exact_percent_encoded_unix_url():
    url = nonce_url()
    raw = url.render_as_string(hide_password=False)
    assert "%2F" in raw
    cfg = Config()
    with pytest.raises(ValueError, match="invalid interpolation syntax"):
        cfg.set_main_option("sqlalchemy.url", raw)
    cfg.set_main_option("sqlalchemy.url", raw.replace("%", "%%"))
    assert cfg.get_main_option("sqlalchemy.url") == raw
    assert cfg.get_section(cfg.config_ini_section)["sqlalchemy.url"] == raw
    assert make_url(cfg.get_main_option("sqlalchemy.url")) == url


def test_actual_env_passes_original_nonce_url_to_forbidden_connect_engine(monkeypatch):
    url = nonce_url()
    raw = url.render_as_string(hide_password=False)
    cfg = Config()
    seen = []

    class NoConnect(RuntimeError):
        pass

    class ForbiddenEngine:
        def connect(self):
            seen.append("CONNECT_REFUSED_NO_DATABASE")
            raise NoConnect

    def engine(section, *, prefix, poolclass):
        assert section["sqlalchemy.url"] == raw
        assert make_url(section["sqlalchemy.url"]) == url
        assert prefix == "sqlalchemy." and poolclass is sqlalchemy.pool.NullPool
        seen.append("EXACT_ORIGINAL_URL")
        return ForbiddenEngine()

    monkeypatch.setattr(context, "config", cfg, raising=False)
    monkeypatch.setattr(context, "is_offline_mode", lambda: False)
    monkeypatch.setattr(settings, "get_settings", lambda: SimpleNamespace(database_url=raw))
    monkeypatch.setattr(sqlalchemy, "engine_from_config", engine)
    env = Path(__file__).resolve().parents[2] / "sql/migrations/env.py"
    with pytest.raises(NoConnect):
        runpy.run_path(str(env), run_name="__controlled_actual_env__")
    assert seen == ["EXACT_ORIGINAL_URL", "CONNECT_REFUSED_NO_DATABASE"]
