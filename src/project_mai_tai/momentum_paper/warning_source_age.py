"""Shared A-bucket age contract; decision clocks and bucket bounds stay real."""

# MOMAGE1 retained PM/RTH: max receipt-end age 3789.173ms + unchanged 1000ms
# receive allowance, rounded up. AH/full-session calibration remains unmeasured.
MAX_AGGREGATE_SOURCE_END_AGE_MS = 4790


def aggregate_source_age_reason(*, end_ms: int, observed_ms: int) -> str | None:
    age_ms = observed_ms - end_ms
    if age_ms < 0:
        return "aggregate_bucket_incomplete"
    if age_ms > MAX_AGGREGATE_SOURCE_END_AGE_MS:
        return "aggregate_source_end_stale"
    return None
