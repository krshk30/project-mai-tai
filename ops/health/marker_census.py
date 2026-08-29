#!/usr/bin/env python3
"""One-window census for fireable fleet markers and their denominators.

This is a census, not an acceptance gate.  It never promotes a quiet marker to
PASS.  Each line names the population that could have produced the marker and
states what zero means.  The report deliberately keeps these states distinct:

* OBSERVED -- a non-zero population made the line readable;
* UNEXERCISED -- the denominator was zero, including a gate never reached;
* COULD_NOT_TELL -- the required evidence was unreadable or no exact
  denominator exists;
* EXPECTED_ZERO -- DB3's production population is rare by construction and its
  correctness is mutation-proven, not graded from a live zero;
* BLOCKED / FAIL -- the measured population reached a known bad state.

W3 (the raw-invalid refusal watch) is intentionally absent.  Its population is
structurally zero under the currently accepted order construction, so it is not
a fireable check and does not belong in a marker census.

Exit codes:
  0  REPORT_COMPLETE       every requested evidence source answered
  2  REFUSED               malformed or empty time window
  3  COULD_NOT_TELL        at least one evidence source could not be read
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import gzip
import importlib.util
import io
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable, Iterable, Mapping, Sequence


REPORT_COMPLETE = 0
REFUSED = 2
COULD_NOT_TELL = 3

LOG_DIR = Path("/var/log/project-mai-tai")
V2_SERVICE = "schwab-1m-v2"
OMS_SERVICE = "oms"

LOG_TS_RE = re.compile(r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:,(?P<micros>\d{1,6}))?")
BROKER_CENSUS_ACCOUNT_RE = re.compile(
    r"(?P<account>[^|]+?): ok=(?P<ok>\d+) failed=(?P<failed>\d+) "
    r"consecutive_now=(?P<consecutive>\d+)"
)
DEFERRED_RE = re.compile(r"\[VIRTUAL-CLEAR-DEFERRED\] deferred=(\d+) of unbacked_positive=(\d+)")
CLEARED_RE = re.compile(
    r"\[VIRTUAL-CLEAR\] zeroed (\d+) virtual position\(s\).*evaluated_unbacked=(\d+)"
)


DATABASE_SQL = r"""
BEGIN READ ONLY;
COPY (
WITH
bounds AS (
    SELECT :'window_since'::timestamptz AS since_at,
           :'window_until'::timestamptz AS until_at
),
event_rows AS (
    SELECT count(*)::bigint AS n
    FROM broker_order_events e, bounds b
    WHERE e.event_at >= b.since_at AND e.event_at < b.until_at
),
terminal_evidence AS (
    SELECT
        bo.id AS order_id,
        bo.client_order_id,
        min(e.event_at) AS first_terminal_at
    FROM broker_order_events e
    JOIN broker_orders bo ON bo.id = e.order_id
    WHERE e.event_type = 'rejected'
      AND upper(regexp_replace(trim(coalesce(e.payload::jsonb->>'reason', '')), '\s+', ' ', 'g'))
          IN ('ORDER IN STATE CANCELED CANNOT BE CANCELED',
              'ORDER IN STATE FILLED CANNOT BE CANCELED')
    GROUP BY bo.id, bo.client_order_id
),
terminal_targets AS (
    SELECT count(*)::bigint AS n
    FROM terminal_evidence t, bounds b
    WHERE t.first_terminal_at < b.until_at
),
strategy_cancel_followups AS (
    SELECT count(*)::bigint AS n
    FROM trade_intents ti
    JOIN terminal_evidence t
      ON coalesce(
             ti.payload::jsonb->'metadata'->>'target_client_order_id',
             ti.payload::jsonb->'metadata'->>'client_order_id'
         ) = t.client_order_id
    CROSS JOIN bounds b
    WHERE ti.intent_type = 'cancel'
      AND ti.created_at >= b.since_at AND ti.created_at < b.until_at
      AND ti.created_at > t.first_terminal_at
),
session_days AS (
    SELECT day::date AS et_day
    FROM bounds b,
         LATERAL generate_series(
             (b.since_at AT TIME ZONE 'America/New_York')::date,
             ((b.until_at - interval '1 microsecond') AT TIME ZONE 'America/New_York')::date,
             interval '1 day'
         ) day
),
probes AS (
    -- Anchor the observational grid at 07:00 ET each session. Anchoring at --since would silently
    -- change the sampled population when the caller chose midnight, 04:00, or another start time.
    SELECT p AS probe_at
    FROM session_days d
    CROSS JOIN bounds b
    CROSS JOIN LATERAL generate_series(
        (d.et_day + time '07:00') AT TIME ZONE 'America/New_York',
        (d.et_day + time '13:00') AT TIME ZONE 'America/New_York',
        interval '3 hours'
    ) p
    WHERE extract(isodow FROM d.et_day) BETWEEN 1 AND 5
      AND p >= b.since_at AND p < b.until_at
),
active_intents AS (
    SELECT
        p.probe_at,
        upper(ti.symbol) AS symbol,
        ba.name AS account
    FROM probes p
    JOIN trade_intents ti
      ON ti.created_at <= p.probe_at
     AND greatest(ti.updated_at, ti.created_at + interval '1 microsecond') > p.probe_at
    JOIN broker_accounts ba ON ba.id = ti.broker_account_id
    JOIN strategies s ON s.id = ti.strategy_id
    WHERE s.code = 'schwab_1m_v2'
      AND ti.side = 'buy'
      AND ti.intent_type IN ('open', 'scale')
      AND ba.name IN ('live:orb', 'live:schwab_1m_v2')
),
probe_symbols AS (
    SELECT
        probe_at,
        symbol,
        bool_or(account = 'live:orb') AS has_fanout,
        bool_or(account = 'live:schwab_1m_v2') AS has_primary
    FROM active_intents
    GROUP BY probe_at, symbol
),
db3 AS (
    SELECT
        count(*)::bigint AS evaluated,
        count(*) FILTER (WHERE has_fanout AND NOT has_primary)::bigint AS excluded
    FROM probe_symbols
)
SELECT 'order_event_rows' AS metric, n AS value FROM event_rows
UNION ALL SELECT 'terminal_target_episodes', n FROM terminal_targets
UNION ALL SELECT 'strategy_cancel_followups', n FROM strategy_cancel_followups
UNION ALL SELECT 'db3_probe_symbols', evaluated FROM db3
UNION ALL SELECT 'db3_fanout_only_excluded', excluded FROM db3
ORDER BY metric
) TO STDOUT WITH (FORMAT CSV, HEADER TRUE);
ROLLBACK;
"""


class EvidenceFailure(RuntimeError):
    """A required read-only evidence source did not answer."""


@dataclass(frozen=True)
class TimedLine:
    at: datetime
    text: str


@dataclass(frozen=True)
class CensusLine:
    name: str
    status: str
    fields: tuple[tuple[str, str], ...]
    zero_means: str

    def render(self) -> str:
        payload = " ".join(f"{key}={value}" for key, value in self.fields)
        return (
            f"[MARKER-CENSUS] metric={self.name} status={self.status} {payload} "
            f"zero_means={self.zero_means}"
        )


@dataclass(frozen=True)
class CensusReport:
    exit_code: int
    verdict: str
    lines: tuple[str, ...]


def parse_instant(raw: str, label: str) -> datetime:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO instant: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must carry Z or an explicit UTC offset")
    return value.astimezone(UTC)


def parse_log_timestamp(line: str) -> datetime | None:
    match = LOG_TS_RE.match(line)
    if match is None:
        return None
    micros = (match.group("micros") or "0").ljust(6, "0")
    return datetime.strptime(f"{match.group('at')}.{micros}", "%Y-%m-%d %H:%M:%S.%f").replace(
        tzinfo=UTC
    )


def window_lines(lines: Iterable[str], *, since: datetime, until: datetime) -> list[TimedLine]:
    selected: list[TimedLine] = []
    for raw in lines:
        at = parse_log_timestamp(raw)
        if at is not None and since <= at < until:
            selected.append(TimedLine(at=at, text=raw.rstrip("\n")))
    return sorted(selected, key=lambda item: (item.at, item.text))


def exact_marker(lines: Sequence[TimedLine], marker: str) -> list[TimedLine]:
    token = f"[{marker}]"
    return [line for line in lines if token in line.text]


def _read_path(path: str) -> list[str]:
    result = subprocess.run(
        ["sudo", "-n", "cat", "--", path],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise EvidenceFailure(
            result.stderr.decode(errors="replace").strip() or f"could not read {path}"
        )
    payload = result.stdout
    if path.endswith(".gz"):
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise EvidenceFailure(f"could not decompress {path}: {exc}") from exc
    return payload.decode(errors="replace").splitlines()


def read_service_logs(service: str, log_dir: Path = LOG_DIR) -> list[str]:
    result = subprocess.run(
        [
            "sudo",
            "-n",
            "find",
            str(log_dir),
            "-maxdepth",
            "1",
            "-type",
            "f",
            "-name",
            f"{service}.log*",
            "-print0",
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise EvidenceFailure(
            result.stderr.decode(errors="replace").strip() or f"could not enumerate {service} logs"
        )
    paths = [raw.decode() for raw in result.stdout.split(b"\0") if raw]
    if not paths:
        raise EvidenceFailure(f"no {service}.log files were found")
    lines: list[str] = []
    for path in paths:
        lines.extend(_read_path(path))
    return lines


def parse_database_counts(raw: str) -> dict[str, int]:
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames != ["metric", "value"]:
        raise EvidenceFailure("database census header is not metric,value")
    counts: dict[str, int] = {}
    for row in reader:
        metric = row["metric"]
        if metric in counts:
            raise EvidenceFailure(f"database census repeated metric {metric!r}")
        try:
            value = int(row["value"])
        except ValueError as exc:
            raise EvidenceFailure(f"database census {metric!r} is not an integer") from exc
        if value < 0:
            raise EvidenceFailure(f"database census {metric!r} is negative")
        counts[metric] = value
    expected = {
        "order_event_rows",
        "terminal_target_episodes",
        "strategy_cancel_followups",
        "db3_probe_symbols",
        "db3_fanout_only_excluded",
    }
    if set(counts) != expected:
        raise EvidenceFailure("database census rows do not match the fixed metric set")
    if counts["db3_fanout_only_excluded"] > counts["db3_probe_symbols"]:
        raise EvidenceFailure("DB3 excluded count exceeds its probe-symbol denominator")
    return counts


def query_database_counts(since: datetime, until: datetime) -> dict[str, int]:
    command = [
        "sudo",
        "-n",
        "-u",
        "postgres",
        "psql",
        "-X",
        "-qAt",
        "-v",
        "ON_ERROR_STOP=1",
        "-v",
        f"window_since={since.isoformat()}",
        "-v",
        f"window_until={until.isoformat()}",
        "-d",
        "project_mai_tai",
        "-f",
        "-",
    ]
    try:
        result = subprocess.run(
            command,
            input=DATABASE_SQL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise EvidenceFailure(f"could not execute database census: {exc}") from exc
    if result.returncode != 0:
        raise EvidenceFailure(result.stderr.strip() or "psql exited non-zero without an error")
    if result.stderr.strip():
        raise EvidenceFailure(f"psql wrote unexpected stderr: {result.stderr.strip()}")
    return parse_database_counts(result.stdout)


def _load_identity_module():
    path = Path(__file__).with_name("fanout_identity_acceptance.py")
    spec = importlib.util.spec_from_file_location("marker_census_fanout_identity", path)
    if spec is None or spec.loader is None:
        raise EvidenceFailure("could not load fanout_identity_acceptance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def identity_observation(
    since: datetime,
    until: datetime,
    v2_raw_lines: Sequence[str],
) -> tuple[str, Mapping[str, int | str]]:
    module = _load_identity_module()
    try:
        intents, attempts = module.query_database(since, until)
        starts = module.parse_process_starts(v2_raw_lines)
        report = module.evaluate(
            intents=intents,
            attempts=attempts,
            starts=starts,
            since=since,
            until=until,
        )
    except (module.EvidenceFailure, ValueError) as exc:
        raise EvidenceFailure(f"fan-out identity evidence failed: {exc}") from exc
    roots, max_depth, hard_chain_errors, continuity_errors = module._chain_metrics(attempts)
    return report.verdict, {
        "queued": len(intents),
        "submitted": len(attempts),
        "intent_identity_complete": sum(module._identity_error(item) is None for item in intents),
        "roots": roots,
        "max_chain_depth": max_depth,
        "chain_errors": len(hard_chain_errors) + len(continuity_errors),
    }


def _count(lines: Sequence[TimedLine], marker: str) -> int:
    return len(exact_marker(lines, marker))


def _sum_capture(lines: Sequence[TimedLine], pattern: re.Pattern[str], group: int) -> int:
    total = 0
    for line in lines:
        match = pattern.search(line.text)
        if match:
            total += int(match.group(group))
    return total


def build_lines(
    *,
    v2: Sequence[TimedLine],
    oms: Sequence[TimedLine],
    db: Mapping[str, int] | None,
    identity: tuple[str, Mapping[str, int | str]] | None,
    evidence_errors: Sequence[str] = (),
) -> list[CensusLine]:
    result: list[CensusLine] = []

    # W1 -- reactive fan-out suppression. The two exact marker tokens are siblings, not prefixes.
    suppressed = _count(v2, "V2-FANOUT-REACTIVE-SUPPRESSED")
    latched = _count(v2, "V2-FANOUT-REACTIVE-LATCHED")
    opportunities = suppressed + latched
    result.append(
        CensusLine(
            "w1_reactive_suppression",
            "UNEXERCISED" if opportunities == 0 else "OBSERVED",
            (("suppressed", str(suppressed)), ("opportunities", str(opportunities))),
            "UNEXERCISED(no_reactive_fanout_opportunity)"
            if opportunities == 0
            else "zero_suppressed_means_site_reached_but_no_duplicate_was_prevented",
        )
    )

    # W2 -- delegate identity semantics to the acceptance report rather than creating a second join.
    if identity is None:
        result.append(
            CensusLine(
                "w2_fanout_identity",
                "COULD_NOT_TELL",
                (("queued", "unknown"), ("identity_complete", "unknown")),
                "zero_cannot_be_interpreted_without_the_database_and_process_start_tape",
            )
        )
    else:
        identity_status, values = identity
        queued = int(values["queued"])
        complete = int(values["intent_identity_complete"])
        result.append(
            CensusLine(
                "w2_fanout_identity",
                identity_status,
                (
                    ("identity_complete", str(complete)),
                    ("queued", str(queued)),
                    ("submitted", str(values["submitted"])),
                    ("roots", str(values.get("roots", "unknown"))),
                    ("max_chain_depth", str(values.get("max_chain_depth", "unknown"))),
                    ("chain_errors", str(values.get("chain_errors", "unknown"))),
                ),
                "UNEXERCISED(no_queued_fanout_intent)"
                if queued == 0
                else "zero_complete_is_identity_failure_on_an_exercised_population",
            )
        )

    # W4 -- the census already carries the per-account denominator. Never sum venues together.
    broker_totals: dict[str, list[int]] = {}
    for line in exact_marker(oms, "BROKER-SYNC-CENSUS"):
        payload = line.text.split("[BROKER-SYNC-CENSUS]", 1)[1]
        for match in BROKER_CENSUS_ACCOUNT_RE.finditer(payload):
            account = match.group("account").strip()
            totals = broker_totals.setdefault(account, [0, 0])
            totals[0] += int(match.group("ok"))
            totals[1] += int(match.group("failed"))
    if not broker_totals:
        result.append(
            CensusLine(
                "w4_broker_sync",
                "UNEXERCISED",
                (("account", "none"), ("reads", "0"), ("failed", "0")),
                "UNEXERCISED(no_account_read_census_population)",
            )
        )
    else:
        for account, (ok, failed) in sorted(broker_totals.items()):
            reads = ok + failed
            result.append(
                CensusLine(
                    "w4_broker_sync",
                    "FAIL" if failed else ("OBSERVED" if reads else "UNEXERCISED"),
                    (("account", account), ("reads", str(reads)), ("failed", str(failed))),
                    "UNEXERCISED(no_reads_for_this_account)"
                    if reads == 0
                    else "zero_failed_is_readable_only_because_reads_is_nonzero",
                )
            )

    dropped = _count(oms, "OMS-ORDER-EVENT-DROPPED")
    if db is None:
        result.append(
            CensusLine(
                "w5_order_event_savepoint",
                "COULD_NOT_TELL",
                (("dropped", str(dropped)), ("attempted", "unknown")),
                "zero_dropped_has_no_meaning_without_the_event_row_denominator",
            )
        )
    else:
        attempted = db["order_event_rows"] + dropped
        result.append(
            CensusLine(
                "w5_order_event_savepoint",
                "UNEXERCISED" if attempted == 0 else ("FAIL" if dropped else "OBSERVED"),
                (("dropped", str(dropped)), ("attempted", str(attempted))),
                "UNEXERCISED(no_order_event_write_attempt)"
                if attempted == 0
                else "zero_dropped_means_all_observed_audit_rows_landed",
            )
        )

    # 824 -- the outcome is intentionally not forced to reconcile inside one arbitrary window.
    hold_started = _count(v2, "V2-FANOUT-CLAIM-ZERO-HOLD")
    hold_cancelled = _count(v2, "V2-FANOUT-CLAIM-ZERO-HOLD-CANCELLED")
    hold_expired = _count(v2, "V2-FANOUT-CLAIM-ZERO-HOLD-EXPIRED")
    result.append(
        CensusLine(
            "pr824_positive_zero_hold",
            "UNEXERCISED" if hold_started == 0 else "OBSERVED",
            (
                ("started", str(hold_started)),
                ("cancelled", str(hold_cancelled)),
                ("expired", str(hold_expired)),
            ),
            "UNEXERCISED(no_positive_evidence_union_zero_episode)"
            if hold_started == 0
            else "zero_cancelled_or_expired_is_not_a_pass;an_episode_may_cross_the_window",
        )
    )

    deferred = _sum_capture(oms, DEFERRED_RE, 1)
    cleared = _sum_capture(oms, CLEARED_RE, 1)
    unbacked = deferred + cleared
    result.append(
        CensusLine(
            "pr825_virtual_clear_deferred",
            "UNEXERCISED" if unbacked == 0 else "OBSERVED",
            (("deferred", str(deferred)), ("unbacked_positive", str(unbacked))),
            "UNEXERCISED(no_unbacked_positive_row)"
            if unbacked == 0
            else "zero_deferred_means_candidates_existed_but_none_was_inside_the_bound",
        )
    )

    intent_bound = _count(oms, "OMS-CANCEL-DEAD-TARGET-BOUND")
    direct_bound = _count(oms, "OMS-DIRECT-CANCEL-DEAD-TARGET-BOUND")
    if db is None:
        result.extend(
            (
                CensusLine(
                    "pr829_intent_cancel_bound",
                    "COULD_NOT_TELL",
                    (("refused", str(intent_bound)), ("followup_intents", "unknown")),
                    "zero_refused_has_no_meaning_without_followup_intent_population",
                ),
                CensusLine(
                    "pr832_direct_cancel_bound",
                    "COULD_NOT_TELL",
                    (("refused", str(direct_bound)), ("eligible_targets", "unknown")),
                    "zero_refused_has_no_meaning_without_terminal_target_population",
                ),
            )
        )
    else:
        followups = db["strategy_cancel_followups"]
        candidates = db["terminal_target_episodes"]
        result.append(
            CensusLine(
                "pr829_intent_cancel_bound",
                "UNEXERCISED" if followups == 0 else "OBSERVED",
                (("refused", str(intent_bound)), ("followup_intents", str(followups))),
                "UNEXERCISED(no_strategy_cancel_after_terminal_evidence)"
                if followups == 0
                else "zero_refused_means_followups_existed_but_no_bound_marker_was_observed",
            )
        )
        # Direct paths create no durable intent or per-attempt event. Terminal targets are only an
        # upper-bound population, never the exact per-path denominator. Therefore a missing bound
        # marker cannot establish a zero opportunity population: the denominator is absent by
        # instrumentation, not waiting for a better query.
        direct_status = "OBSERVED" if direct_bound else "COULD_NOT_TELL"
        result.append(
            CensusLine(
                "pr832_direct_cancel_bound",
                direct_status,
                (
                    ("refused", str(direct_bound)),
                    ("durable_terminal_targets_before_until", str(candidates)),
                    ("exact_path_denominator", "absent_by_instrumentation"),
                ),
                "zero_refused_is_permanently_could_not_tell_until_direct_attempts_are_instrumented",
            )
        )

    empty_population = sum(
        "reason=empty_evaluated_population_after_exclusions" in line.text
        or "reason=empty_tradeable_scanner_population" in line.text
        for line in exact_marker(v2, "V2-BOOT-RESTORE")
    )
    seed_incomplete = sum(
        "reason=state_seed_incomplete" in line.text for line in exact_marker(v2, "V2-BOOT-RESTORE")
    )
    rest_incomplete = sum(
        "reason=rest_warmup_incomplete" in line.text for line in exact_marker(v2, "V2-BOOT-RESTORE")
    )
    complete = sum(
        "restoration_complete=1 evaluated=" in line.text
        for line in exact_marker(v2, "V2-BOOT-RESTORE")
    )
    releases = sum(
        "released" in line.text and "restoration_complete=1" in line.text
        for line in exact_marker(v2, "V2-BOOT-HOLD")
    )

    population_reached = seed_incomplete + rest_incomplete + complete
    result.append(
        CensusLine(
            "boot_gate_1_population",
            "BLOCKED"
            if empty_population and population_reached == 0
            else ("OBSERVED" if population_reached else "UNEXERCISED"),
            (
                ("blocked_empty_markers", str(empty_population)),
                ("reached_next_gate_markers", str(population_reached)),
            ),
            "FALSE_ZERO(gate_never_evaluated)"
            if empty_population == 0 and population_reached == 0
            else "zero_blocked_is_readable_only_when_reached_next_gate_is_nonzero",
        )
    )
    seed_reached = seed_incomplete + rest_incomplete + complete
    result.append(
        CensusLine(
            "boot_gate_2_state_seed",
            "BLOCKED" if seed_incomplete else ("OBSERVED" if seed_reached else "UNEXERCISED"),
            (
                ("blocked_seed_markers", str(seed_incomplete)),
                ("reached_gate_markers", str(seed_reached)),
            ),
            "FALSE_ZERO(gate_never_reached)"
            if seed_reached == 0
            else "zero_blocked_means_seed_completed_on_the_observed_gate_population",
        )
    )
    rest_reached = rest_incomplete + complete
    result.append(
        CensusLine(
            "boot_gate_3_rest_warmup",
            "BLOCKED" if rest_incomplete else ("OBSERVED" if rest_reached else "UNEXERCISED"),
            (
                ("blocked_rest_markers", str(rest_incomplete)),
                ("restoration_complete", str(complete)),
                ("reached_gate_markers", str(rest_reached)),
            ),
            "FALSE_ZERO(gate_never_reached)"
            if rest_reached == 0
            else "zero_blocked_is_readable_only_when_restoration_complete_is_nonzero",
        )
    )
    result.append(
        CensusLine(
            "boot_hold_release",
            "UNEXERCISED" if complete == 0 else ("OBSERVED" if releases else "FAIL"),
            (("released", str(releases)), ("restoration_complete", str(complete))),
            "FALSE_ZERO(restoration_complete_was_never_observed)"
            if complete == 0
            else "zero_released_after_completion_means_the_explicit_release_line_is_missing",
        )
    )

    if db is None:
        result.append(
            CensusLine(
                "db3_fanout_only_excluded",
                "COULD_NOT_TELL",
                (("excluded", "unknown"), ("probe_symbols", "unknown")),
                "expected_zero_but_the_observational_population_was_unreadable",
            )
        )
    else:
        excluded = db["db3_fanout_only_excluded"]
        probe_symbols = db["db3_probe_symbols"]
        result.append(
            CensusLine(
                "db3_fanout_only_excluded",
                "EXPECTED_ZERO" if excluded == 0 else "OBSERVED",
                (("excluded", str(excluded)), ("probe_symbols", str(probe_symbols))),
                "EXPECTED(the_population_is_rare_by_design;correctness_is_mutation_proven)"
                if excluded == 0
                else "nonzero_is_an_exercise_count_not_a_correctness_grade",
            )
        )

    if evidence_errors:
        result.append(
            CensusLine(
                "evidence_sources",
                "COULD_NOT_TELL",
                (("errors", str(len(evidence_errors))),),
                "one_or_more_read_only_sources_did_not_answer",
            )
        )
    return result


def run_report(
    *,
    since: datetime,
    until: datetime,
    log_reader: Callable[[str], list[str]] = read_service_logs,
    db_reader: Callable[[datetime, datetime], dict[str, int]] = query_database_counts,
    identity_reader: Callable[
        [datetime, datetime, Sequence[str]], tuple[str, Mapping[str, int | str]]
    ] = identity_observation,
) -> CensusReport:
    if since >= until:
        return CensusReport(
            REFUSED,
            "REFUSED",
            ("REFUSED: window start is not before window end",),
        )

    errors: list[str] = []
    v2_raw: list[str] = []
    oms_raw: list[str] = []
    for service, sink in ((V2_SERVICE, v2_raw), (OMS_SERVICE, oms_raw)):
        try:
            sink.extend(log_reader(service))
        except EvidenceFailure as exc:
            errors.append(f"{service}_logs={exc}")
    v2 = window_lines(v2_raw, since=since, until=until)
    oms = window_lines(oms_raw, since=since, until=until)

    db: dict[str, int] | None
    try:
        db = db_reader(since, until)
    except EvidenceFailure as exc:
        db = None
        errors.append(f"database={exc}")

    identity: tuple[str, Mapping[str, int | str]] | None
    try:
        identity = identity_reader(since, until, v2_raw)
    except EvidenceFailure as exc:
        identity = None
        errors.append(f"identity={exc}")

    metrics = build_lines(
        v2=v2,
        oms=oms,
        db=db,
        identity=identity,
        evidence_errors=errors,
    )
    verdict = "COULD_NOT_TELL" if errors else "REPORT_COMPLETE"
    output = [
        "### MARKER CENSUS",
        f"window=[{since.isoformat()}, {until.isoformat()})",
        "scope=fireable markers only; W3 omitted as structurally unexercisable",
    ]
    output.extend(metric.render() for metric in metrics)
    output.extend(f"evidence_error={error}" for error in errors)
    output.append(f"VERDICT={verdict} denominators=stated")
    return CensusReport(
        COULD_NOT_TELL if errors else REPORT_COMPLETE,
        verdict,
        tuple(output),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="inclusive ISO instant with offset")
    parser.add_argument("--until", required=True, help="exclusive ISO instant with offset")
    args = parser.parse_args(argv)
    try:
        since = parse_instant(args.since, "--since")
        until = parse_instant(args.until, "--until")
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return REFUSED
    report = run_report(since=since, until=until)
    print("\n".join(report.lines))
    return report.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
