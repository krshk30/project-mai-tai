"""Exact C1 source contract controls; no packet/native acceptance substitutes."""

from pathlib import Path
import subprocess

import pytest

from scripts.momold1_live_offer_proof import (
    ADMISSION, HANDOFF, PRE_REF, compare_amod_roots, exact_momneg_allowlist,
    handoff_source_proof,
)


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sources():
    def original(path):
        return subprocess.run(["git", "show", f"{PRE_REF}:{path}"], cwd=ROOT,
                              check=True, capture_output=True).stdout

    return original(HANDOFF), (ROOT / HANDOFF).read_bytes(), original(ADMISSION), (ROOT / ADMISSION).read_bytes()


def test_only_exact_guard_delta_is_approved_and_raw_diff_is_retained(sources):
    report = exact_momneg_allowlist(*sources)
    assert report["approved"] and report["pre_pins_match"]
    assert report["admission_unchanged"] and report["producer_serialization_unchanged"]
    assert '+            raw_prior_close = record.payload.get("prior_close")' in report["raw_allowlisted_diff"]
    assert '-                            start, end, Decimal(str(detect["price"]))' in report["raw_allowlisted_diff"]
    assert '+                            start, end, signal_price' in report["raw_allowlisted_diff"]
    legacy = handoff_source_proof(ROOT)
    assert legacy["equal"] is False  # Preserve the original byte inequality, not a digest refresh.
    assert legacy["approved"] is True


@pytest.mark.parametrize("mutation", ["guard", "producer", "comment", "admission", "pre", "no_guard"])
def test_any_nonallowlisted_byte_or_missing_guard_refuses(sources, mutation):
    before, after, admission_before, admission_after = sources
    if mutation == "guard":
        after = after.replace(b"signal_price < prior_close", b"signal_price <= prior_close", 1)
    elif mutation == "producer":
        after = after.replace(b'"one_shot": "true"', b'"one_shot": "false"', 1)
    elif mutation == "comment":
        after += b"\n# unrelated change\n"
    elif mutation == "admission":
        admission_after += b"\n"
    elif mutation == "pre":
        before += b"\n"
    else:
        after = before
    assert (before, after, admission_before, admission_after) != sources
    assert not exact_momneg_allowlist(before, after, admission_before, admission_after)["approved"]


def test_same_root_is_rejected_before_any_worker_or_packet_read(tmp_path):
    with pytest.raises(ValueError, match="distinct actual PRE/POST roots required"):
        compare_amod_roots(tmp_path, tmp_path, tmp_path / "missing.csv")


def test_wrong_pre_root_fails_before_worker_or_packet_read(tmp_path):
    path = tmp_path / HANDOFF
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not the pinned original source")
    with pytest.raises(ValueError, match="PRE root differs from original source"):
        compare_amod_roots(tmp_path, ROOT, tmp_path / "missing.csv")
