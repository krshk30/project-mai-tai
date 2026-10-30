"""CONTROLLED gate operands; original pinned functions are the behavior oracle.

No service constructor, boot readiness, database, provider or order is involved.
"""
import ast
import json
import logging
from pathlib import Path
from types import MethodType, SimpleNamespace as NS

import pytest

from project_mai_tai import line_entry_receipt as receipt
from project_mai_tai.services import schwab_1m_v2_bot as service
from project_mai_tai.strategy_core.keepwatch import KeepWatchSession


ROOT = Path(__file__).resolve().parents[2]
BOT = ROOT / 'src/project_mai_tai/services/schwab_1m_v2_bot.py'
BASE = json.loads((ROOT / 'tests/fixtures/line_entry_receipt_30c63_baseline.json').read_text())
ORIGINAL = dict(service.__dict__)
for source in BASE['functions'].values():
    exec(compile(source, '<pinned-30c63-entry-oracle>', 'exec'), ORIGINAL)


class Controlled:
    def __init__(self, branch='ordinary', failure='none', *, original=False):
        self.bot = service.SchwabV2BotService.__new__(service.SchwabV2BotService)
        b = self.bot
        self.calls = []
        now = 1_793_363_580_000  # Fixed clock, not a current-production observation.
        anchor = service.session_start_ts_ms(now)
        current = now // 60_000 * 60_000 - 60_000
        state = NS(line_live_fallback=branch == 'line_live_fallback',
                   bars=[NS(timestamp_ms=current)], atr_state='long', atr_trail=1.0)
        ledger = NS(epoch=1, current_bar_ms=current)

        def called(name, value):
            def call(*args, **kwargs):
                self.calls.append(name)
                return value
            return call

        ledger.has_unrecovered_traded_gap = called('traded_gap', failure == 'traded_gap_unrecovered')
        ledger.prepare = called('prepare', object())
        b.strategy = NS(_now_ms=called('clock', now), _atr_period=5,
                        _symbol_states={'TNMG': state},
                        gap_hold_active=called('gap_hold', failure == 'gap_hold'))
        b.settings = NS(protected_symbol_set=set(),
            strategy_schwab_1m_v2_keepwatch_transient_rest_hold_enabled=True)
        b._keepwatch_enabled = True
        b._watchlist = {'TNMG'}
        b._entry_watch_symbols = {'TNMG'}
        b._boot_exclusion_sources_readable = True
        b._keepwatch = KeepWatchSession(anchor_ms=anchor, tradeable={'TNMG'})
        b._keepwatch_rest_hold = NS(deferring=called('deferring', failure == 'rest_hold_deferring'))
        b._keepwatch_boot_excluded_symbols = called('boot_exclusions', {'TNMG'} if failure == 'boot_excluded' else set())
        b._keepwatch_current_publication = called('current_publication', object() if branch == 'current_publication' else None)
        b._line_pg_poll_fallback_current = called('pg_fallback', None)
        b._keepwatch_bar_quarantine = {}
        b._keepwatch_unknown = set()
        b._keepwatch_proven = {'TNMG': NS(anchor_ms=anchor)}
        b._line_readd_needs_live = set()
        b._line_sessions = {'TNMG': ledger}
        b._line_source_waiting = {}
        b._line_published = {'TNMG': NS(request=NS(current_bar_ms=current, spanning_pairs=()))}
        b._line_live_bars = {'TNMG': set()}
        b._keepwatch_halt_binding_current = called('sparse_binding', False)
        b._line_snapshot_current = called('snapshot', failure != 'snapshot_not_current')
        if failure == 'awaiting_sell': b._keepwatch.awaiting_sell.add('TNMG')
        if failure == 'not_tradeable': b._keepwatch.tradeable.clear()
        if failure == 'anchor_mismatch': b._keepwatch.anchor_ms -= 86_400_000
        if failure == 'quarantine': b._keepwatch_bar_quarantine['TNMG'] = anchor
        if failure == 'protected_symbol': b.settings.protected_symbol_set.add('TNMG')
        if failure == 'boot_exclusion_sources_unreadable': b._boot_exclusion_sources_readable = False
        if failure == 'history_unknown': b._keepwatch_unknown.add('TNMG')
        if failure == 'history_proof_missing': b._keepwatch_proven.clear()
        if failure == 'history_anchor_mismatch': b._keepwatch_proven['TNMG'].anchor_ms -= 86_400_000
        if failure == 'readd_needs_live': b._line_readd_needs_live.add('TNMG')
        if failure == 'source_waiting': b._line_source_waiting['TNMG'] = ledger.epoch
        if failure == 'current_bar_stale':
            state.bars[-1].timestamp_ms -= 60_000
            ledger.current_bar_ms -= 60_000
        if failure == 'atr_not_ready': state.atr_state = 'unknown'
        if failure == 'trail_missing': state.atr_trail = None
        if failure == 'trail_nan': state.atr_trail = float('nan')
        if failure == 'trail_zero': state.atr_trail = 0.0
        if failure == 'bars_missing': state.bars = []
        if failure == 'ledger_missing': b._line_sessions.clear()
        if failure == 'spanning_unclean': b._line_published['TNMG'].request.spanning_pairs = ((current-600_000, current),)
        if original:
            for name in ('_line_buy_ready', '_entry_membership_allowed', '_keepwatch_entry_allowed'):
                setattr(b, name, MethodType(ORIGINAL[name], b))
            b._keepwatch.allows_entry = MethodType(ORIGINAL['allows_entry'], b._keepwatch)


@pytest.mark.parametrize('branch,failure', [
    ('current_publication', 'none'), ('current_publication', 'awaiting_sell'),
    ('current_publication', 'not_tradeable'), ('current_publication', 'rest_hold_deferring'),
    ('current_publication', 'boot_excluded'), ('current_publication', 'anchor_mismatch'),
    ('current_publication', 'quarantine'), ('current_publication', 'protected_symbol'),
    ('current_publication', 'boot_exclusion_sources_unreadable'),
    ('current_publication', 'history_unknown'),
    ('ordinary', 'none'), ('ordinary', 'awaiting_sell'), ('ordinary', 'history_unknown'),
    ('ordinary', 'history_proof_missing'), ('ordinary', 'history_anchor_mismatch'),
    ('ordinary', 'readd_needs_live'), ('ordinary', 'source_waiting'),
    ('ordinary', 'snapshot_not_current'), ('ordinary', 'current_bar_stale'),
    ('ordinary', 'atr_not_ready'), ('ordinary', 'trail_missing'),
    ('ordinary', 'trail_nan'), ('ordinary', 'trail_zero'),
    ('ordinary', 'spanning_unclean'), ('ordinary', 'gap_hold'),
    ('line_live_fallback', 'none'), ('line_live_fallback', 'awaiting_sell'),
    ('line_live_fallback', 'bars_missing'), ('line_live_fallback', 'ledger_missing'),
    ('line_live_fallback', 'traded_gap_unrecovered'),
    ('line_live_fallback', 'current_bar_stale'), ('line_live_fallback', 'atr_not_ready'),
    ('line_live_fallback', 'gap_hold'),
])
def test_original_boolean_and_gate_call_order_unchanged(branch, failure):
    before = Controlled(branch, failure, original=True)
    after = Controlled(branch, failure)
    expected = before.bot._line_buy_ready('TNMG')
    actual = receipt.capture(after.bot._line_buy_ready, 'TNMG')
    assert actual.allowed is bool(expected)
    assert after.calls == before.calls
    assert actual.branch == branch
    reason = 'atr_not_ready' if failure in {'trail_missing', 'trail_nan', 'trail_zero'} else failure
    if branch == 'current_publication' and failure == 'history_unknown': reason = 'none'
    assert actual.first_false_predicate == reason


def test_membership_refusal_wins_over_precomputed_unclean_span():
    c = Controlled('ordinary', 'spanning_unclean')
    c.bot._keepwatch.awaiting_sell.add('TNMG')
    actual = receipt.capture(c.bot._line_buy_ready, 'TNMG')
    assert not actual.allowed and actual.first_false_predicate == 'awaiting_sell'
    assert 'snapshot' not in c.calls and 'gap_hold' not in c.calls


def publication_log_statements():
    tree = ast.parse(BOT.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'entry_receipt' for t in node.targets):
            assignment = node
        if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            text = node.args[0].value
            if isinstance(text, str) and 'outcome=published live_append=' in text:
                log = ast.Expr(value=node)
    return compile(ast.fix_missing_locations(ast.Module(body=[assignment, log], type_ignores=[])), str(BOT), 'exec')


def test_actual_publication_log_uses_one_evaluation_and_matching_receipt():
    c = Controlled('current_publication', 'awaiting_sell')
    count = []
    evaluate = c.bot._line_buy_ready
    def counted(symbol):
        count.append(symbol)
        return evaluate(symbol)
    c.bot._line_buy_ready = counted
    records = []
    class Collect(logging.Handler):
        def emit(self, record): records.append(record.getMessage())
    logger = logging.Logger('CONTROLLED-line-receipt')
    logger.addHandler(Collect())
    ledger = c.bot._line_sessions['TNMG']
    ledger.revision = 129
    env = dict(service.__dict__, self=c.bot, symbol='TNMG', ledger=ledger,
               result=NS(request=NS(bars=[1], spanning_pairs=[1])),
               adjacent_live=True, period=5, logger=logger)
    exec(publication_log_statements(), env)
    assert count == ['TNMG'] and len(records) == 1
    assert 'entry_allowed=0' in records[0]
    assert 'first_false_predicate=awaiting_sell branch=current_publication' in records[0]
    assert 'revision=129' in records[0] and 'clean_live_required=10' in records[0]


def test_nested_capture_and_exception_restore_context():
    def outer(symbol):
        receipt.branch('ordinary')
        inner = receipt.capture(lambda s: receipt.predicate(False, 'quarantine'), symbol)
        assert inner.first_false_predicate == 'quarantine'
        return receipt.predicate(False, 'gap_hold')
    assert receipt.capture(outer, 'TNMG').first_false_predicate == 'gap_hold'
    def fails(symbol):
        receipt.predicate(False, 'history_unknown')
        raise ValueError('CONTROLLED-original-exception')
    with pytest.raises(ValueError, match='CONTROLLED-original-exception'):
        receipt.capture(fails, 'TNMG')
    assert receipt.capture(lambda s: True, 'TNMG').first_false_predicate == 'none'


def test_named_wrappers_strip_to_all_four_original_gate_bodies():
    class Strip(ast.NodeTransformer):
        def visit_Expr(self, node):
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == '_entry_branch':
                return None
            return self.generic_visit(node)
        def visit_Call(self, node):
            node = self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id == '_entry_predicate': return node.args[0]
            if (isinstance(node.func, ast.Name) and node.func.id == 'bool' and len(node.args) == 1
                    and isinstance(node.args[0], ast.Attribute) and node.args[0].attr == 'bars'):
                return node.args[0]
            return node
    paths = [BOT, ROOT / 'src/project_mai_tai/strategy_core/keepwatch.py']
    matched = set()
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef) and node.name in BASE['functions']:
                original = ast.parse(BASE['functions'][node.name]).body[0]
                assert ast.dump(Strip().visit(node)) == ast.dump(original)
                matched.add(node.name)
    assert matched == set(BASE['functions'])
