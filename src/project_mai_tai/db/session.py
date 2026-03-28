from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.settings import Settings


@lru_cache
def build_engine(database_url: str):
    return create_engine(database_url, pool_pre_ping=True)


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = build_engine(settings.database_url)
    return sessionmaker(bind=engine, expire_on_commit=False)
