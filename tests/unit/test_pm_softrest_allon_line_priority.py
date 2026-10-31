"""SOURCE UNRUN: C1 02:33 paired live LINE-first stale-bar priority."""
from project_mai_tai import pm_softrest_quote_log as diagnostic
from project_mai_tai.settings import Settings
from tests.unit.oct30_allon_control import assert_allon, settings_for_allon
from tests.unit import test_pm_softrest_quote_log_integration as integration
from tests.unit.test_pm_softrest_quote_log_integration import (
    Logger, NOW, armed, clock as clock, observation,
)


def test_ALLON_same_cleared_bars_refuses_LINE_before_downstream_bar_reason(clock, record_property, monkeypatch):
    # Supply the full profile before the real strategy/line reader is constructed.
    monkeypatch.setattr(integration, "Settings",
                        lambda *args, **kwargs: settings_for_allon(Settings(*args, **kwargs)))
    strategy, state = armed()
    assert_allon(strategy.settings)
    assert strategy.line_buy_ready("NCPL") and strategy._line_version_reader("NCPL")
    state.bars.clear()  # EXACT same adversary as the unchanged original stale_bar control.
    assert strategy.line_buy_ready("NCPL") is False
    logger = Logger()
    observed = observation(logger)
    draft = strategy.on_stream_trade("NCPL", 1.55, NOW - 100, ask_price=1.55,
        bid_price=1.54, print_size=100, ask_age_ms=100, _pm_observation=observed)
    diagnostic.observe(observed, "finish")
    assert draft is None and state.resting_flip_ms == 0
    assert logger.rows[-1]["decision"] == "REFUSED"
    assert logger.rows[-1]["reason"] == "line_not_ready"
    record_property("PM_stale_profile", "ALLON_LINE_TRUE_REAL_READER_FIRST; ORIGINAL_LINE_OFF_EXPECTATION_UNCHANGED")
