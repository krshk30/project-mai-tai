"""Explicit single-session paper validation limits; ordinary defaults never move."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

DEFAULT_DETECTION_END = time(9, 30)
DEFAULT_GUARD_END = time(9, 40)


@dataclass(frozen=True)
class MomentumValidationWindow:
    session_date: date | None = None
    gateway_end: time = DEFAULT_DETECTION_END
    detection_end: time = DEFAULT_DETECTION_END
    guard_end: time = DEFAULT_GUARD_END

    def __post_init__(self) -> None:
        for value in (self.gateway_end, self.detection_end, self.guard_end):
            if not isinstance(value, time) or value.tzinfo is not None or value.second or value.microsecond:
                raise ValueError("validation ends must be whole-minute ET clocks")
        if self.session_date is None:
            if (self.gateway_end, self.detection_end, self.guard_end) != (
                DEFAULT_DETECTION_END, DEFAULT_DETECTION_END, DEFAULT_GUARD_END
            ):
                raise ValueError("custom paper limits require an explicit validation date")
            return
        if not isinstance(self.session_date, date) or isinstance(self.session_date, datetime):
            raise ValueError("validation date must be a session date")
        minimum_close = (datetime.combine(self.session_date, self.detection_end)
                         + timedelta(minutes=10)).time()
        if not (DEFAULT_DETECTION_END <= self.detection_end <= self.gateway_end <= time(20)
                and minimum_close <= self.guard_end <= time(20)):
            raise ValueError("validation limits must preserve the paper tail and feed window")

    @classmethod
    def from_settings(cls, settings) -> "MomentumValidationWindow":
        return cls(
            session_date=getattr(settings, "momentum_validation_session_date", None),
            gateway_end=getattr(settings, "market_data_momentum_warning_end_et", DEFAULT_DETECTION_END),
            detection_end=getattr(settings, "momentum_paper_detection_end_et", DEFAULT_DETECTION_END),
            guard_end=getattr(settings, "momentum_paper_guard_end_et", DEFAULT_GUARD_END),
        )

    def gateway_end_for(self, day: date) -> time:
        return self.gateway_end if day == self.session_date else DEFAULT_DETECTION_END

    def detection_end_for(self, day: date) -> time:
        return self.detection_end if day == self.session_date else DEFAULT_DETECTION_END

    def guard_end_for(self, day: date) -> time:
        return self.guard_end if day == self.session_date else DEFAULT_GUARD_END

    def paper_close_for(self, day: date) -> time:
        return self.guard_end_for(day).replace(second=1)

    def is_validation_clock(self, observed_et: datetime) -> bool:
        return (observed_et.date() == self.session_date
                and DEFAULT_DETECTION_END <= observed_et.time().replace(tzinfo=None) < self.detection_end)
