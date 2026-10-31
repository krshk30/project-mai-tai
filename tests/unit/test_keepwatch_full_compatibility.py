"""Compatibility does not turn on KEEPWATCH or grant entry from subscriptions."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.health import expected_flags_check as flags
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.keepwatch import KeepWatchSession
from project_mai_tai.strategy_core.schwab_1m_v2 import (
    OHLCVBar, SchwabV2Config, SchwabV2Strategy, session_start_ts_ms,
)


KEY = 'strategy_schwab_1m_v2_keepwatch1_enabled'
CATALOG = Path(__file__).resolve().parents[2] / 'ops/health/expected_flags.json'


@pytest.mark.parametrize('kind', ['public_config', 'legacy_namespace', 'settings_off'])
def test_legacy_public_constructor_keeps_optional_mode_off_without_relaxing_seed(kind):
    settings = (SchwabV2Config() if kind == 'public_config' else SimpleNamespace()
                if kind == 'legacy_namespace' else Settings(_env_file=None, **{KEY: False}))
    strategy = SchwabV2Strategy(settings)
    assert strategy._keepwatch_liquidity_enabled is False
    assert strategy._symbol_states == {}
    assert not strategy._pending_intents


def test_public_config_declares_the_same_production_rollout_boolean_default():
    assert SchwabV2Config.__dataclass_fields__[KEY].default is False
    assert Settings.model_fields[KEY].default is False
    assert SchwabV2Strategy(SchwabV2Config(**{KEY: True}))._keepwatch_liquidity_enabled is True


@pytest.mark.parametrize('enabled', [False, True])
def test_explicit_boolean_preserves_distinct_original_and_first_accepted_volume_contract(enabled):
    strategy = SchwabV2Strategy(Settings(_env_file=None, **{KEY: enabled}))
    state = strategy.watchlist_state('TEST')
    first = OHLCVBar(timestamp_ms=60000, open=2, high=2, low=2, close=2, volume=4000)
    revised = OHLCVBar(timestamp_ms=60000, open=2, high=2, low=2, close=2, volume=16000)
    following = OHLCVBar(timestamp_ms=120000, open=2, high=2, low=2, close=2, volume=17000)
    assert strategy._keepwatch_liquidity_enabled is enabled
    assert strategy._bar_volume_for_liquidity_floor(state, first) == 4000
    assert strategy._bar_volume_for_liquidity_floor(state, revised) == (4000 if enabled else 16000)
    assert strategy._bar_volume_for_liquidity_floor(state, following) == 17000
    assert not strategy._pending_intents


@pytest.mark.parametrize('value', [None, 0, 1, 'false', 'true'])
def test_explicit_non_boolean_legacy_value_refuses_instead_of_enabling(value):
    with pytest.raises(ValueError, match=KEY + ' must be a boolean'):
        SchwabV2Strategy(SimpleNamespace(**{KEY: value}))


def test_catalog_registers_exact_optional_default_off_expected_on_owner():
    entries = flags.load_catalog(CATALOG)
    assert len(entries) == len(flags.boolean_fields()) == 157  # Installed diagnostics/WBEXIT catalog.
    entry = next(item for item in entries if item['name'] == KEY)
    assert entry['expected'] is True
    assert entry['owning_service'] == 'schwab-1m-v2'
    assert entry.get('also_check_services', []) == []
    assert Settings.model_fields[KEY].default is False
    assert 'not activation or installation authority' in entry['ruling']


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'unknown'])
def test_catalog_still_refuses_missing_duplicate_or_unknown_boolean(tmp_path, damage):
    document = json.loads(CATALOG.read_text())
    if damage == 'missing':
        document['flags'] = [item for item in document['flags'] if item['name'] != KEY]
    elif damage == 'duplicate':
        document['flags'].append(next(item for item in document['flags'] if item['name'] == KEY))
    else:
        document['flags'].append(dict(name='unreviewed_enabled', expected=True,
            owning_service='schwab-1m-v2', reason='adverse control', ruling='not approved'))
    path = tmp_path / 'catalog.json'
    path.write_text(json.dumps(document))
    with pytest.raises(flags.CatalogError, match='inventory mismatch|duplicate names'):
        flags.load_catalog(path)


@pytest.mark.parametrize('mode,expected_rc', [('on', 0), ('off', 1), ('absent', 1), ('invalid', 2)])
def test_real_process_audit_never_substitutes_catalog_expectation_for_actual(mode, expected_rc):
    entry = next(item for item in flags.load_catalog(CATALOG) if item['name'] == KEY)
    env = {} if mode == 'absent' else {'MAI_TAI_' + KEY.upper():
        {'on': 'true', 'off': 'false', 'invalid': 'not-a-bool'}[mode]}
    called = []

    def read(service):
        called.append(service)
        return flags.ServiceEnvironment(123, env, frozenset(), None)

    rc, lines = flags.audit([entry], read)
    assert rc == expected_rc
    assert called == ['schwab-1m-v2']
    assert ('PASS flag=' in lines[0]) is (mode == 'on')
    if mode == 'absent':
        assert 'running=false' in lines[0] and 'source=settings-default' in lines[0]


def test_process_dotenv_value_missing_from_actual_environment_remains_unknown():
    entry = next(item for item in flags.load_catalog(CATALOG) if item['name'] == KEY)
    rc, lines = flags.audit([entry], lambda _: flags.ServiceEnvironment(
        123, {}, frozenset({'MAI_TAI_' + KEY.upper()}), Path('/controlled/.env')))
    assert rc == 2
    assert lines[0].startswith('UNKNOWN flag=')


class MembershipHarness:
    _observation_symbols = SchwabV2BotService._observation_symbols
    _subscription_symbols = SchwabV2BotService._subscription_symbols
    _entry_membership_allowed = SchwabV2BotService._entry_membership_allowed

    def __init__(self, enabled):
        self.settings = Settings(_env_file=None, protected_symbols='PROTECTED', **{KEY: enabled})
        self._keepwatch_enabled = enabled
        self._watchlist = {'WATCHED'}
        self._exit_coverage = {'HELD'}
        self.strategy = SimpleNamespace(_now_ms=lambda: 1791896400000)
        self._boot_exclusion_sources_readable = False
        self._keepwatch = KeepWatchSession()
        anchor = session_start_ts_ms(self.strategy._now_ms())
        self._keepwatch.update(anchor, {'KEPT', 'PROTECTED'}, self.strategy._now_ms(), {},
                               retain_entries=True)


@pytest.mark.parametrize('enabled', [False, True])
def test_real_observation_union_keeps_exits_but_never_grants_unknown_entry(enabled):
    harness = MembershipHarness(enabled)
    assert harness._subscription_symbols() == ({'WATCHED', 'HELD', 'KEPT'} if enabled
                                               else {'WATCHED', 'HELD'})
    assert not harness._entry_membership_allowed('HELD')
    assert not harness._entry_membership_allowed('KEPT')
    assert not harness._entry_membership_allowed('PROTECTED')
    assert harness._entry_membership_allowed('WATCHED') is (not enabled)


@pytest.mark.parametrize('module', ['gateway', 'held'])
def test_legacy_subscription_harnesses_bind_real_new_dependency_and_explicit_off(module):
    if module == 'gateway':
        from tests.unit.test_v2_gateway_subscription import _Harness
        harness = _Harness(enabled=True, watchlist={'WATCHED'}, coverage={'HELD'})
    else:
        from tests.unit.test_v2_held_symbol_exit_coverage import _Harness
        harness = _Harness(watchlist={'WATCHED'}, coverage={'HELD'})
    assert _Harness._observation_symbols is SchwabV2BotService._observation_symbols
    assert harness._keepwatch_enabled is False
    assert getattr(harness.settings, KEY) is False
    assert harness._subscription_symbols() == {'WATCHED', 'HELD'}


def test_install_manifest_registers_actual_boolean_on_values_without_forging_l14_path():
    manifest = json.loads((CATALOG.parents[1] / 'install/OCT24-L11-L12-L14-ENV-MANIFEST.json').read_text())
    entries = {item['name']: item for item in flags.load_catalog(CATALOG)}
    assert {item['component'] for item in manifest['boolean_install_values']} == {'L11', 'L12'}
    for item in manifest['boolean_install_values']:
        assert item['value'] == 'true'
        assert item['env'] == 'MAI_TAI_' + item['setting'].upper()
        assert Settings.model_fields[item['setting']].annotation is bool
        assert Settings.model_fields[item['setting']].default is item['code_default'] is False
        catalog = entries[item['setting']]
        assert catalog['expected'] is True
        assert item['services'] == [catalog['owning_service'], *catalog.get('also_check_services', [])]
    page = manifest['l14_activation']
    assert page['boolean_switch'] is None
    assert page['aggregate']['env'] == 'MAI_TAI_ATR_SIMULATION_AGGREGATE_RECEIPT_PATH'
    assert page['aggregate']['required'] is True
    assert page['aggregate']['value'] is None
    assert page['aggregate']['sha256'] == '265b847b615df1efbb85cbf3b053bd9d597198309bdebd89d270ca458b0698e8'
    assert manifest['install_ready'] is False and manifest['blockers']
