"""C1 16:39 market-clock mapping. Never maps durable or native evidence.

This module prepares boot-only operands, not a ready/seed or entry certificate.
The existing service must still consume recorded events through real callbacks.
"""

from copy import deepcopy
import csv
from datetime import date, datetime, time, timedelta
from hashlib import sha256
import json
import io
import re
from decimal import Decimal
from zoneinfo import ZoneInfo


SCOPE = "RECONSTRUCTED-RETIMED (BOOTGATEONLY)"
SOURCE_DAY = "2026-10-23"
NY = ZoneInfo("America/New_York")
ADAPTER_HEAD = "94b0814cbb480e8c3bc0db5104a9a88541eeef28"
ADAPTER_PINS = {
    "tools/keepwatch_weekend_unified.py": "9d30ab939691c61fa4576c7f0aa8b60ddf75000a4633af2f0e13d59de8740d6a",
    "tools/keepwatch_weekend_qualification_driver.py": "dcfc18641cd83bcd8f6080b32ac25aa46d969cd57bab0cb9842966df64907f9c",
    "tools/keepwatch_weekend_owner_clock.py": "5eae482762804531275ffdea9f9673b5fa1aaa18d3a2fad4e15021ede5853946",
}
FILE_PORT_SHA = "14e9b8bfdae34d9dd4144849162471fa5eb3f102e2f286ccf52b18b125e7a6b9"
DATA_SHA = "41153d97a1977e94a54f4f9f1e0c22ca0d85f69c510f85865fc051cec7f4c1b5"
FLEET_FILES = {
    "scanner_confirmed_events_2026-10-23.csv": "1a72dd56401aaf94356531554b24b4402409f4d21d6a9622d8da2ac86036b41d",
    "strategy_bar_history_2026-10-23_18names.csv": "abe1a958cfa0b161f4fe34f66b2250acc462540e0ba85c3fd460403e73ec7d4a",
    "massive_1m_2026-10-23_18names.csv": "5b79eb71a2156fd1030b9dffcfb21c9dbb3b082aa92b88dabe90c7523fd9c79f",
}
FLEET_NAMES = tuple(sorted(("AARD", "YMAT", "AIXI", "FGL", "BKYI", "NCPL", "CLRO", "STAK",
    "POM", "SAIQ", "AIOK", "BOLT", "HTCO", "JYD", "VEEA", "DKI", "SPRC", "RENT")))
OLD_LOG_SHA = "a2a3cd0a5e2c11e7ee9fbf2dbe3bc794f6c6931784cae45f089a12080da876dc"
OLD_LOG_FILE = "schwab-1m-v2_2026-10-23_18names_window_export.log"
OLD_RETURN = re.compile(r"\[V2-RESTING-RETURN\] symbol=([A-Z0-9.-]+) opportunity_id=\d+ bar_ms=(\d+) slot=([a-z_]+) reason=([a-z_]+)$")
OLD_ACCOUNT_LEG_RETURN = re.compile(
    r"\[V2-RESTING-RETURN\] symbol=([A-Z0-9.-]+) opportunity_id=\d+ bar_ms=(\d+) "
    r"slot=((first|reclaim):(live:(?:schwab_1m_v2|orb))) reason=(leg_skipped_country_other_venue)$")
OLD_MEMBERSHIP = re.compile(r"watchlist updated count=(\d+) sample=([^ ]*) warmed=(\d+)$")
# Literal returns below occur AFTER line_buy_ready in the actual _track_resting
# method. rearm_* and warmup/drain are NOT line-ready witnesses.
OLD_READY_RETURNS = frozenset(("track_disabled", "track_gap_hold", "track_entries_held",
    "track_other_slot_owned", "track_position_held", "track_window_closed",
    "track_keep_existing_buy_rest", "track_pm_flip_grace_expired", "track_fill_settling",
    "track_flip_grace_expired", "track_quote_missing", "track_quote_unusable",
    "track_liquidity_floor", "track_short_not_established", "track_bar_stale",
    "track_stop_ask_refused", "track_first_slot_consumed", "track_placement_not_armed",
    "track_liquidity_cancel", "track_existing_rest_managed", "track_long_no_new_rest", "track_signal_unknown"))


def _digest(value):
    if (type(value) is not str or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)):
        raise ValueError("explicit_reviewed_SHA256_required")
    return value


def _midnight(day):
    return datetime.combine(date.fromisoformat(day), time(), NY)


def _ms(value):
    epoch = datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))
    delta = value.astimezone(epoch.tzinfo) - epoch
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


class RecordedSessionMap:
    """Explicit wall-session map; no float timestamp/ns projection or defaults."""

    def __init__(self, *, target_day, ruling_sha256, chronology_review_sha256):
        target = date.fromisoformat(target_day)
        if target <= date(2026, 10, 24) or target.weekday() >= 5:
            raise ValueError("explicit_next_weekday_after_actual_capture_required")
        self.target_day = target_day
        self.ruling_sha256 = _digest(ruling_sha256)
        self.chronology_review_sha256 = _digest(chronology_review_sha256)

    def market_ms(self, value):
        if type(value) is not int:
            raise ValueError("recorded_integer_market_ms_required")
        start = _ms(_midnight(SOURCE_DAY))
        offset = value - start
        if not 0 <= offset < 86400000:
            raise ValueError("market_timestamp_outside_approved_Friday_session")
        return _ms(_midnight(self.target_day) + timedelta(milliseconds=offset))

    def market_ns(self, value):
        if type(value) is not int:
            raise ValueError("recorded_integer_market_ns_required")
        millis, remainder = divmod(value, 1000000)
        return self.market_ms(millis) * 1000000 + remainder

    def receipt(self):
        return dict(scope=SCOPE, source_day=SOURCE_DAY, target_day=self.target_day,
            ruling_sha256=self.ruling_sha256,
            chronology_review_sha256=self.chronology_review_sha256,
            durable_rows="UNCHANGED_ACTUAL_10_24_CAPTURE_WITH_ORIGINAL_AGES",
            native_headers="UNCHANGED_UNKNOWN_OR_STALE_REMAINS_REFUSAL",
            economics="NOT_QUALIFIED", original_case_PASS=False,
            original_provider_receipt="UNKNOWN", permission_granted=False)


def map_file_history_rows(rows, mapping):
    """Map only the literal reviewed FileHistoryReader's market fields.

    Preserve original records separately. UNKNOWN availability stays None; the
    existing adapter may model it only with its explicit reconstruction label.
    """
    if type(mapping) is not RecordedSessionMap or type(rows) is not list:
        raise ValueError("explicit_session_map_and_recorded_rows_required")
    result, seen = [], set()
    for original in rows:
        if type(original) is not dict or not {"stamp", "row_id", "origin"} <= original.keys():
            raise ValueError("reviewed_file_history_row_required")
        row = deepcopy(original)
        key = (row["origin"], row["row_id"])
        if key in seen:
            raise ValueError("duplicate_recorded_history_identity")
        seen.add(key)
        row["stamp"] = mapping.market_ms(original["stamp"])
        if original.get("available_at_ms") is not None:
            row["available_at_ms"] = mapping.market_ms(original["available_at_ms"])
        ref = original.get("massive_reference")
        if ref is not None:
            if type(ref) is not dict or ref.get("stamp") != original["stamp"]:
                raise ValueError("Massive_companion_recorded_minute_mismatch")
            row["massive_reference"] = map_file_history_rows([ref], mapping)[0]
        row["boot_mapping"] = dict(scope=SCOPE, original=deepcopy(original),
            original_record_canonical_sha256=sha256(json.dumps(original, sort_keys=True,
                separators=(",", ":"), default=str).encode()).hexdigest())
        result.append(row)
    return result


def timeline_denominator(*, recorded_names, chronology_by_name, observed_names):
    """Never silently shrink the population to successfully reconstructed cases."""
    if (type(recorded_names) is not list or not recorded_names
            or any(type(name) is not str or not name for name in recorded_names)
            or len(set(recorded_names)) != len(recorded_names)
            or type(chronology_by_name) is not dict
            or type(observed_names) not in (set, list, tuple)):
        raise ValueError("explicit_nonempty_recorded_population_required")
    names = set(recorded_names) | set(observed_names)
    if any(type(name) is not str or not name for name in names):
        raise ValueError("invalid_observed_symbol")
    if set(chronology_by_name) - names:
        raise ValueError("chronology_outside_declared_population")
    missing = sorted(name for name in names if not chronology_by_name.get(name))
    return dict(names=sorted(names), denominator=len(names),
        missing_chronology=missing, status="UNKNOWN" if missing else "INPUT_NAMES_BOUND_NOT_SEEDED",
        native_permission="UNKNOWN", seed_complete="UNRUN")


def _aware_ms(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("aware_recorded_CSV_clock_required")
    return _ms(parsed)


def _aware_ns(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("aware_recorded_CSV_clock_required")
    return _ms(parsed) * 1000000 + (parsed.microsecond % 1000) * 1000


def decode_friday_fleet(blobs, mapping):
    """Exact delivered packet, by CSV names; no IO, provider or inferred CONFIRM.

    Preserve all strategies/raw rows. Only the actual v2 strategy's stored bars
    enter its Schwab FILE projection. Other strategies remain explicit noninputs.
    Raw provider availability is UNKNOWN; DB created_at is not HTTP arrival.
    """
    if type(mapping) is not RecordedSessionMap or type(blobs) is not dict or set(blobs) != set(FLEET_FILES):
        raise ValueError("exact_three_Friday_fleet_artifacts_required")
    parsed = {}
    counts = (62, 2751, 11186)
    for (name, digest), count in zip(FLEET_FILES.items(), counts):
        raw = blobs[name]
        if type(raw) is not bytes or sha256(raw).hexdigest() != digest:
            raise ValueError("Friday_fleet_artifact_SHA_mismatch")
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8"), newline=""))
        if len(reader.fieldnames or ()) != len(set(reader.fieldnames or ())):
            raise ValueError("duplicate_CSV_columns")
        rows = list(reader)
        if len(rows) != count or any(None in row or None in row.values() for row in rows):
            raise ValueError("complete_Friday_CSV_count_and_shape_required")
        parsed[name] = rows
    scanner_name, history_name, massive_name = tuple(FLEET_FILES)
    scanner = parsed[scanner_name]
    if {row["symbol"] for row in scanner} != set(FLEET_NAMES):
        raise ValueError("exact18_Friday_population_required")
    chronology = {name: [] for name in FLEET_NAMES}
    first_confirm = {name: None for name in FLEET_NAMES}
    confirmations, ids, previous = [], set(), None
    for line, row in enumerate(scanner, 2):
        if (row["trade_date"] != SOURCE_DAY or row["id"] in ids
                or row["event_type"] not in {"CONFIRM", "FADE", "RETENTION_DROP"}):
            raise ValueError("scanner_identity_type_or_day_refused")
        ids.add(row["id"])
        source_ns = _aware_ns(row["event_at"])
        source_ms = source_ns // 1000000
        mapped_ms = mapping.market_ms(source_ms)
        if previous is not None and mapped_ms < previous:
            raise ValueError("scanner_source_order_changed_no_sort_repair")
        previous = mapped_ms
        record = dict(symbol=row["symbol"], event_type=row["event_type"], event_id=row["id"],
            original_ms=source_ms, mapped_ms=mapped_ms, original_ns=source_ns,
            mapped_ns=mapping.market_ns(source_ns), original=deepcopy(row),
            raw_ref=scanner_name + ":CSV:" + str(line), raw_sha256=FLEET_FILES[scanner_name])
        chronology[row["symbol"]].append(record)
        if row["event_type"] == "CONFIRM":
            confirmations.append(record)
            if first_confirm[row["symbol"]] is None:
                first_confirm[row["symbol"]] = record
    # Keep each source's original identity and every non-v2 row; no wildcard
    # dedup or favorable merge. The existing FILE adapter's Schwab precedence
    # is a reconstructed response contract, not original producer availability.
    provider_rows = {name: {} for name in FLEET_NAMES}
    retained_history, unused_history, history_ids = [], [], set()
    for line, row in enumerate(parsed[history_name], 2):
        name = row["symbol"]
        if name not in provider_rows or row["id"] in history_ids:
            raise ValueError("history_foreign_or_duplicate_PK")
        history_ids.add(row["id"])
        source_ms = _aware_ms(row["bar_time"])
        mapping.market_ms(source_ms)
        retained_history.append(deepcopy(row))
        if row["strategy_code"] != "schwab_1m_v2" or row["interval_secs"] != "60":
            unused_history.append(dict(csv_line=line, original=deepcopy(row)))
            continue
        pair = provider_rows[name].setdefault(source_ms, {})
        if "schwab" in pair:
            raise ValueError("duplicate_v2_closed_minute_no_silent_revision_selection")
        available_ns = _aware_ns(row["created_at"])
        pair["schwab"] = dict(stamp=source_ms, row_id=row["id"], origin="strategy_bar_history",
            available_at_ms=(available_ns + 999999) // 1000000, db_original_created_at=row["created_at"],
            source=row["source"], strategy_code=row["strategy_code"], provider=None,
            **{key: row[key + "_price"] for key in ("open", "high", "low", "close")},
            volume=row["volume"], raw_ref=history_name + ":CSV:" + str(line),
            raw_sha256=FLEET_FILES[history_name])
    for line, row in enumerate(parsed[massive_name], 2):
        name, stamp = row["symbol"], int(row["start_ms"])
        if name not in provider_rows:
            raise ValueError("foreign_Massive_symbol")
        mapping.market_ms(stamp)
        pair = provider_rows[name].setdefault(stamp, {})
        if "massive" in pair:
            raise ValueError("duplicate_Massive_closed_minute")
        pair["massive"] = dict(stamp=stamp, row_id="MASSIVE_CSV:" + str(line),
            origin="C1_MASSIVE_POSTHOC_REFETCH", provider="massive",
            **{key: row[key] for key in ("open", "high", "low", "close", "volume")},
            raw_ref=massive_name + ":CSV:" + str(line), raw_sha256=FLEET_FILES[massive_name])
    file_rows = {}
    for name, minutes in provider_rows.items():
        file_rows[name] = []
        for stamp, pair in sorted(minutes.items()):
            selected = deepcopy(pair.get("schwab", pair.get("massive")))
            if "schwab" in pair and "massive" in pair:
                selected["massive_reference"] = deepcopy(pair["massive"])
            for key in ("open", "high", "low", "close", "volume"):
                number = Decimal(selected[key])
                if not number.is_finite():
                    raise ValueError("nonfinite_recorded_OHLCV")
                selected[key] = number
            if "massive_reference" in selected:
                for key in ("open", "high", "low", "close", "volume"):
                    number = Decimal(selected["massive_reference"][key])
                    if not number.is_finite():
                        raise ValueError("nonfinite_companion_OHLCV")
                    selected["massive_reference"][key] = number
            file_rows[name].append(selected)
    return dict(version="FRIDAY18_PER_CONFIRM_V1", names=list(FLEET_NAMES),
        source_sha256=dict(FLEET_FILES), mapping=mapping.receipt(), chronology=chronology,
        confirmations=confirmations, first_confirm_by_name=first_confirm, file_rows=file_rows,
        retained_history=retained_history, retained_massive=deepcopy(parsed[massive_name]),
        non_v2_history_not_consumed=unused_history,
        missing_confirm_names=[name for name, record in first_confirm.items() if record is None],
        native_availability="UNKNOWN", provider_availability="UNKNOWN_NOT_DB_CREATED_AT",
        seed_limit_bars=None, seed_limit_review_sha256=None, permission_granted=False)


class ConfirmationSeedTracker:
    """Measure actual observed FILE-model seed latency; never invent a bar limit."""

    def __init__(self, fleet, *, limit_bars, limit_metric, limit_review_sha256):
        if (type(fleet) is not dict or fleet.get("version") != "FRIDAY18_PER_CONFIRM_V1"
                or fleet.get("names") != list(FLEET_NAMES) or fleet.get("source_sha256") != FLEET_FILES):
            raise ValueError("exact_Friday18_reviewed_fleet_required")
        if limit_bars is None:
            if limit_metric is not None or limit_review_sha256 is not None:
                raise ValueError("unbound_bar_limit_cannot_have_implied_policy")
        elif (type(limit_bars) is not int or limit_bars < 0
                or limit_metric not in {"CLOSED_MINUTE_BOUNDARIES", "ELAPSED_MINUTE_CEILING"}):
            raise ValueError("explicit_reviewed_bar_limit_and_count_definition_required")
        else:
            _digest(limit_review_sha256)
        self.fleet, self.limit_bars, self.limit_metric = fleet, limit_bars, limit_metric
        self.limit_review_sha256 = limit_review_sha256
        self.anchor_ms = _ms(_midnight(fleet["mapping"]["target_day"]) + timedelta(hours=4))
        self.observed = {}
        self.last_observed_ns = None
        self.window_ruling_sha256 = None
        self.old_windows = None
        self.not_ready = {}
        self.first_line_ready = {}

    def bind_c11729(self, *, ruling_sha256, old_window_review):
        """Bind independent OLD per-window evidence, never the NEW target output.

        None keeps the comparison UNKNOWN. Aggregate whole-day totals cannot
        replace a complete source-referenced split of each CONFIRM/re-add.
        """
        self.window_ruling_sha256 = _digest(ruling_sha256)
        if old_window_review is None:
            self.old_windows = None
            return
        if (type(old_window_review) is not dict or set(old_window_review) != {
                "scope", "raw_log_sha256", "review_sha256", "scanner_sha256", "windows"}
                or old_window_review["scope"] != "INDEPENDENT_LIVE_OLD_LOG_PER_CONFIRM_WINDOWS"
                or old_window_review["scanner_sha256"] != FLEET_FILES["scanner_confirmed_events_2026-10-23.csv"]):
            raise ValueError("independent_raw_OLD_window_review_required_not_whole_day_totals")
        _digest(old_window_review["raw_log_sha256"])
        _digest(old_window_review["review_sha256"])
        windows = old_window_review["windows"]
        expected = {row["event_id"]: row for row in self.fleet["confirmations"]}
        if type(windows) is not dict or set(windows) != set(expected):
            raise ValueError("every_actual_CONFIRM_window_required")
        for identity, row in windows.items():
            confirmation = expected[identity]
            following = [event["original_ns"] for event in self.fleet["chronology"][confirmation["symbol"]]
                if event["original_ns"] > confirmation["original_ns"]]
            expected_end = min(following) if following else 1792800000000 * 1000000
            if (type(row) is not dict or set(row) != {"original_confirm_ns", "original_end_ns",
                    "original_first_ready_ns", "not_ready_closed_minutes", "ready_status", "raw_line_references"}
                    or row["original_confirm_ns"] != confirmation["original_ns"]
                    or type(row["original_end_ns"]) is not int
                    or row["original_end_ns"] != expected_end
                    or type(row["not_ready_closed_minutes"]) is not int
                    or row["not_ready_closed_minutes"] < 0
                    or row["ready_status"] not in {"READY", "NEVER_READY", "UNKNOWN"}
                    or type(row["raw_line_references"]) is not list or not row["raw_line_references"]):
                raise ValueError("exact_OLD_window_clocks_counts_and_raw_references_required")
            ready = row["original_first_ready_ns"]
            if ((row["ready_status"] == "READY" and (type(ready) is not int
                    or not row["original_confirm_ns"] <= ready < row["original_end_ns"]))
                    or row["ready_status"] != "READY" and ready is not None):
                raise ValueError("OLD_first_ready_clock_cannot_be_inferred_from_silence")
        self.old_windows = deepcopy(old_window_review)

    def _active_confirmations(self, symbol, clock_ns):
        events = self.fleet["chronology"][symbol]
        for index, event in enumerate(events):
            if event["event_type"] != "CONFIRM" or clock_ns < event["mapped_ns"]:
                continue
            end_ns = events[index + 1]["mapped_ns"] if index + 1 < len(events) else None
            if end_ns is None or clock_ns < end_ns:
                yield event

    def observe_return_reason(self, *, symbol, reason, clock_ns, closed_bar_ms):
        if self.window_ruling_sha256 is None or reason != "track_line_not_ready":
            return
        if symbol not in FLEET_NAMES or type(clock_ns) is not int or type(closed_bar_ms) is not int:
            raise ValueError("actual_not_ready_return_symbol_clock_and_closed_bar_required")
        if closed_bar_ms % 60000 or (closed_bar_ms + 60000) * 1000000 > clock_ns:
            raise ValueError("not_ready_count_cannot_use_unclosed_bar")
        for event in self._active_confirmations(symbol, clock_ns):
            first_closed_ns = (event["mapped_ns"] + 59999999999) // 60000000000 * 60000000000
            if (closed_bar_ms + 60000) * 1000000 < first_closed_ns:
                continue
            self.not_ready.setdefault(event["event_id"], {}).setdefault(closed_bar_ms, dict(
                clock_ns=clock_ns, closed_bar_ms=closed_bar_ms, reason=reason))

    def observe(self, sample):
        if sample.get("status") != "OBSERVED_SEED_SAMPLE_NOT_FULL_TIMELINE_QUALIFICATION":
            raise ValueError("actual_service_seed_observer_sample_required")
        now = sample["observed_ms"]
        if type(now) is not int:
            raise ValueError("actual_integer_sample_clock_required")
        now_ns = sample.get("observed_clock_ns", now * 1000000)
        if (type(now_ns) is not int or now_ns // 1000000 != now
                or self.last_observed_ns is not None and now_ns < self.last_observed_ns):
            raise ValueError("causal_exact_observer_clock_required")
        self.last_observed_ns = now_ns
        seeded = {row["symbol"]: row for row in sample["symbols"] if row["seed_complete"] is True}
        if any(row.get("history_anchor_ms") != self.anchor_ms for row in seeded.values()):
            raise ValueError("seed_receipt_session_anchor_contradiction")
        for name, row in seeded.items():
            if row.get("line_buy_ready") is True:
                for event in self._active_confirmations(name, now_ns):
                    self.first_line_ready.setdefault(event["event_id"], dict(
                        observed_clock_ns=now_ns, observed_ms=now,
                        history_completed_ns=row.get("history_completed_ns")))
        for confirmation in self.fleet["confirmations"]:
            identity, name, confirmed = confirmation["event_id"], confirmation["symbol"], confirmation["mapped_ms"]
            confirmed_ns = confirmation["mapped_ns"]
            if identity in self.observed or now_ns < confirmed_ns or name not in seeded:
                continue
            row, elapsed = seeded[name], now - confirmed
            self.observed[identity] = dict(symbol=name, event_id=identity,
                raw_ref=confirmation["raw_ref"], original_confirm_ms=confirmation["original_ms"],
                mapped_confirm_ms=confirmed, observed_seed_ms=now, elapsed_model_ms=elapsed,
                elapsed_model_ns=now_ns - confirmed_ns, observed_clock_ns=now_ns,
                closed_minute_boundaries=now // 60000 - confirmed // 60000,
                elapsed_minute_ceiling=(now_ns - confirmed_ns + 59999999999) // 60000000000,
                history_completed_ns=row.get("history_completed_ns"), scope=SCOPE,
                original_provider_latency="UNKNOWN", native_permission="UNKNOWN")

    def report(self):
        per_name = []
        for name in FLEET_NAMES:
            first = self.fleet["first_confirm_by_name"][name]
            observed = self.observed.get(first["event_id"]) if first is not None else None
            within = None
            if observed is not None and self.limit_bars is not None:
                field = {"CLOSED_MINUTE_BOUNDARIES": "closed_minute_boundaries",
                    "ELAPSED_MINUTE_CEILING": "elapsed_minute_ceiling"}[self.limit_metric]
                within = observed[field] <= self.limit_bars
            per_name.append(dict(symbol=name, first_confirmation=first, first_seed=observed,
                within_reviewed_bar_limit=within,
                status="CONFIRM_CLOCK_UNKNOWN" if first is None else
                    "SEED_UNOBSERVED" if observed is None else
                    "BAR_LIMIT_UNBOUND" if self.limit_bars is None else "MEASURED_NOT_INSTALL_GO"))
        windows = []
        if self.window_ruling_sha256 is not None:
            for event in self.fleet["confirmations"]:
                identity = event["event_id"]
                returns = list(self.not_ready.get(identity, {}).values())
                ready = self.first_line_ready.get(identity)
                old = self.old_windows["windows"][identity] if self.old_windows is not None else None
                outcome = ("FAIL_NEW_WINDOW_OVER_10" if len(returns) > 10 else
                    "UNKNOWN_OLD_WINDOW_LOG_UNBOUND" if old is None else
                    "FAIL_NEW_SLOWER_THAN_OLD" if len(returns) > old["not_ready_closed_minutes"] else
                    "UNKNOWN_OLD_WINDOW_READY_UNPROVEN" if old["ready_status"] == "UNKNOWN" else
                    "NEVER_READY_LIKE_FOR_LIKE_LABEL_NOT_AUTOMATIC_PASS" if ready is None and old["ready_status"] == "NEVER_READY" else
                    "UNKNOWN_NEW_READY_UNOBSERVED" if ready is None else "MEASURED_WITHIN_BOTH_LIMITS_NOT_INSTALL_GO")
                windows.append(dict(event_id=identity, symbol=event["symbol"],
                    original_confirm_ns=event["original_ns"], mapped_confirm_ns=event["mapped_ns"],
                    actual_not_ready_closed_minutes=len(returns), actual_returns=returns,
                    first_line_ready=ready, old_window=old, outcome=outcome))
        return dict(seed_denominator=18, per_name=per_name,
            observed_first_confirm_seeds=sum(row["first_seed"] is not None for row in per_name),
            missing_confirm_names=list(self.fleet["missing_confirm_names"]),
            per_confirmation=list(self.observed.values()), limit_bars=self.limit_bars,
            limit_metric=self.limit_metric, limit_review_sha256=self.limit_review_sha256,
            c11729_ruling_sha256=self.window_ruling_sha256, per_confirm_windows=windows,
            c11729_hard_cap_closed_minutes=10 if self.window_ruling_sha256 is not None else None,
            window_definition="CONFIRM_UNTIL_NEXT_SCANNER_EVENT_SAME_NAME_EXPLICIT_REVIEW_REQUIRED",
            counter="UNIQUE_ACTUAL_TRACK_LINE_NOT_READY_CLOSED_BAR_RETURNS_NOT_ELAPSED_MINUTES",
            status=("C11729_OLD_WINDOW_LOG_UNBOUND_NOT_PASS" if self.old_windows is None else
                "C11729_WINDOW_COMPARISON_NOT_INSTALL_GO") if self.window_ruling_sha256 is not None else
                "BAR_LIMIT_UNBOUND_NOT_PASS" if self.limit_bars is None else "OBSERVED_LIMIT_COMPARISON_REQUIRES_REVIEW",
            permission_granted=False)


def parse_old_log_lines(raw):
    """Geometry only, also usable by CONTROLLED unit records. Not acceptance."""
    records = []
    for ordinal, line in enumerate(raw.decode("utf-8").splitlines(), 1):
        if not re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ", line):
            raise ValueError("OLD_log_clock_shape_STOP_line_" + str(ordinal))
        stamp = datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=ZoneInfo("UTC"))
        record = dict(ordinal=ordinal, original_ns=_aware_ns(stamp.isoformat()),
            raw_line_sha256=sha256(line.encode()).hexdigest(), raw=line)
        record["session_context"] = ("BEFORE_FRIDAY_04_ROLL_NOT_CURRENT_FLEET_PROOF"
            if record["original_ns"] < 1792742400000 * 1000000 else
            "FRIDAY_SESSION_RECORDED_CONTEXT_NOT_PERMISSION")
        matched = OLD_RETURN.search(line)
        leg = OLD_ACCOUNT_LEG_RETURN.search(line) if matched is None else None
        if "[V2-RESTING-RETURN]" in line and matched is None and leg is None:
            raise ValueError("OLD_return_grammar_STOP_line_" + str(ordinal))
        if matched:
            record.update(kind="RETURN", symbol=matched[1], closed_bar_ms=int(matched[2]),
                slot=matched[3], reason=matched[4])
        elif leg:
            record.update(kind="ACCOUNT_LEG_CONTEXT_NOT_READINESS", symbol=leg[1],
                closed_bar_ms=int(leg[2]), slot=leg[3], base_slot=leg[4], account=leg[5],
                reason=leg[6], readiness_witness=False)
        else:
            member = OLD_MEMBERSHIP.search(line)
            if "watchlist updated" in line and member is None:
                raise ValueError("OLD_watchlist_grammar_STOP_line_" + str(ordinal))
            if member:
                names = member[2].split(",") if member[2] else []
                count = int(member[1])
                if len(names) != min(count, 5) or len(names) != len(set(names)):
                    raise ValueError("OLD_watchlist_sample_cardinality_STOP")
                record.update(kind="MEMBERSHIP", count=count, sampled_names=names,
                    sample_is_complete=count <= 5, warmed=int(member[3]))
            else:
                record["kind"] = "CONTEXT_NOT_A_READINESS_WITNESS"
        records.append(record)
    return records


def old_window_candidates_from_records(records, fleet, *, review_sha256):
    """UNIT geometry seam. Independently binds OLD only, no assessed NEW input.

    Missing membership/ready evidence is UNKNOWN, never zero-ready or a fabricated
    CONFIRM. A raw empty sample at04roll is a specific OLD observation only.
    """
    _digest(review_sha256)
    windows, details = {}, []
    for confirmation in fleet["confirmations"]:
        symbol, start = confirmation["symbol"], confirmation["original_ns"]
        after = [row["original_ns"] for row in fleet["chronology"][symbol] if row["original_ns"] > start]
        end = min(after) if after else 1792800000000 * 1000000
        first_boundary = (start + 59999999999) // 60000000000 * 60000000000
        selected = [row for row in records if row["kind"] == "RETURN"
            and row.get("symbol") == symbol and start <= row["original_ns"] < end]
        not_ready, ready = {}, []
        for row in selected:
            bar = row["closed_bar_ms"]
            if bar % 60000 or (bar + 60000) * 1000000 > row["original_ns"]:
                raise ValueError("OLD_closed_bar_clock_contradiction")
            if row["reason"] == "track_line_not_ready" and (bar + 60000) * 1000000 >= first_boundary:
                not_ready.setdefault(bar, row)
            elif row["reason"] in OLD_READY_RETURNS:
                ready.append(row)
        first = min(ready, key=lambda row: row["original_ns"]) if ready else None
        member_rows = [row for row in records if row["kind"] == "MEMBERSHIP" and start <= row["original_ns"] < end]
        actual_member = any(symbol in row["sampled_names"] for row in member_rows)
        # A truncated sample cannot prove nonmembership. No membership update
        # in a window likewise does not prove exclusion/absence.
        membership = "OBSERVED_V2_SAMPLE_MEMBER" if actual_member else "UNKNOWN_NO_POSITIVE_V2_MEMBERSHIP_WITNESS"
        references = [OLD_LOG_FILE + ":line:" + str(row["ordinal"]) for row in not_ready.values()]
        if first is not None:
            references.append(OLD_LOG_FILE + ":line:" + str(first["ordinal"]))
        if not references:
            references = [OLD_LOG_FILE + ":WINDOW_NO_POSITIVE_READY_WITNESS:" + confirmation["event_id"]]
        windows[confirmation["event_id"]] = dict(original_confirm_ns=start, original_end_ns=end,
            original_first_ready_ns=first["original_ns"] if first is not None else None,
            not_ready_closed_minutes=len(not_ready), ready_status="READY" if first is not None else "UNKNOWN",
            raw_line_references=references)
        details.append(dict(event_id=confirmation["event_id"], symbol=symbol,
            scanner_CONFIRM_is_not_V2_membership=True,
            actual_V2_membership=membership, membership_log_lines=[row["ordinal"] for row in member_rows],
            not_ready_records=list(not_ready.values()), first_observed_line_ready_return=first,
            zero_count_is_not_ready_proof=not not_ready and first is None))
    return dict(status="INDEPENDENT_OLD_WINDOW_CANDIDATE_REQUIRES_MAIN_REVIEW",
        baseline_candidate=dict(scope="INDEPENDENT_LIVE_OLD_LOG_PER_CONFIRM_WINDOWS",
            raw_log_sha256=OLD_LOG_SHA, review_sha256=review_sha256,
            scanner_sha256=FLEET_FILES["scanner_confirmed_events_2026-10-23.csv"], windows=windows),
        details=details, scanner_denominator=18, missing_confirm_names=fleet["missing_confirm_names"],
        prior_session_context=[row for row in records
            if row["session_context"] == "BEFORE_FRIDAY_04_ROLL_NOT_CURRENT_FLEET_PROOF"],
        membership_records=[row for row in records if row["kind"] == "MEMBERSHIP"],
        account_leg_context=[row for row in records if row["kind"] == "ACCOUNT_LEG_CONTEXT_NOT_READINESS"],
        permission_granted=False, native_availability="UNKNOWN")


def split_delivered_old_fleet_log(raw, fleet, *, review_sha256):
    if type(raw) is not bytes or sha256(raw).hexdigest() != OLD_LOG_SHA:
        raise ValueError("exact_delivered_OLD_log_SHA_required")
    records = parse_old_log_lines(raw)
    if len(records) != 5107:
        raise ValueError("complete_5107_OLD_log_lines_required")
    return old_window_candidates_from_records(records, fleet, review_sha256=review_sha256)


C11810_CARRIED = frozenset(("AARD", "NCPL", "STAK", "POM", "SAIQ", "VEEA", "DKI"))
SOURCE_ANCHOR_MS = 1792742400000
SOURCE_END_NS = 1792800000000 * 1000000


def c11810_window_geometry(fleet, records, *, ruling_sha256):
    """SOURCE geometry, not inferred membership or manufactured CONFIRM events.

    FADE alone never terminates a window. A positive OLD member sample followed
    by a complete absent sample can witness an actual drop; both clocks remain.
    """
    _digest(ruling_sha256)
    if (fleet.get("names") != list(FLEET_NAMES)
            or set(fleet["missing_confirm_names"]) != C11810_CARRIED):
        raise ValueError("C11810_exact18_and_seven_reviewed_carried_names_required")
    memberships = sorted((row for row in records if row["kind"] == "MEMBERSHIP"),
        key=lambda row: (row["original_ns"], row["ordinal"]))
    windows = []
    for name in FLEET_NAMES:
        events = fleet["chronology"][name]
        confirms = [row for row in events if row["event_type"] == "CONFIRM"]
        starts = [dict(event_id=row["event_id"], kind="RECORDED_CONFIRM",
            original_ns=row["original_ns"], raw_ref=row["raw_ref"]) for row in confirms]
        if name in C11810_CARRIED:
            starts.insert(0, dict(event_id="C11810_CARRIED_BOOT:" + name, kind="CARRIED_IN_BOOT",
                original_ns=SOURCE_ANCHOR_MS * 1000000,
                raw_ref="C11810_REVIEWED_CARRIED_DISPOSITION:" + name))
        for index, start in enumerate(starts):
            end = starts[index + 1]["original_ns"] if index + 1 < len(starts) else SOURCE_END_NS
            fades = [row for row in events if row["event_type"] in {"FADE", "RETENTION_DROP"}
                and start["original_ns"] <= row["original_ns"] < end]
            drops = []
            for fade in fades:
                before = [row for row in memberships if row["original_ns"] < fade["original_ns"]]
                prior = before[-1] if before else None
                if prior is None or name not in prior["sampled_names"]:
                    continue
                # The FIRST subsequent membership observation must be complete
                # and absent. Do not search past a contradictory/partial sample
                # for a more favorable boundary.
                subsequent = [row for row in memberships
                    if fade["original_ns"] <= row["original_ns"] < end]
                following = subsequent[0] if subsequent else None
                if (following is not None and following["sample_is_complete"]
                        and name not in following["sampled_names"]):
                    drops.append(dict(fade_ref=fade["raw_ref"], fade_ns=fade["original_ns"],
                        present_line=prior["ordinal"], absent_line=following["ordinal"],
                        present_membership_record=deepcopy(prior), absent_membership_record=deepcopy(following),
                        actual_drop_observed_ns=following["original_ns"]))
            drop = min(drops, key=lambda row: row["actual_drop_observed_ns"]) if drops else None
            actual_end = drop["actual_drop_observed_ns"] if drop else end
            bars = [row for row in fleet.get("file_rows", {}).get(name, [])
                if SOURCE_ANCHOR_MS <= row["stamp"] and (row["stamp"] + 60000) * 1000000 <= actual_end]
            first = min(bars, key=lambda row: row["stamp"]) if bars else None
            count_start = ((first["stamp"] + 60000) * 1000000 if first is not None else None
                ) if start["kind"] == "CARRIED_IN_BOOT" else (
                    (start["original_ns"] + 59999999999) // 60000000000 * 60000000000)
            windows.append(dict(start, symbol=name, original_end_ns=actual_end,
                uninterrupted_end_ns=end, old_actual_drop=drop,
                recorded_fades=[dict(raw_ref=row["raw_ref"], original_ns=row["original_ns"])
                    for row in fades], count_start_original_ns=count_start,
                first_recorded_session_bar=(dict(stamp=first["stamp"], raw_ref=first["raw_ref"],
                    raw_sha256=first["raw_sha256"], scope="RECORDED_BAR_NOT_ORIGINAL_HTTP_ARRIVAL")
                    if first is not None else None), ruling_sha256=ruling_sha256,
                membership="NOT_ESTABLISHED_BY_SCANNER_OR_CARRIED_DISPOSITION"))
    return windows


def c11810_old_window_candidate(raw, fleet, *, ruling_sha256):
    """Exact delivered OLD bytes only. Candidate requires independent Main review."""
    if type(raw) is not bytes or sha256(raw).hexdigest() != OLD_LOG_SHA:
        raise ValueError("exact_delivered_OLD_log_SHA_required")
    records = parse_old_log_lines(raw)
    if len(records) != 5107:
        raise ValueError("complete_5107_OLD_log_lines_required")
    result = c11810_candidate_from_records(fleet, records, ruling_sha256=ruling_sha256)
    result.update(raw_log_sha256=OLD_LOG_SHA, record_origin="ACTUAL_FIXED_SHA_AND_LINE_COUNT_DELIVERY")
    return result


def c11810_candidate_from_records(fleet, records, *, ruling_sha256):
    """CONTROLLED seam; not evidence that controlled records are actual delivery."""
    windows = c11810_window_geometry(fleet, records, ruling_sha256=ruling_sha256)
    for window in windows:
        selected = [row for row in records if row["kind"] == "RETURN"
            and row["symbol"] == window["symbol"]
            and window["original_ns"] <= row["original_ns"] < window["original_end_ns"]]
        not_ready, positive = {}, []
        for row in selected:
            close = (row["closed_bar_ms"] + 60000) * 1000000
            if row["closed_bar_ms"] % 60000 or close > row["original_ns"]:
                raise ValueError("OLD_closed_bar_clock_contradiction")
            if row["reason"] in OLD_READY_RETURNS:
                positive.append(row)
            if (row["reason"] == "track_line_not_ready"
                    and window["count_start_original_ns"] is not None
                    and close >= window["count_start_original_ns"]):
                not_ready.setdefault(row["closed_bar_ms"], row)
        first = min(positive, key=lambda row: row["original_ns"]) if positive else None
        window["OLD"] = dict(not_ready_closed_minutes=(len(not_ready)
            if window["count_start_original_ns"] is not None else None),
            actual_returns=list(not_ready.values()), first_observed_ready_return=first,
            ready_status="OBSERVED_READY_PREDICATE" if first else "NO_OBSERVED_READY_UNKNOWN_NOT_ZERO_READY")
    return dict(version="C11810_OLD_WINDOW_CANDIDATE_V2", ruling_sha256=ruling_sha256,
        raw_log_sha256=None, record_origin="CONTROLLED_GEOMETRY_NOT_VALIDATED_LOG_DELIVERY",
        scanner_sha256=FLEET_FILES[next(iter(FLEET_FILES))],
        seed_denominator=18, carried_in_names=sorted(C11810_CARRIED), windows=windows,
        account_leg_context=[deepcopy(row) for row in records
            if row["kind"] == "ACCOUNT_LEG_CONTEXT_NOT_READINESS"],
        chronology_records_unchanged=True, native_availability="UNKNOWN",
        status="REQUIRES_INDEPENDENT_MAIN_WINDOW_REVIEW_NOT_PERMISSION", permission_granted=False)


def c11810_candidate_sha256(candidate):
    return sha256(json.dumps(candidate, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, allow_nan=False).encode()).hexdigest()


class C11810SeedTracker:
    """Reviewed revised windows only. No forced watch/seed/permission state."""

    def __init__(self, fleet, candidate, *, policy_review):
        fields = {"version", "candidate_sha256", "ruling_sha256", "review_sha256"}
        candidate_sha = c11810_candidate_sha256(candidate)
        if (type(policy_review) is not dict or set(policy_review) != fields
                or policy_review["version"] != "C11810_INDEPENDENT_WINDOW_REVIEW_V1"
                or policy_review["candidate_sha256"] != candidate_sha
                or policy_review["ruling_sha256"] != candidate.get("ruling_sha256")
                or candidate.get("version") != "C11810_OLD_WINDOW_CANDIDATE_V2"
                or candidate.get("seed_denominator") != 18
                or candidate.get("carried_in_names") != sorted(C11810_CARRIED)):
            raise ValueError("explicit_C11810_independent_candidate_review_required")
        _digest(policy_review["review_sha256"])
        _digest(policy_review["ruling_sha256"])
        expected_ids = {row["event_id"] for row in fleet["confirmations"]} | {
            "C11810_CARRIED_BOOT:" + name for name in C11810_CARRIED}
        windows = candidate.get("windows")
        if (type(windows) is not list or len(windows) != len(expected_ids)
                or {row["event_id"] for row in windows} != expected_ids
                or {row["symbol"] for row in windows} != set(FLEET_NAMES)
                or policy_review["review_sha256"] == "0" * 64
                or policy_review["ruling_sha256"] == "0" * 64):
            raise ValueError("every_actual_CONFIRM_and_seven_carried_windows_required_no_0_of_0")
        self.fleet, self.candidate, self.policy_review = deepcopy(fleet), deepcopy(candidate), deepcopy(policy_review)
        self.mapping = RecordedSessionMap(**{key: fleet["mapping"][key]
            for key in ("target_day", "ruling_sha256", "chronology_review_sha256")})
        self.windows = deepcopy(candidate["windows"])
        self.not_ready, self.first_seed, self.first_ready = {}, {}, {}
        self.last_clock_ns = None

    def active(self, symbol, clock_ns):
        for window in self.windows:
            if (window["symbol"] == symbol
                    and self.mapping.market_ns(window["original_ns"]) <= clock_ns
                    < self.mapping.market_ns(window["original_end_ns"])):
                yield window

    def observe_return_reason(self, *, symbol, reason, clock_ns, closed_bar_ms):
        if reason != "track_line_not_ready":
            return
        if (symbol not in FLEET_NAMES or type(clock_ns) is not int or type(closed_bar_ms) is not int
                or closed_bar_ms % 60000 or (closed_bar_ms + 60000) * 1000000 > clock_ns):
            raise ValueError("actual_closed_bar_reason_clock_required")
        for window in self.active(symbol, clock_ns):
            start = window["count_start_original_ns"]
            if start is not None and (closed_bar_ms + 60000) * 1000000 >= self.mapping.market_ns(start):
                self.not_ready.setdefault(window["event_id"], {}).setdefault(closed_bar_ms,
                    dict(clock_ns=clock_ns, closed_bar_ms=closed_bar_ms, reason=reason))

    def observe(self, sample):
        clock_ns = sample.get("observed_clock_ns", sample.get("observed_ms", 0) * 1000000)
        if (sample.get("status") != "OBSERVED_SEED_SAMPLE_NOT_FULL_TIMELINE_QUALIFICATION"
                or type(clock_ns) is not int or clock_ns // 1000000 != sample["observed_ms"]
                or self.last_clock_ns is not None and clock_ns < self.last_clock_ns):
            raise ValueError("actual_causal_seed_sample_required")
        self.last_clock_ns = clock_ns
        for row in sample["symbols"]:
            if row.get("symbol") not in FLEET_NAMES:
                raise ValueError("actual_observation_outside_C11810_population_not_silently_dropped")
            if row["seed_complete"] is not True:
                continue
            if row.get("history_anchor_ms") != self.mapping.market_ms(SOURCE_ANCHOR_MS):
                raise ValueError("actual_seed_session_anchor_contradiction")
            for window in self.active(row["symbol"], clock_ns):
                observation = dict(clock_ns=clock_ns, seed_complete=True,
                    history_completed_ns=row.get("history_completed_ns"))
                self.first_seed.setdefault(window["event_id"], observation)
                if row.get("line_buy_ready") is True:
                    self.first_ready.setdefault(window["event_id"], observation)

    def report(self):
        results = []
        for window in self.windows:
            identity = window["event_id"]
            count = len(self.not_ready.get(identity, {}))
            seed, ready, old = self.first_seed.get(identity), self.first_ready.get(identity), window["OLD"]
            comparable = (old["first_observed_ready_return"] is not None
                and old["not_ready_closed_minutes"] is not None)
            start = window["count_start_original_ns"]
            before_bar = seed is not None and start is not None and seed["clock_ns"] < self.mapping.market_ns(start)
            elapsed = (seed["clock_ns"] // 60000000000
                - self.mapping.market_ns(start) // 60000000000
                if seed is not None and start is not None and not before_bar else None)
            outcome = ("FAIL_NEW_OVER_10" if count > 10 else
                "FAIL_CARRIED_SEED_OVER_10_AFTER_FIRST_BAR" if window["kind"] == "CARRIED_IN_BOOT"
                    and elapsed is not None and elapsed > 10 else
                "UNKNOWN_FIRST_SESSION_BAR" if start is None else
                "UNKNOWN_SEED_BEFORE_FIRST_RECORDED_BAR_NOT_ZERO" if before_bar else
                "FAIL_NEW_SLOWER_THAN_OLD" if comparable
                    and count > old["not_ready_closed_minutes"] else
                "UNKNOWN_SEED_OR_READY_UNOBSERVED" if seed is None or ready is None else
                "OBSERVED_WITHIN_ABSOLUTE_CAP_OLD_COMPARISON_UNKNOWN_NOT_INSTALL_PERMISSION" if not comparable else
                "OBSERVED_WITHIN_LIMITS_NOT_INSTALL_PERMISSION")
            results.append(dict(window=deepcopy(window), NEW_not_ready_closed_minutes=count,
                actual_NEW_returns=list(self.not_ready.get(identity, {}).values()),
                first_seed=seed, first_ready=ready, seed_closed_minutes_after_count_anchor=elapsed,
                OLD_comparison=("UNKNOWN_NO_POSITIVE_OLD_READY_WITNESS" if not comparable else
                    "FAIL_NEW_SLOWER_THAN_OLD" if count > old["not_ready_closed_minutes"] else
                    "OBSERVED_NEW_NOT_GREATER_THAN_OLD"),
                applied_not_ready_limit=(min(10, old["not_ready_closed_minutes"]) if comparable else 10),
                outcome=outcome))
        names = {name: [row for row in results if row["window"]["symbol"] == name] for name in FLEET_NAMES}
        seeded_names = [name for name, rows in names.items()
            if rows and all(row["first_seed"] is not None for row in rows)]
        return dict(version="C11859_RETIMED_BOOT_WINDOWS_V3", seed_denominator=18,
            per_name=names, windows=results, carried_in_names=sorted(C11810_CARRIED),
            observed_seed_count=len(seeded_names), observed_seed_names=seeded_names,
            window_denominator=len(results),
            positive_OLD_comparison_windows=sum(row["OLD_comparison"] !=
                "UNKNOWN_NO_POSITIVE_OLD_READY_WITNESS" for row in results),
            UNKNOWN_OLD_comparison_windows=sum(row["OLD_comparison"] ==
                "UNKNOWN_NO_POSITIVE_OLD_READY_WITNESS" for row in results),
            new_gate_failures=[row["window"]["event_id"] for row in results if row["outcome"].startswith("FAIL_")],
            new_gate_unresolved=[row["window"]["event_id"] for row in results if row["outcome"].startswith("UNKNOWN_")],
            count_context_not_gate={name: dict(OLD_C11859_context_minutes=old_count,
                NEW_sum_window_not_ready_minutes=sum(row["NEW_not_ready_closed_minutes"] for row in names[name]),
                NEW_window_counts=[dict(event_id=row["window"]["event_id"],
                    count=row["NEW_not_ready_closed_minutes"], OLD_comparison=row["OLD_comparison"])
                    for row in names[name]], scope="SCOPED_WINDOW_SUM_NOT_INVENTED_WHOLE_DAY_COMPARISON")
                for name, old_count in (("YMAT", 30), ("BOLT", 10))},
            unobserved_seed_windows=[row["window"]["event_id"] for row in results if row["first_seed"] is None],
            raw_scanner_confirmations=len(self.fleet["confirmations"]),
            raw_scanner_events=sum(len(rows) for rows in self.fleet["chronology"].values()),
            policy_review=deepcopy(self.policy_review), hard_cap_closed_minutes=10,
            comparison_criterion="C1_20261024_1859_POSITIVE_OLD_AND10_OTHERWISE_OLD_UNKNOWN_ABSOLUTE10",
            missing_ready_is_not_zero=True, permission_granted=False,
            native_availability="UNKNOWN_NO_RETIMED_NATIVE_HEADERS",
            first_bar_scope="FIRST_RECORDED_SESSION_BAR_NOT_ORIGINAL_PROVIDER_ARRIVAL")


def classify_c11229_individual_blocks(seed_report, witnesses, *, mapping, ruling_sha256,
                                      report_guard, witness_guard):
    """Explicit post-observation disposition, not seed/entry mutation or proof IO.

    Preserve every original seed result and all34 windows. Independent Main
    guards must bind the complete actual run, ALL actual exclusions, retained
    input and no-entry/release evidence. A CONTROLLED fault is not a real34
    input exception; omitted actual exclusions cannot yield a favorable report.
    """
    _digest(ruling_sha256)
    if (ruling_sha256 == "0" * 64 or type(mapping) is not RecordedSessionMap or type(seed_report) is not dict
            or seed_report.get("version") != "C11859_RETIMED_BOOT_WINDOWS_V3"
            or seed_report.get("seed_denominator") != 18
            or seed_report.get("window_denominator") != 34
            or seed_report.get("permission_granted") is not False
            or type(seed_report.get("windows")) is not list or len(seed_report["windows"]) != 34
            or type(witnesses) is not list or len(witnesses) > 34
            or not callable(report_guard) or not callable(witness_guard)):
        raise ValueError("C11229_complete_actual34_and_independent_review_required")
    windows = seed_report["windows"]
    ids = [row["window"]["event_id"] for row in windows]
    outcomes = {"FAIL_NEW_OVER_10", "FAIL_CARRIED_SEED_OVER_10_AFTER_FIRST_BAR",
        "UNKNOWN_FIRST_SESSION_BAR", "UNKNOWN_SEED_BEFORE_FIRST_RECORDED_BAR_NOT_ZERO",
        "FAIL_NEW_SLOWER_THAN_OLD", "UNKNOWN_SEED_OR_READY_UNOBSERVED",
        "OBSERVED_WITHIN_ABSOLUTE_CAP_OLD_COMPARISON_UNKNOWN_NOT_INSTALL_PERMISSION",
        "OBSERVED_WITHIN_LIMITS_NOT_INSTALL_PERMISSION"}
    if (len(set(ids)) != 34 or {row["window"]["symbol"] for row in windows} != set(FLEET_NAMES)
            or any(row.get("outcome") not in outcomes for row in windows)
            or report_guard(deepcopy(seed_report), deepcopy(witnesses)) is not True):
        raise ValueError("C11229_actual_denominator_or_source_report_unbound")
    by_id = {row["window"]["event_id"]: row["window"] for row in windows}
    accepted = {}
    required = {"version", "event_id", "symbol", "exclusion_clock_ns", "window_start_ns",
        "window_end_ns", "observed_through_ns", "reason", "input", "input_origin",
        "input_reference", "input_sha256", "witness_sha256", "review_sha256",
        "actual_exclusion", "symbol_unknown", "symbol_entry_blocked", "entry_count",
        "rest_fleet_released"}
    for witness in witnesses:
        if (type(witness) is not dict or set(witness) != required
                or witness["version"] != "C11229_REAL_INPUT_INDIVIDUAL_BLOCK_V1"
                or witness["event_id"] not in by_id or witness["event_id"] in accepted
                or witness["symbol"] != by_id[witness["event_id"]]["symbol"]
                or witness["input_origin"] != "ACTUAL_RETAINED_INPUT_OR_REAL_READER_RESULT"
                or type(witness["input_reference"]) is not str or not witness["input_reference"]
                or any(witness[key] is not True for key in ("actual_exclusion", "symbol_unknown",
                    "symbol_entry_blocked", "rest_fleet_released"))
                or type(witness["entry_count"]) is not int or witness["entry_count"] != 0
                or any(type(witness[key]) is not int for key in ("exclusion_clock_ns",
                    "window_start_ns", "window_end_ns", "observed_through_ns"))
                or not 0 < witness["window_start_ns"] <= witness["exclusion_clock_ns"]
                    < witness["window_end_ns"] <= witness["observed_through_ns"]):
            raise ValueError("C11229_real_own_UNKNOWN_no_entry_released_fleet_full_window_required")
        for key in ("input_sha256", "witness_sha256", "review_sha256"):
            _digest(witness[key])
            if witness[key] == "0" * 64:
                raise ValueError("C11229_actual_input_and_complete_output_reviews_required")
        raw = witness["input"]
        if type(raw) is not dict:
            raise ValueError("C11229_exact_input_reason_operand_required")
        if witness["reason"] == "STALE_BAR_OVER_180_SECONDS":
            if (set(raw) != {"bar_ms", "observed_ms"} or type(raw["bar_ms"]) is not int
                    or type(raw["observed_ms"]) is not int or raw["bar_ms"] <= 0
                    or raw["observed_ms"] != witness["exclusion_clock_ns"] // 1000000
                    or raw["observed_ms"] - raw["bar_ms"] <= 180000):
                raise ValueError("C11229_stale_must_be_strictly_over180_seconds")
        elif witness["reason"] == "HISTORY_READ_ERROR":
            if (set(raw) != {"exception_type"} or type(raw["exception_type"]) is not str
                    or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", raw["exception_type"]) is None):
                raise ValueError("C11229_actual_reader_error_type_required")
        elif witness["reason"] == "UNPROVEN_PRODUCER":
            if set(raw) != {"producer_proven"} or raw["producer_proven"] is not False:
                raise ValueError("C11229_actual_unproven_producer_required")
        else:
            raise ValueError("C11229_unknown_or_unsupported_individual_block_reason")
        # Main binds these mapped interval operands to the exact original window
        # and complete run; supplied timestamps alone cannot prove that binding.
        window = by_id[witness["event_id"]]
        if (mapping.market_ns(window["original_ns"]) != witness["window_start_ns"]
                or mapping.market_ns(window["original_end_ns"]) != witness["window_end_ns"]):
            raise ValueError("C11229_window_clock_differs_from_reviewed_mapping")
        if witness_guard(deepcopy(window), deepcopy(witness)) is not True:
            raise ValueError("C11229_input_window_and_no_entry_release_witness_unbound")
        accepted[witness["event_id"]] = deepcopy(witness)
    results = deepcopy(windows)
    for row in results:
        row["original_seed_outcome"] = row["outcome"]
        evidence = accepted.get(row["window"]["event_id"])
        if evidence is not None:
            row.update(outcome="EXPECTED_INDIVIDUAL_BLOCK", individual_block_witness=evidence,
                seed_PASS_claimed=False, symbol_admission_granted=False)
    return dict(version="C11229_REAL_UNKNOWN_SEED_DISPOSITIONS_V4", original_seed_report=deepcopy(seed_report),
        windows=results, seed_denominator=18, window_denominator=34,
        expected_individual_blocks=sorted(accepted),
        new_gate_failures=[row["window"]["event_id"] for row in results if row["outcome"].startswith("FAIL_")],
        new_gate_unresolved=[row["window"]["event_id"] for row in results if row["outcome"].startswith("UNKNOWN_")],
        other_seed_limits_unchanged=True, ruling_sha256=ruling_sha256, permission_granted=False)
