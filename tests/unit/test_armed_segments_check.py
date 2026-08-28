"""Tests for ops/health/armed_segments_check.py (P1.4's external pager).

The point of these is NOT that the check returns GREEN on healthy live state — it did that on the
first run, and a check that has only ever gone green proves nothing (the validate_buy_stop lesson:
it proved Webull accepts an order shape, not that the path works). Each of the three fault
conditions the bot names -- "dangerous present, entries_held too long, or snapshot stale" -- is
asserted to actually PAGE, and each documented not-a-fault is asserted to stay quiet.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[2] / "ops" / "health" / "armed_segments_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("armed_segments_check", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def asc(monkeypatch):
    mod = _load()
    pages: list[tuple[str, str]] = []
    monkeypatch.setattr(mod, "page", lambda title, body, **kw: pages.append((title, body)))
    # Healthy defaults; each test perturbs exactly one thing.
    monkeypatch.setattr(mod, "unit_active", lambda: True)
    monkeypatch.setattr(mod, "safety_flag_on", lambda: True)
    monkeypatch.setattr(mod, "uptime_secs", lambda: 86_400.0)
    monkeypatch.setattr(
        mod,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": [],
                "entries_held": False,
                "restoration_complete": True,
            },
            3.0,
        ),
    )
    monkeypatch.setattr(mod.sys, "argv", ["armed_segments_check.py"])
    mod._pages = pages
    return mod


def _seg(symbol, *, entries=0, max_entries=1, capped=False, reconstructed=False, dangerous=False):
    return {"symbol": symbol, "arm_bar_ts": 1784244420000, "entries_this_flip": entries,
            "max_entries": max_entries, "capped": capped, "reconstructed": reconstructed,
            "dangerous": dangerous}


# --------------------------------------------------------------- the healthy baseline

def test_green_on_live_healthy_state(asc, monkeypatch):
    """Reproduces the real 2026-07-17 snapshot: 4 armed, 3 capped, none reconstructed."""
    segs = [_seg("IQST", entries=1, capped=True), _seg("DXST", entries=1, capped=True),
            _seg("LBGJ"), _seg("ASTN", entries=1, capped=True)]
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": segs,
                "entries_held": False,
                "restoration_complete": True,
            },
            3.0,
        ),
    )
    assert asc.main() == 0
    assert asc._pages == []


# --------------------------------------------------------------- fault 1: dangerous

def test_pages_on_dangerous_segment(asc, monkeypatch):
    """A reconstructed segment that survived P1.3's seed-cap — the CPHI cap-reset shape."""
    segs = [_seg("CPHI", entries=0, reconstructed=True, dangerous=True)]
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: ({"cw_armed_segments": segs, "entries_held": True}, 3.0))
    assert asc.main() == 2
    title, body = asc._pages[0]
    assert "DANGEROUS" in title and "CPHI" in body


def test_reconstructed_but_capped_is_not_dangerous(asc, monkeypatch):
    """P1.3 doing its job is not a fault. Only reconstructed AND uncapped pages."""
    segs = [_seg("CPHI", entries=1, capped=True, reconstructed=True, dangerous=False)]
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": segs,
                "entries_held": False,
                "restoration_complete": True,
            },
            3.0,
        ),
    )
    assert asc.main() == 0
    assert asc._pages == []


# --------------------------------------------------------------- fault 2: boot-hold

def test_thresholds_are_sane():
    """CONFIG GUARD — pins the constants themselves, not just the comparisons.

    Caught by mutation testing: every other boot-hold test derives its uptime from
    BOOT_HOLD_GRACE_SECS, so they move WITH the constant and stay green even if it is set to a
    billion seconds — which silently disables the pager. A threshold nobody asserts is a threshold
    that can be turned off by accident. Same family as the 120s reconcile grace still being a guess.
    """
    mod = _load()
    assert 60 <= mod.BOOT_HOLD_GRACE_SECS <= 3600, "boot-hold grace must be minutes, not disabled"
    assert 60 <= mod.SNAPSHOT_STALE_SECS <= 900, "staleness bar must be minutes, not disabled"
    assert mod.SCAN_COUNT >= 10, "ORB shares the stream; too small a scan can miss v2 entirely"


def test_pages_when_boot_hold_outlives_grace(asc, monkeypatch):
    """entries_held with 0 dangerous long after boot => v2 is silently entry-less and cannot page
    about itself (it IS the stuck thing). This is the whole reason the pager is external.

    Uptime is ABSOLUTE (1h), not grace-relative: a grace-relative uptime makes this test pass for
    any grace, including one large enough to disable the check.
    """
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": [],
                "entries_held": True,
                "restoration_complete": True,
            },
            3.0,
        ),
    )
    monkeypatch.setattr(asc, "uptime_secs", lambda: 3600.0)
    assert asc.main() == 2
    assert "BOOT-HOLD NEVER RELEASED" in asc._pages[0][0]


def test_pages_with_the_real_reason_when_restoration_never_completes(asc, monkeypatch):
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": [],
                "entries_held": True,
                "restoration_complete": False,
            },
            3.0,
        ),
    )
    monkeypatch.setattr(asc, "uptime_secs", lambda: 3600.0)

    assert asc.main() == 2
    title, body = asc._pages[0]
    assert "STATE RESTORATION INCOMPLETE" in title
    assert "restoration_complete=false" in body


def test_pages_when_entries_are_released_before_restoration_completes(asc, monkeypatch):
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": [],
                "entries_held": False,
                "restoration_complete": False,
            },
            3.0,
        ),
    )

    assert asc.main() == 2
    title, body = asc._pages[0]
    assert "RELEASED BEFORE STATE RESTORATION" in title
    assert "entries_held=false" in body


def test_missing_restoration_field_is_unknown_not_a_defect(asc, monkeypatch, capsys):
    """An old/flag-off bot cannot prove restoration precedence, but it also did not violate it."""
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: ({"cw_armed_segments": [], "entries_held": False}, 3.0),
    )

    assert asc.main() == 0
    assert asc._pages == []
    assert "COULD_NOT_TELL: restoration_complete is absent" in capsys.readouterr().out


@pytest.mark.parametrize("invalid", ["false", 0])
def test_non_boolean_restoration_state_is_unknown_not_green(asc, monkeypatch, invalid):
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": [],
                "entries_held": False,
                "restoration_complete": invalid,
            },
            3.0,
        ),
    )

    assert asc.main() == 2
    assert len(asc._pages) == 1
    assert "RESTORATION STATE UNKNOWN" in asc._pages[0][0]


def test_boot_hold_within_grace_is_quiet(asc, monkeypatch):
    """entries_held is TRUE at boot BY DESIGN — paging on it would fire on every restart."""
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: ({"cw_armed_segments": [], "entries_held": True}, 3.0))
    monkeypatch.setattr(asc, "uptime_secs", lambda: 30.0)
    assert asc.main() == 0
    assert asc._pages == []


def test_pages_when_boot_hold_cannot_be_aged(asc, monkeypatch):
    """Unknown uptime + held => fail LOUD, not quiet. We cannot prove it is within grace."""
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: ({"cw_armed_segments": [], "entries_held": True}, 3.0))
    monkeypatch.setattr(asc, "uptime_secs", lambda: None)
    assert asc.main() == 2
    assert "uptime unknown" in asc._pages[0][0]


# --------------------------------------------------------------- fault 3: snapshot

def test_pages_when_snapshot_stale(asc, monkeypatch):
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: ({"cw_armed_segments": [], "entries_held": False},
                                                            asc.SNAPSHOT_STALE_SECS + 1))
    assert asc.main() == 2
    assert "SNAPSHOT STALE" in asc._pages[0][0]


def test_pages_when_no_snapshot_while_active(asc, monkeypatch):
    """Active but publishing nothing => armed state is unobservable, which IS the fault."""
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: (None, None))
    assert asc.main() == 2
    assert "CHECK BLIND" in asc._pages[0][0]


# --------------------------------------------------------------- documented not-a-faults

def test_inactive_v2_is_not_a_fault(asc, monkeypatch):
    """Armed segments are in-memory only and die with the process — the OPPOSITE of the OMS case,
    where down = blind = page. Stopping v2 is the documented way to CLEAR armed segments, so a
    stopped v2 must never page (it would fire every deploy)."""
    monkeypatch.setattr(asc, "unit_active", lambda: False)
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: (None, None))
    assert asc.main() == 0
    assert asc._pages == []


def test_safety_flag_off_is_not_a_fault(asc, monkeypatch):
    """Flag off => P1.3 never seed-caps => dangerous is expected. Paging would train the operator
    to ignore the pager."""
    monkeypatch.setattr(asc, "safety_flag_on", lambda: False)
    segs = [_seg("CPHI", reconstructed=True, dangerous=True)]
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: ({"cw_armed_segments": segs, "entries_held": True}, 3.0))
    assert asc.main() == 0
    assert asc._pages == []


def test_absent_flag_uses_the_shipped_off_default(monkeypatch):
    mod = _load()

    def fake_sh(command: list[str]) -> str:
        if command[:3] == ["systemctl", "show", mod.UNIT]:
            return "1234"
        if command[:2] == ["sudo", "tr"]:
            return "SOME_OTHER_FLAG=true\n"
        raise AssertionError(command)

    monkeypatch.setattr(mod, "sh", fake_sh)
    assert mod.safety_flag_on() is False


def test_sh_failure_is_unknown_not_an_empty_success(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError("unreadable")),
    )

    assert mod.sh(["sudo", "tr", "\\0", "\\n", "/proc/1234/environ"]) is None


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        (1, "permission denied"),
        (0, ""),
    ],
)
def test_sh_nonzero_or_empty_stdout_is_unknown(monkeypatch, returncode, stdout):
    mod = _load()
    result = type("Completed", (), {"returncode": returncode, "stdout": stdout})()
    monkeypatch.setattr(mod.subprocess, "run", lambda *_args, **_kwargs: result)

    assert mod.sh(["sudo", "tr", "\\0", "\\n", "/proc/1234/environ"]) is None


def test_pages_when_live_flag_environ_read_fails(monkeypatch):
    """The live flag can be ON while MainPID -> /proc races; unreadable must never mean OFF."""
    mod = _load()
    pages: list[tuple[str, str]] = []

    def fake_sh(command: list[str]) -> str | None:
        if command[:3] == ["systemctl", "show", mod.UNIT]:
            return "1234"
        if command[:2] == ["sudo", "tr"]:
            return None
        raise AssertionError(command)

    monkeypatch.setattr(mod, "unit_active", lambda: True)
    monkeypatch.setattr(mod, "sh", fake_sh)
    monkeypatch.setattr(mod, "page", lambda title, body, **_kw: pages.append((title, body)))
    monkeypatch.setattr(mod.sys, "argv", ["armed_segments_check.py"])

    assert mod.main() == 2
    assert len(pages) == 1
    assert "SAFETY FLAG UNKNOWN" in pages[0][0]


def test_missing_armed_segments_field_is_unknown_not_green(asc, monkeypatch):
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: ({"entries_held": False, "restoration_complete": True}, 3.0),
    )

    assert asc.main() == 2
    assert "PAYLOAD UNKNOWN" in asc._pages[0][0]


def test_missing_dangerous_field_is_unknown_not_green(asc, monkeypatch):
    segment = _seg("DAIC")
    segment.pop("dangerous")
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": [segment],
                "entries_held": False,
                "restoration_complete": True,
            },
            3.0,
        ),
    )

    assert asc.main() == 2
    assert "DANGER STATE UNKNOWN" in asc._pages[0][0]


def test_unparsable_snapshot_time_is_unknown_not_green(asc, monkeypatch):
    monkeypatch.setattr(
        asc,
        "latest_v2_snapshot",
        lambda: (
            {
                "cw_armed_segments": [],
                "entries_held": False,
                "restoration_complete": True,
            },
            None,
        ),
    )

    assert asc.main() == 2
    assert "SNAPSHOT TIME UNKNOWN" in asc._pages[0][0]


# --------------------------------------------------------------- precedence

def test_dangerous_wins_over_boot_hold(asc, monkeypatch):
    """Both fire together in the real failure (dangerous => held). Report the CAUSE, not the symptom."""
    segs = [_seg("CPHI", reconstructed=True, dangerous=True)]
    monkeypatch.setattr(asc, "latest_v2_snapshot", lambda: ({"cw_armed_segments": segs, "entries_held": True}, 3.0))
    monkeypatch.setattr(asc, "uptime_secs", lambda: asc.BOOT_HOLD_GRACE_SECS + 60)
    assert asc.main() == 2
    assert len(asc._pages) == 1
    assert "DANGEROUS" in asc._pages[0][0]
