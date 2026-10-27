"""Pure historical reference denominator/footer; never a trade or PnL projector.

Caller supplies its existing bounded stored read. No reader, writer, connection,
engine or provider is accepted. Counts distinguish signals, account inputs and
canonical world-result rows. No-input signals never become fake account rows.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
import hashlib
from html import escape
import json
import re


LABEL = "replay, in-sample; underlying capture completeness UNKNOWN"
ORDERED_KEYS_SHA256 = "ad69e2256eacbc9cdd108e0b90af26bb032241d8f92e016a2c624c9083272162"
WORLDS = ("FIRSTBAR", "FIRSTBAR_X3")
IDENTITY_FIELDS = ("trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id")
COMPARISONS = {"UNKNOWN", "DIFFERENT", "MATCH_COMPARED_FIELDS_ONLY"}


@dataclass(frozen=True)
class SignalEvidence:
    reference_key: tuple[str, str, str]
    in_requested_window: bool
    reconstructed_account_legs: int
    declared_result_rows: int
    firstbar_comparison: str
    x3_comparison: str
    issues: tuple[str, ...]


@dataclass(frozen=True)
class ExcludedSignal:
    reference_key: tuple[str, str, str]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class WorldEvidence:
    mode: str
    expected_result_rows_in_window: int
    observed_result_rows_in_page: int | None
    unknown_signal_evidence_in_window: int
    different_signals_in_window: int
    matched_reference_fields_only_in_window: int
    page_membership_status: str


@dataclass(frozen=True)
class StudyCaseIndexSummary:
    status: str
    source_id: str
    requested_start: date
    requested_end: date
    selected_seed_signals: int | None
    requested_window_signals: int | None
    seed_signals_without_inputs: int | None
    window_signals_without_inputs: int | None
    reconstructed_seed_account_legs: int | None
    reconstructed_window_account_legs: int | None
    worlds: tuple[WorldEvidence, ...]
    cases: tuple[SignalEvidence, ...]
    exclusions: tuple[ExcludedSignal, ...]
    issues: tuple[str, ...]
    population_label: str = LABEL
    economic_qualification: str = "UNQUALIFIED_REPLAY"
    underlying_capture_completeness: str = "UNKNOWN"
    global_pnl_qualified: bool = False


def _require(condition):
    if not condition:
        raise ValueError("historical_case_index_contract_required")


def _sequence(value, maximum):
    _require(isinstance(value, (list, tuple)) and len(value) <= maximum)
    return value


def _text(value):
    _require(isinstance(value, str) and 0 < len(value) <= 512)
    return value


def _key(value):
    _require(isinstance(value, (list, tuple)) and len(value) == 3)
    key = tuple(_text(item) for item in value)
    date.fromisoformat(key[1])
    return key


def _identity(value):
    _require(isinstance(value, Mapping) and set(value) == set(IDENTITY_FIELDS))
    return tuple(_text(value[field]) for field in IDENTITY_FIELDS)


def _unknown(source_id, start, end, issue):
    return StudyCaseIndexSummary("UNKNOWN", source_id, start, end,
        None, None, None, None, None, None, (), (), (), (issue,))


def _index(run, source_id, start, end):
    from .firstbar_binding import FIRSTBAR_RESULT_VERSION
    from .firstbar_ui_reader import seed_run_is_bound

    _require(isinstance(run, Mapping) and run.get("source_id") == source_id and seed_run_is_bound(run))
    _require(run["coverage"].get("engine_contract_version") == FIRSTBAR_RESULT_VERSION
        and run["coverage"].get("population_label") == LABEL
        and run["coverage"].get("underlying_capture_completeness") == "UNKNOWN")
    study = run["coverage"].get("historical_study")
    _require(isinstance(study, Mapping) and study.get("version") == "ATRSIM_HISTORICAL_PUBLICATION_V1"
        and study.get("global_pnl_qualified") is False)
    checksum = study.get("receipt_sha256")
    _require(isinstance(checksum, str) and re.fullmatch(r"[0-9a-f]{64}", checksum) is not None
        and source_id == "firstbar-study:" + checksum)
    selected = _sequence(study.get("selected_cases"), 173)
    _require(len(selected) == 173)
    keys = [_key(item["reference_key"]) for item in selected]
    _require(len(set(keys)) == 173 and hashlib.sha256(json.dumps(keys, separators=(",", ":"),
        ensure_ascii=True).encode()).hexdigest() == ORDERED_KEYS_SHA256)
    cases, identities, expected = [], set(), {world: set() for world in WORLDS}
    for item, key in zip(selected, keys):
        _require(isinstance(item, Mapping))
        legs = tuple(_identity(identity) for identity in _sequence(item.get("identities"), 5000))
        _require(len(set(legs)) == len(legs) and not set(legs) & identities)
        _require(all(identity[0] == key[1] and identity[1] == key[0] for identity in legs))
        identities.update(legs)
        count = item.get("canonical_rows")
        _require(type(count) is int and count == 2 * len(legs))
        issues = tuple(_text(issue) for issue in _sequence(item.get("reconstruction_issues"), 64))
        display_issues = list(issues)
        comparisons = _sequence(item.get("comparisons"), 2)
        by_mode = {}
        if legs:
            _require(item.get("status") == "CANONICAL_ROWS_PRESENT_NOT_ACCEPTANCE" and not issues)
            for comparison in comparisons:
                _require(isinstance(comparison, Mapping) and _key(comparison.get("reference_key")) == key)
                mode, status = comparison.get("mode"), comparison.get("status")
                _require(isinstance(mode, str) and mode in WORLDS and mode not in by_mode
                    and isinstance(status, str) and status in COMPARISONS)
                bound = tuple(_identity(identity) for identity in _sequence(comparison.get("bindings"), 5000))
                _require(len(bound) == len(legs) and set(bound) == set(legs))
                display_issues.extend(mode + ": " + _text(issue)
                    for issue in _sequence(comparison.get("issues"), 64))
                by_mode[mode] = status
            _require(set(by_mode) == set(WORLDS))
        else:
            _require(item.get("status") == "UNKNOWN_NO_CANONICAL_INPUTS" and bool(issues) and not comparisons)
            by_mode = {world: "UNKNOWN_NO_CANONICAL_INPUTS" for world in WORLDS}
        in_window = start <= date.fromisoformat(key[1]) <= end
        if in_window:
            for world in WORLDS:
                expected[world].update(legs)
        cases.append(SignalEvidence(key, in_window, len(legs), count,
            by_mode["FIRSTBAR"], by_mode["FIRSTBAR_X3"], tuple(dict.fromkeys(display_issues))))
    _require(2 * len(identities) <= 10000)
    exclusions, excluded_keys = [], set()
    for item in _sequence(study.get("reference_exclusions"), 7):
        _require(isinstance(item, Mapping))
        key = _key(item.get("reference_key"))
        reasons = tuple(_text(reason) for reason in _sequence(item.get("reasons"), 16))
        _require(key not in keys and key not in excluded_keys and bool(reasons))
        excluded_keys.add(key)
        exclusions.append(ExcludedSignal(key, reasons))
    _require(len(exclusions) == 7)
    return tuple(cases), tuple(exclusions), expected


def _page(stored, expected, start, end):
    from .firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION

    _require(type(stored.get("truncated")) is bool)
    _require(stored.get("read_status") in ("AVAILABLE", "NO_ROWS_UNKNOWN"))
    observed = {world: set() for world in WORLDS}
    for row in _sequence(stored.get("rows"), 10000):
        _require(isinstance(row, Mapping))
        world = row.get("mode")
        _require(isinstance(world, str) and world in WORLDS)
        identity = _identity(row.get("identity"))
        _require(start <= date.fromisoformat(identity[0]) <= end
            and identity in expected[world] and identity not in observed[world]
            and row.get("version") == FIRSTBAR_RESULT_VERSION
            and row.get("engine_dependency_head") == FIRSTBAR_ENGINE_HEAD
            and row.get("population_label") == LABEL)
        observed[world].add(identity)
    return observed


def historical_study_case_index(stored, *, source_id, requested_start, requested_end):
    """Detached typed summary from ONE existing read, not a whole-PnL witness."""
    if (not isinstance(source_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", source_id) is None
            or type(requested_start) is not date or type(requested_end) is not date
            or not date(2026, 9, 22) <= requested_start <= requested_end <= date(2026, 10, 23)
            or not 0 <= (requested_end - requested_start).days <= 31):
        raise ValueError("bounded_historical_summary_source_and_window_required")
    if not isinstance(stored, Mapping):
        return _unknown(source_id, requested_start, requested_end, "STUDY_CASE_INDEX_UNAVAILABLE")
    try:
        cases, exclusions, expected = _index(stored.get("run"), source_id, requested_start, requested_end)
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        return _unknown(source_id, requested_start, requested_end, "STUDY_CASE_INDEX_UNBOUND_OR_MALFORMED")
    issues = []
    try:
        observed = _page(stored, expected, requested_start, requested_end)
        truncated = stored["truncated"]
        if truncated:
            issues.append("CANONICAL_RESULT_PAGE_TRUNCATED")
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError):
        observed, truncated = None, True
        issues.append("CANONICAL_RESULT_PAGE_UNKNOWN_INDEX_PRESERVED")
    window = tuple(case for case in cases if case.in_requested_window)
    worlds = []
    for world in WORLDS:
        field = "firstbar_comparison" if world == "FIRSTBAR" else "x3_comparison"
        statuses = Counter(getattr(case, field) for case in window)
        page_status = ("UNKNOWN" if observed is None else "TRUNCATED_UNKNOWN" if truncated
            else "EXACT_DECLARED_RESULT_ROWS_ONLY" if observed[world] == expected[world]
            else "MISSING_DECLARED_RESULT_ROWS_UNKNOWN")
        worlds.append(WorldEvidence(world, len(expected[world]),
            None if observed is None else len(observed[world]),
            statuses["UNKNOWN"] + statuses["UNKNOWN_NO_CANONICAL_INPUTS"],
            statuses["DIFFERENT"], statuses["MATCH_COMPARED_FIELDS_ONLY"], page_status))
    return StudyCaseIndexSummary("BOUND_REFERENCE_INDEX", source_id, requested_start, requested_end,
        len(cases), len(window), sum(not case.reconstructed_account_legs for case in cases),
        sum(not case.reconstructed_account_legs for case in window),
        sum(case.reconstructed_account_legs for case in cases),
        sum(case.reconstructed_account_legs for case in window), tuple(worlds), cases, exclusions, tuple(issues))


def render_historical_study_case_index(summary):
    """Reference evidence footer, not extra rows in either LIVE-format trade table."""
    if not isinstance(summary, StudyCaseIndexSummary):
        raise ValueError("typed_historical_case_index_summary_required")
    def text(value):
        return escape(str(value), quote=True)
    parts = ['<section data-firstbar-reference-index="true"><h3>Reference signal evidence - not trade rows</h3>',
        '<p>' + text(summary.population_label) + '</p>',
        '<p>Source ' + text(summary.source_id) + '; ' + text(summary.status) +
        '; window ' + text(summary.requested_start) + ' through ' + text(summary.requested_end) +
        '. Economics UNQUALIFIED_REPLAY; underlying capture UNKNOWN. No global P&amp;L qualification.</p>']
    if summary.selected_seed_signals is None:
        return ''.join([*parts, '<p>Signal denominator UNKNOWN; case index unavailable or unbound.</p></section>'])
    parts.append('<p>Selected seed signals: ' + text(summary.selected_seed_signals)
        + '; requested-window signals: ' + text(summary.requested_window_signals)
        + '; seed signals without canonical inputs: ' + text(summary.seed_signals_without_inputs)
        + '; reconstructed seed account legs: ' + text(summary.reconstructed_seed_account_legs)
        + '. Signals, account legs and canonical world-result rows are different counts. '
        'A signal without inputs is UNKNOWN, not an affirmed no-buy or zero P&amp;L.</p>')
    for world in summary.worlds:
        parts.append('<p>' + text(world.mode) + ': requested-window canonical result rows '
            + text(world.observed_result_rows_in_page if world.observed_result_rows_in_page is not None else "UNKNOWN")
            + ' observed / ' + text(world.expected_result_rows_in_window) + ' declared; '
            + text(world.page_membership_status) + '; comparison UNKNOWN signals '
            + text(world.unknown_signal_evidence_in_window) + '; DIFFERENT '
            + text(world.different_signals_in_window) + '; matched reference fields only '
            + text(world.matched_reference_fields_only_in_window) + '.</p>')
    parts.append('<details><summary>All selected reference signals (not SIM/native entry clocks)</summary>'
        '<table><thead><tr><th>Reference symbol/day/fill-clock key</th><th>In window</th>'
        '<th>Account input legs</th><th>Canonical result rows, both worlds</th>'
        '<th>FIRSTBAR comparison</th><th>FIRSTBAR_X3 comparison</th><th>Input/comparison issues</th></tr></thead><tbody>')
    for case in summary.cases:
        values = (" / ".join(case.reference_key), "yes" if case.in_requested_window else "no",
            case.reconstructed_account_legs, case.declared_result_rows,
            case.firstbar_comparison, case.x3_comparison, "; ".join(case.issues) or "none in index")
        parts.append('<tr>' + ''.join('<td>' + text(value) + '</td>' for value in values) + '</tr>')
    parts.append('</tbody></table></details><details><summary>Reference exclusions: '
        + text(len(summary.exclusions)) + ' (not affirmative strategy skips)</summary><ul>')
    for exclusion in summary.exclusions:
        parts.append('<li>' + text(" / ".join(exclusion.reference_key)) + ': '
            + text("; ".join(exclusion.reasons)) + '</li>')
    parts.append('</ul></details>')
    for issue in summary.issues:
        parts.append('<p>Read evidence: ' + text(issue) + '</p>')
    return ''.join([*parts, '</section>'])
