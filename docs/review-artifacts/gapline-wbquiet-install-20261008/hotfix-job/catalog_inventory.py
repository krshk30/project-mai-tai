"""Reviewed ORBLIVE namespace reconciliation; no flag value or numeric edits."""
import json

RETIRED = {'orb_intrabar_reclaim_enabled', 'orb_paper_atr_entry_gate_enabled',
           'orb_paper_atr_exit_enabled', 'orb_paper_four_red_delay_enabled',
           'orb_paper_lifecycle_enabled', 'orb_resting_entry_enabled', 'orb_running_high_enabled'}


def candidate(box_raw, approved_raw):
    def rows(raw):
        value = json.loads(raw)
        result = {row['name']: row for row in value['flags']}
        if len(result) != len(value['flags']):
            raise ValueError('duplicate boolean inventory row')
        return result
    box, approved = rows(box_raw), rows(approved_raw)
    if len(approved) != 132 or set(box) - set(approved) not in (RETIRED, set()) or set(approved) - set(box):
        raise ValueError('unreviewed boolean inventory delta')
    if any(type(row.get('expected')) is not bool for row in approved.values()):
        raise ValueError('unreadable current expected boolean')
    changed = sorted(name for name in approved if box[name].get('expected') is not approved[name]['expected'])
    if changed:
        raise ValueError('hotfix cannot change expected boolean values: ' + ','.join(changed))
    return approved_raw, dict(before_boolean_rows=len(box), after_boolean_rows=len(approved),
                             removed_settings_fields=sorted(set(box) - set(approved)),
                             expected_value_changes=[],
                             metadata_changed_rows=sorted(n for n in approved if approved[n] != box[n]),
                             numeric_policy='BOX numeric file unchanged, including both retry-budget=0 consumers',
                             legacy_orb_service_action='none; actual identity remains independently pinned')
