import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import continue_oms_first as continuation
import pytest
from datetime import datetime, timezone
from post_proof import overnight_hold
from release_policy import Unknown
from release_policy import Stop
from restart_report import render_held

RECORDED_RESTORE = '2026-10-07 01:55:57,523 WARNING project_mai_tai.services.schwab_1m_v2_bot | [V2-BOOT-RESTORE] restoration_complete=0 evaluated=7 confirmed=7 rest_warmed=0 timeout_released=0 warmup_pending=7 warmup_pending_symbols=APUS,BIYA,IPDN,MOBX,MTEN,OLOX,SMXT reason=rest_warmup_incomplete; waiting for a fresh REST or streamer bar within 300s'
RECORDED_HOLD = '2026-10-07 01:55:54,253 WARNING project_mai_tai.services.schwab_1m_v2_bot | [V2-BOOT-HOLD] HELD restoration_complete=0'
RECORDED_STATE = {'ExecMainStartTimestamp': 'Wed 2026-10-07 01:53:49 UTC'}
RECORDED_NOW = datetime(2026, 10, 7, 1, 56, tzinfo=timezone.utc)

OFFICIAL_HELD = '''Overall: REAL FAILURE (2 failed checks / 0 unknown checks / 9 reported checks)
Final call: REAL FAILURE; no post-restart restoration_complete=1 line
| Check | Measured evidence | Result |
| Restarted services | new active/running | PASS |
| REST warmup | complete markers=0 | FAIL |
| BOOT-HOLD released | release markers=0 | FAIL |
| Bar continuity | restart outside bar session | N/A_OFF_SESSION |
Failures:
- no post-restart restoration_complete=1 line
- no literal post-restart BOOT-HOLD release with restoration_complete=1
'''


def test_official_held_pair_never_rendered_as_pass():
    proof = dict(lines=[RECORDED_HOLD, RECORDED_RESTORE], state=RECORDED_STATE)
    text = render_held(OFFICIAL_HELD, proof, RECORDED_NOW)
    assert 'Final call: ACCEPTED_HELD_OFFSESSION;' in text
    assert 'HELD_EXPECTED_OFFSESSION' in text and 'restoration/release UNMEASURED' in text
    assert '| REST warmup | complete markers=0 | PASS |' not in text


@pytest.mark.parametrize('extra', ['\n- unrelated real failure', '\nUnknowns:\n- unrelated unknown'])
def test_official_other_failure_or_unknown_is_not_admitted(extra):
    with pytest.raises(Stop):
        render_held(OFFICIAL_HELD + extra, dict(lines=[RECORDED_HOLD, RECORDED_RESTORE], state=RECORDED_STATE), RECORDED_NOW)


def test_recorded_overnight_hold_is_literal_held_not_restoration_pass():
    result = overnight_hold([RECORDED_HOLD, RECORDED_RESTORE], RECORDED_STATE, RECORDED_NOW)
    assert result['population'] == 7 and result['entry_allowed'] is False
    assert result['verdict'] == 'HELD_EXPECTED_NO_SCHEDULED_BARS'
    assert result['restoration'].startswith('UNMEASURED')


@pytest.mark.parametrize('old,new', [
    ('restoration_complete=0', 'restoration_complete=1'),
    ('confirmed=7', 'confirmed=6'), ('rest_warmed=0', 'rest_warmed=1'),
    ('timeout_released=0', 'timeout_released=1'), ('warmup_pending=7', 'warmup_pending=6'),
    ('APUS,BIYA', 'APUS,APUS'), ('reason=rest_warmup_incomplete', 'reason=other'),
    ('01:55:57,523', '01:50:57,523')])
def test_overnight_hold_refuses_each_changed_recorded_shape(old, new):
    with pytest.raises(Unknown):
        overnight_hold([RECORDED_HOLD, RECORDED_RESTORE.replace(old, new)], RECORDED_STATE, RECORDED_NOW)


def test_daytime_start_cannot_use_overnight_observation():
    with pytest.raises(Unknown):
        overnight_hold([RECORDED_HOLD, RECORDED_RESTORE],
                       {'ExecMainStartTimestamp': 'Tue 2026-10-06 23:53:49 UTC'}, RECORDED_NOW)


def test_phase_seven_closeout_does_not_repeat_any_service_action():
    fx = MagicMock()
    fx.started = {}
    fx.log_base = {name: {} for name in ('oms', 'schwab-1m-v2', 'strategy')}
    with patch('post_proof.collect', return_value={}):
        continuation.resume(fx, 7)
    fx.action.assert_not_called()
    fx.closeout.assert_called_once()


def test_oms_starts_before_first_freshness_read_and_sequence_is_scoped():
    fx = MagicMock()
    fx.started = {}
    fx.log_base = {name: {} for name in ('oms', 'schwab-1m-v2', 'strategy')}
    with patch('post_proof.collect', return_value={}):
        continuation.resume(fx)
    calls = fx.mock_calls
    assert calls[0].args == ('start', 'oms')
    assert [c.args for c in calls if c[0] == 'action'] == [
        ('start', 'oms'), ('start', 'schwab-1m-v2'), ('start', 'strategy'), ('restart', 'control')]
    assert calls.index(next(c for c in calls if c[0] == 'flat')) > calls.index(next(c for c in calls if c[0] == 'checkpoint'))
    fx.closeout.assert_called_once()
    fx.complete.assert_called_once()


def test_failed_start_invokes_authorized_fallback_without_later_sequence():
    fx = MagicMock()
    fx.action.side_effect = RuntimeError('start failed')
    try:
        continuation.resume(fx)
    except RuntimeError:
        pass
    else:
        raise AssertionError('failure must be reported')
    fx.recover_start_failure.assert_called_once_with('oms')
    fx.closeout.assert_not_called()
