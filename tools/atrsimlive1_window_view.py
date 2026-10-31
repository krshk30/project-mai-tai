"""FILE equality view over Main's immutable full-input artifact; no I/O.

This is not a new production SQL certificate. The common source reference is
the actual retained full input in both comparisons; selected counts/hashes are
reported separately and never presented as a second full-day read.
"""

from bisect import bisect_left
import re

from atrsimlive1_collect import digest
from atrsimlive1_data_coverage import derive
from atrsimlive1_window_census import full_census, condition_groups
from atrsimlive1_working_windows import plan, terminal, selection, nanos


SCHEMA = "ATRSIMLIVE1_RETAINED_FULL_INPUT_WINDOW_VIEW_V1"


def envelope_sha256(data):
    return digest({key: data[key] for key in ('trading_day', 'source_asof', 'snapshot_id',
        'start', 'end', 'symbols', 'source_hashes', 'source_counts', 'query_sha256')})


def leg_slice(descriptors, origin, trades, trade_clocks, quotes, quote_clocks, *, left, right):
    """One episode's working membership; physical rows/keys are never copied.

    Coverage remains the original separately proved broad envelope. This only
    avoids scanning another inactive episode's tape. Overlapping slices share
    the SAME immutable trade object/raw key in the engine's cohort ledger.
    """
    matched = [item for item in descriptors if item['origin'] == origin
               or item['origin'].startswith(origin + ':original_ns=')]
    if len(matched) != 1:
        raise ValueError('unique_original_working_anchor_required')
    item, = matched
    lo = max(left, item['start_ns'])
    hi = min(right, item['end_exclusive_ns'] if item['end_inclusive_timestamp_ns'] is None
             else item['end_inclusive_timestamp_ns'] + 1)
    selected = trades[bisect_left(trade_clocks, lo):bisect_left(trade_clocks, hi)]
    quote_start = bisect_left(quote_clocks, lo)
    selected_quotes = quotes[max(0, quote_start - 1):bisect_left(quote_clocks, hi)]
    return selected, selected_quotes


def make_view(data, *, policy, source, artifact, artifact_sha256, null_policy=None):
    from atrsimlive1_null_policy import require_null_policy

    require_null_policy(null_policy)
    if (len(data["symbols"]) != 1 or not artifact.startswith("/")
            or re.fullmatch(r"[0-9a-f]{64}", artifact_sha256) is None):
        raise ValueError("literal_single_symbol_full_artifact_required")
    symbol, = data["symbols"]
    rows = data["rows"]
    census = full_census(rows["trades"])
    if any(item["invalid_price_count"] for item in census):
        raise ValueError("positive_source_number_required")
    coverage_windows, coverage_report = derive(symbol=symbol, day=data["trading_day"],
        bars=rows["bars"], trades=rows["trades"], read_proof=data["read_proof"],
        log=data["capture_log"], asof_ns=nanos(data["source_asof"]), nanos=nanos)
    if "UNKNOWN_SAME_SNAPSHOT_FULL_READ_CERTIFICATE" in coverage_report["issues"]:
        raise ValueError("original_complete_input_binding_required")
    anchors = []
    for row in rows["fills"]:
        if row["side"] == "buy":
            anchors.append(dict(symbol=symbol, at=row["filled_at"], source="fill:" + row["fill_id"]))
            if row["submitted_at"] is not None:
                anchors.append(dict(symbol=symbol, at=row["submitted_at"], source="order:" + row["order_id"]))
    planned = plan(data["symbols"], rows["bars"], start=data["start"], end=data["end"], placements=anchors)
    ordered = sorted(rows["trades"], key=lambda row: (nanos(row["event_ts"]), row["id"]))
    clocks = [nanos(row["event_ts"]) for row in ordered]
    resolved = []
    for window in planned["windows"]:
        probe = ordered[bisect_left(clocks, window.boundary_ns):] if window.terminal_pending else ()
        resolved.append(terminal(window, probe, policy=policy, source=source, complete=True,
                                 null_policy=null_policy))
    descriptors = selection(resolved)
    included = {}
    for item in descriptors:
        lo = bisect_left(clocks, item["start_ns"])
        if item["end_inclusive_timestamp_ns"] is None:
            hi = bisect_left(clocks, item["end_exclusive_ns"])
        else:
            hi = bisect_left(clocks, item["end_inclusive_timestamp_ns"] + 1)
        for row in ordered[lo:hi]:
            included[row["id"]] = row
    trades = sorted(included.values(), key=lambda row: (nanos(row["event_ts"]), row["id"]))
    quote_rows = sorted(rows["quotes"], key=lambda row: (nanos(row["event_ts"]), row["id"]))
    quote_clocks = [nanos(row["event_ts"]) for row in quote_rows]
    quote_ids = {}
    for item in descriptors:
        lo = bisect_left(quote_clocks, item["start_ns"])
        if lo:
            quote_ids[quote_rows[lo - 1]["id"]] = quote_rows[lo - 1]  # Latest, never latest-valid.
        hi = bisect_left(quote_clocks, (item["end_inclusive_timestamp_ns"] + 1)
                         if item["end_inclusive_timestamp_ns"] is not None else item["end_exclusive_ns"])
        for row in quote_rows[lo:hi]:
            quote_ids[row["id"]] = row
    quotes = sorted(quote_ids.values(), key=lambda row: (nanos(row["event_ts"]), row["id"]))
    view = dict(data)
    view["rows"] = dict(rows, trades=trades, quotes=quotes)
    # These still identify the actually retained full FILE in both comparisons.
    # They are not claims that production SQL fetched these omitted raw rows.
    metadata = dict(census=census, conditions=condition_groups(rows["trades"]), coverage_report=coverage_report,
        coverage_windows=[(left, right, (), note, metrics)
                          for left, right, _, note, metrics in coverage_windows])
    if null_policy is not None:
        metadata['null_policy'] = null_policy
    view["window_view"] = dict(schema=SCHEMA, purpose="FILE_EQUALITY_ONLY_NOT_SQL_FULL_READ",
        artifact=artifact, artifact_sha256=artifact_sha256, snapshot_id=data["snapshot_id"],
        parent_source_hashes=dict(data["source_hashes"]), envelope_sha256=envelope_sha256(data),
        selected_hashes=dict(trades=digest(trades), quotes=digest(quotes)),
        selected_counts=dict(trades=len(trades), quotes=len(quotes)), descriptors=descriptors,
        metadata=metadata, metadata_sha256=digest(metadata))
    if null_policy is not None:
        view['window_view']['null_policy'] = null_policy
    return view


def validate_view(data):
    from atrsimlive1_null_policy import require_null_policy

    value = data["window_view"]
    require_null_policy(value.get('null_policy'))
    if value.get('null_policy') != value['metadata'].get('null_policy'):
        raise ValueError('bound_window_null_policy_required')
    file_view = value.get('schema') == SCHEMA and value.get('purpose') == 'FILE_EQUALITY_ONLY_NOT_SQL_FULL_READ'
    sql_view = (value.get('schema') == 'ATRSIMLIVE1_SQL_WINDOW_VIEW_V1'
                and value.get('purpose') == 'SAME_RR_SQL_WINDOWS_PLUS_FULL_CENSUS')
    if (not (file_view or sql_view)
            or value.get("snapshot_id") != data["snapshot_id"]
            or value.get("parent_source_hashes") != data["source_hashes"]
            or value.get('envelope_sha256') != envelope_sha256(data)):
        raise ValueError("bound_full_artifact_window_view_required")
    if file_view and (not value.get('artifact', '').startswith('/')
            or re.fullmatch(r'[0-9a-f]{64}', value.get('artifact_sha256', '')) is None):
        raise ValueError('literal_full_artifact_binding_required')
    if sql_view:
        proof = data['read_proof']
        if (proof.get('schema') != 'ATRSIMLIVE1_WINDOW_CENSUS_RR_V1'
                or proof.get('snapshot_id') != data['snapshot_id'] or proof.get('full_read') is not False
                or proof.get('truncated') is not False
                or proof.get('source_asof') != data['source_asof']
                or proof.get('start') != data['start'] or proof.get('end') != data['end']
                or proof.get('census_sha256') != digest(value['metadata']['census'])
                or proof.get('bars_sha256') != digest(data['rows']['bars'])
                or proof.get('selected_hashes') != value['selected_hashes']):
            raise ValueError('same_RR_window_census_binding_required')
        if proof.get('full_counts_sha256') != digest(value['full_counts']):
            raise ValueError('full_source_population_changed')
        if proof.get('stored_null_policy') != value.get('null_policy'):
            raise ValueError('bound_SQL_null_policy_required')
    for kind in ("trades", "quotes"):
        if (value["selected_counts"][kind] != len(data["rows"][kind])
                or value["selected_hashes"][kind] != digest(data["rows"][kind])):
            raise ValueError("window_selection_changed")
    if digest(value["metadata"]) != value["metadata_sha256"]:
        raise ValueError("window_universe_metadata_changed")
    expected = data['source_counts']['trades'] if file_view else value['full_counts']['trades']
    if sum(row["row_count"] for row in value["metadata"]["census"]) != expected:
        raise ValueError("full_census_population_changed")
    for kind in ('trades', 'quotes'):
        expected_count = data['source_counts'][kind] if sql_view else value['selected_counts'][kind]
        if expected_count != len(data['rows'][kind]):
            raise ValueError('selected_source_population_changed')
        if sql_view and expected_count > value['full_counts'][kind]:
            raise ValueError('window_selected_population_exceeds_census')
    return value


def compare_existing_receipts(old, new):
    """Pure FILE differential. No OLD model call; no clock/math normalization.

    A declared tie conversion is disclosed separately, never equality. Any
    changed previously known row blocks, including normalized known-percent.
    This comparison cannot by itself establish two known SIM examples.
    """
    if (old['trading_day'] != new['trading_day'] or old['source_asof'] != new['source_asof']
            or old['producer']['snapshot_id'] != new['producer']['snapshot_id']
            or old['producer']['observed_symbols'] != new['producer']['observed_symbols']
            or old['producer']['input_refs'] != new['producer']['input_refs']):
        raise ValueError('same_original_FILE_population_required')
    def index(document):
        result = {}
        for world, table in document['tables'].items():
            for value in table['rows']:
                key = world, value['outcome_id']
                if key in result:
                    raise ValueError('duplicate_window_equality_identity')
                result[key] = value
        return result
    before, after = index(old), index(new)
    if set(before) != set(after):
        raise ValueError('original_window_equality_population_changed')
    conversions = new['producer'].get('same_sip_conversions', [])
    declared = {(value['world'], value['outcome_id']) for value in conversions}
    if len(declared) != len(conversions) or not declared <= set(after):
        raise ValueError('tie_conversion_identity_changed')
    records = []
    old_receipts = old['producer']['engine_receipts']
    new_receipts = new['producer']['engine_receipts']
    if set(old_receipts) != set(new_receipts):
        raise ValueError('engine_receipt_population_changed')
    for (world, identity), previous in before.items():
        current = after[world, identity]
        old_receipt, new_receipt = old_receipts.get(identity), new_receipts.get(identity)
        known = previous['row'].get('pnl_label_known') is True or previous['row'].get('pnl_pct_label_known') is True
        exact = previous == current and old_receipt == new_receipt
        if exact:
            status = 'EXACT'
        elif ((world, identity) in declared and not known
              and 'UNKNOWN_SAME_SIP_NATIVE_SEQUENCE' in str(old_receipt)):
            status = 'EXPLICIT_TIE_POLICY_DIFFERENCE_NOT_EQUALITY'
        else:
            status = 'UNEXPLAINED_CHANGE'
        records.append(dict(world=world, outcome_id=identity, status=status, previously_known=known,
            old_row_sha256=digest(previous), new_row_sha256=digest(current),
            old_receipt_sha256=digest(old_receipt), new_receipt_sha256=digest(new_receipt)))
    return dict(status='BLOCKED' if any(r['status'] == 'UNEXPLAINED_CHANGE' for r in records)
                else 'REFERENCE_DIFFERENTIAL_RECORDED_NOT_QUALIFICATION', records=records,
        exact=sum(r['status'] == 'EXACT' for r in records),
        explicit_tie_differences=sum(r['status'] == 'EXPLICIT_TIE_POLICY_DIFFERENCE_NOT_EQUALITY' for r in records),
        previously_known=sum(r['previously_known'] for r in records),
        generated_clock_comparison='Actual publication clocks excluded; engine and canonical row clocks not normalized')
