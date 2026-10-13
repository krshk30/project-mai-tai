"""Daily paper observation limits; explicit clocks may override the defaults."""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

DEFAULT_DETECTION_END = time(18)
DEFAULT_GUARD_END = time(18, 10)


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
        if self.session_date is not None and (
            not isinstance(self.session_date, date) or isinstance(self.session_date, datetime)
        ):
            raise ValueError("validation date must be a session date")
        minimum_close = (datetime.combine(date(2000, 1, 1), self.detection_end)
                         + timedelta(minutes=10)).time()
        if not (time(9, 30) <= self.detection_end <= self.gateway_end <= time(20)
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
        return self.gateway_end

    def detection_end_for(self, day: date) -> time:
        return self.detection_end

    def guard_end_for(self, day: date) -> time:
        return self.guard_end

    def paper_close_for(self, day: date) -> time:
        return self.guard_end_for(day).replace(second=1)

    def is_validation_clock(self, observed_et: datetime) -> bool:
        return time(9, 30) <= observed_et.time().replace(tzinfo=None) < self.detection_end
