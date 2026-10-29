"""Current SQL kind clocks must survive without forwarding raw child text."""
import pytest
from atrsimlive1_progress import StageForwarder


@pytest.mark.parametrize('label', ('WINDOW_CENSUS', 'CONDITION_CENSUS', 'QUOTE_CENSUS',
    'TERMINAL:CHSN', 'WINDOW:trades:ELPW', 'WINDOW:quotes:POM', 'QUOTE_CARRY:BRK.B'))
def test_literal_window_kind_stage_survives_split_reads(capsys, label):
    raw = ('1793233048229 '+label+'+:12\n').encode()
    reader = StageForwarder()
    reader.feed(raw[:19])
    reader.feed(raw[19:])
    assert capsys.readouterr().err.encode() == raw


@pytest.mark.parametrize('label', ('WINDOW:password:CHSN', 'TERMINAL:bad_symbol',
    'QUOTE_CARRY:ABCDEFGHIJKLMNOPQ', 'WINDOW_CENSUS SECRET', 'dsn=postgres://SECRET',
    'Traceback (most recent call last):'))
def test_raw_or_nonliteral_window_output_is_not_forwarded(capsys, label):
    reader = StageForwarder()
    reader.feed(('1793233048229 '+label+'+\n').encode())
    assert capsys.readouterr().err == ''
