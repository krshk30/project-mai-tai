import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
import continue_oms_first as continuation


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
