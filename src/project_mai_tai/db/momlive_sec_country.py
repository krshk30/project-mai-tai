"""Isolated MOMLIVE1 SEC metadata; importing registers tables without modifying models.py."""

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from project_mai_tai.db.base import Base


class MomliveSecCountry(Base):
    __tablename__ = "momentum_live_sec_country"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    cik: Mapped[str | None] = mapped_column(String(10))
    business_country: Mapped[str | None] = mapped_column(String(16))
    incorporation: Mapped[str | None] = mapped_column(String(16))
    is_us: Mapped[bool | None] = mapped_column(Boolean)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24))
    lookup_day: Mapped[date] = mapped_column(Date)
    source_sha256: Mapped[str | None] = mapped_column(String(64))


class MomliveSecRequestClock(Base):
    __tablename__ = "momentum_live_sec_request_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_request_ns: Mapped[int] = mapped_column(BigInteger, default=0)
