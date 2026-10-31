"""Pure two-source supplied-state comparison, NOT a historical callback replay.

No files, clock, service, provider or parser. Caller binds actual source classes,
supplies explicit state/control events and captures every required economic
field and persistence write. Incomplete historical input cannot become PASS.
"""

from copy import deepcopy


def compare_supplied_state(events, *, old, proposed, apply_event, capture):
    if not isinstance(events, (list, tuple)) or not events:
        raise ValueError("nonempty_finite_supplied_events_required")
    if type(old) is type(proposed):
        raise ValueError("literal_distinct_source_classes_required_not_feature_OFF")
    rows = []
    for index, event in enumerate(events):
        if (not isinstance(event, dict)
                or event.get("origin") not in {
                    "CONTROLLED_STATE", "RETAINED_STATE_ONLY_INCOMPLETE_CALLBACK"}
                or not isinstance(event.get("source_ref"), str)
                or not event["source_ref"]):
            raise ValueError("explicit_supplied_state_origin_and_reference_required")
        old_result = deepcopy(apply_event(old, deepcopy(event)))
        proposed_result = deepcopy(apply_event(proposed, deepcopy(event)))
        old_state = deepcopy(capture(old))
        proposed_state = deepcopy(capture(proposed))
        rows.append(dict(index=index, origin=event["origin"],
                         source_ref=event["source_ref"], old_result=old_result,
                         proposed_result=proposed_result,
                         supplied_state_equal=(old_result == proposed_result
                                               and old_state == proposed_state)))
    return dict(scope="SUPPLIED_STATE_ONLY_NOT_HISTORICAL_CALLBACK_REPLAY",
                historical_replay_qualified=False, native_proof=False, rows=rows,
                supplied_state_equal=all(row["supplied_state_equal"] for row in rows))
