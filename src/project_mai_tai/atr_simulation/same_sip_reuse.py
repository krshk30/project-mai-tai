"""Whole-world cache admission seam; current RR/content proof belongs to caller.

The reviewed collector/cache wrapper derives both binding populations from
identity/order/cohort, window content, policy and source/code seals. A cache
does not verify SQL or grant native authority here. Original fact provenance
stays in retained results; the caller links current verification separately.
"""
from copy import deepcopy
import re

from .same_sip import enabled, report_key, SAME_SIP_CONSERVATIVE_V1, LABEL
from .types import EpisodeResult, ScenarioResult, EntryMode, FillModel, CONTRACT_VERSION

CORE_WORLDS = {
    'TODAY:SUPPLIED': ('today', EntryMode.TODAY, None),
    'VOLBODY:QUEUE_AWARE': ('volbody_queue_aware', EntryMode.VOLBODY, FillModel.QUEUE_AWARE),
    'VOLBODY:OPTIMISTIC': ('volbody_optimistic', EntryMode.VOLBODY, FillModel.OPTIMISTIC),
}


def _admit(episodes, retained, reports, old_bindings, current_bindings, allowed):
    if retained is None:
        if any(value is not None for value in (reports, old_bindings, current_bindings)):
            raise ValueError('retained_worlds_required_for_cache_arguments')
        return {}, {}
    if (type(retained) is not dict or not set(retained) <= set(allowed)
            or type(old_bindings) is not dict or type(current_bindings) is not dict
            or set(old_bindings) != set(retained) or set(current_bindings) != set(retained)
            or type(reports) is not dict):
        raise ValueError('whole_world_cache_and_exact_binding_population_required')
    identities = tuple(episode.identity for episode in episodes)
    admitted_reports = {}
    for world, results in retained.items():
        old, current = old_bindings[world], current_bindings[world]
        if (type(old) is not tuple or type(current) is not tuple or len(old) != len(episodes)
                or len(current) != len(episodes) or old != current
                or any(type(value) is not str or re.fullmatch('[0-9a-f]{64}', value) is None for value in old)):
            raise ValueError('same_current_verified_whole_cohort_input_bindings_required')
        if (type(results) is not tuple or len(results) != len(episodes)
                or tuple(getattr(result, 'identity', None) for result in results) != identities):
            raise ValueError('retained_whole_cohort_identity_and_order_required')
        for episode in episodes:
            if enabled(episode.same_sip_policy):
                key = report_key(episode.identity, world)
                report = reports.get(key)
                if (type(report) is not dict or report.get('policy') != SAME_SIP_CONSERVATIVE_V1
                        or report.get('label') != LABEL
                        or any(type(report.get(name)) is not int or report[name] < 0 for name in
                            ('same_timestamp_groups', 'entry_groups_blocked', 'exit_stop_wins', 'limit_pricing_fills'))
                        or type(report.get('events')) is not list or len(report['events']) > 32
                        or type(report.get('events_truncated')) is not bool):
                    raise ValueError('retained_world_tie_report_required_not_zero')
                admitted_reports[key] = deepcopy(report)
    return dict(retained), admitted_reports


def core_retained(episodes, retained, reports, old_bindings, current_bindings):
    worlds, copied = _admit(episodes, retained, reports, old_bindings, current_bindings, CORE_WORLDS)
    for world, results in worlds.items():
        attr, mode, model = CORE_WORLDS[world]
        for episode, result in zip(episodes, results):
            if (not isinstance(result, EpisodeResult) or result.execution_policy != episode.execution_policy
                    or result.contract_version != CONTRACT_VERSION):
                raise ValueError('typed_retained_episode_and_policy_required')
            scenario = getattr(result, attr)
            if not isinstance(scenario, ScenarioResult) or scenario.mode is not mode or scenario.model is not model:
                raise ValueError('typed_retained_world_scenario_required')
    return worlds, copied


def firstbar_retained(items, retained, reports, old_bindings, current_bindings, fill_policy):
    from .same_sip_firstbar import FirstBarMode
    from .same_sip_firstbar_replay import FirstBarReplayResult, FIRSTBAR_REPLAY_VERSION

    worlds, copied = _admit(tuple(item.episode for item in items), retained, reports,
        old_bindings, current_bindings, {mode.value for mode in FirstBarMode})
    for world, results in worlds.items():
        for result in results:
            if (not isinstance(result, FirstBarReplayResult) or result.mode.value != world
                    or result.fill_policy is not fill_policy or result.version != FIRSTBAR_REPLAY_VERSION):
                raise ValueError('typed_retained_firstbar_mode_and_policy_required')
    return worlds, copied
