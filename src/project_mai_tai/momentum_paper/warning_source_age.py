"""Shared A-bucket age contract; decision clocks and bucket bounds stay real."""

from array import array

# MOMAGE1 retained PM/RTH, including the Oct13 L tail: max receipt-end age
# 6600.994ms + unchanged 1000ms receive allowance, rounded up (reviewer05:2x).
# AH/full-session calibration remains unmeasured.
MAX_AGGREGATE_SOURCE_END_AGE_MS = 7601


def aggregate_source_age_reason(*, end_ms: int, observed_ms: int) -> str | None:
    age_ms = observed_ms - end_ms
    if age_ms < 0:
        return "aggregate_bucket_incomplete"
    if age_ms > MAX_AGGREGATE_SOURCE_END_AGE_MS:
        return "aggregate_source_end_stale"
    return None


class AggregateSourceAgeStats:
    """Exact millisecond histogram per sample; out-of-range p99 stays UNKNOWN."""

    HISTOGRAM_MAX_MS = 60_000

    def __init__(self) -> None:
        self.histogram = array("Q", [0]) * (self.HISTOGRAM_MAX_MS + 1)
        self.start_ns: int | None = None
        self.total = self.admitted = self.source_refused = self.transport_refused = 0
        self.incomplete = self.nonnegative = self.overflow = 0
        self.late_revisions = 0
        self.max_ms: int | None = None

    def observe(self, *, end_ms: int, observed_ns: int, refusal_reason: str | None) -> None:
        if self.start_ns is None:
            self.start_ns = observed_ns
        self.total += 1
        if refusal_reason is None:
            self.admitted += 1
        elif refusal_reason == "aggregate_source_end_stale":
            self.source_refused += 1
        elif refusal_reason == "aggregate_bucket_incomplete":
            self.incomplete += 1
        else:
            self.transport_refused += 1
        age_ms = observed_ns // 1_000_000 - end_ms
        if age_ms < 0:
            return
        self.nonnegative += 1
        self.max_ms = age_ms if self.max_ms is None else max(self.max_ms, age_ms)
        if age_ms <= self.HISTOGRAM_MAX_MS:
            self.histogram[age_ms] += 1
        else:
            self.overflow += 1

    def take_sample(self, *, observed_ns: int) -> dict[str, object]:
        p99 = None
        status = "NO_NONNEGATIVE_AGES"
        if self.nonnegative:
            rank = (self.nonnegative * 99 + 99) // 100
            cumulative = 0
            for age_ms, count in enumerate(self.histogram):
                cumulative += count
                if cumulative >= rank:
                    p99, status = age_ms, "EXACT_INTEGER_MS_NEAREST_RANK"
                    break
            else:
                status = "UNKNOWN_ABOVE_HISTOGRAM_BOUND"
        result = {
            "source_age_window_start_ns": self.start_ns,
            "source_age_window_end_ns": observed_ns,
            "source_age_window_start_semantics": "previous sample attempt, or first parsed bar before initial sample; None means no initial evidence",
            "source_age_parsed_bars": self.total,
            "source_age_admitted_bars": self.admitted,
            "source_age_refused_bars": self.source_refused,
            "source_age_incomplete_bars": self.incomplete,
            "source_age_late_revision_bars": self.late_revisions,
            "source_age_transport_refused_bars": self.transport_refused,
            "source_end_age_max_ms": self.max_ms,
            "source_end_age_p99_ms": p99,
            "source_end_age_p99_status": status,
            "source_end_age_nonnegative_samples": self.nonnegative,
            "source_end_age_histogram_max_ms": self.HISTOGRAM_MAX_MS,
            "source_end_age_overflow_samples": self.overflow,
            "source_age_bound_ms": MAX_AGGREGATE_SOURCE_END_AGE_MS,
            "source_age_admission_semantics": "AGE_GATE_PASS_NOT_PAPER_ENTRY",
            "source_age_observability": "OBSERVED_PARSED_BARS_NOT_HEALTH" if self.total else "EMPTY_NO_HEALTH_CLAIM",
            "source_age_count_semantics": "parsed bars partitioned by first transport/source-age gate; admitted is NOT warning candidates or trades",
            "source_age_sample_semantics": "window reset at existing consumer_sample emission attempt; no delivery or health guarantee",
            "source_end_age_population": "decision-wall-ms minus source-end-ms; all parsed nonnegative ages, including refused; future ages excluded",
        }
        self.__init__()
        self.start_ns = observed_ns
        return result
