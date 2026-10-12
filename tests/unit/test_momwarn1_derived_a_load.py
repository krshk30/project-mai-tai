"""Small CONTROLLED cardinality/source tests, not full tape or burst receipts."""

import gzip
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("derive_a_load", ROOT / "scripts/momwarn1_derive_a_load.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.fixture(scope="module")
def scanner(tmp_path_factory):
    compiler = shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        pytest.skip("C++ compiler unavailable; no replacement rate scanner")
    binary = tmp_path_factory.mktemp("derived_scanner") / "scanner"
    subprocess.run([compiler, "-O3", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                    str(ROOT / "scripts/momwarn1_trade_second_counts.cpp"), "-lz", "-o", str(binary)], check=True)
    return binary


def controlled_source(tmp_path, lines):
    path = tmp_path / "CONTROLLED.csv.gz"
    with gzip.open(path, "wt") as stream:
        stream.write("ticker,sip_timestamp,price,size,conditions\n"+"\n".join(lines)+"\n")
    return path


def test_unsorted_all_prints_distinct_symbol_seconds_and_downloaded_denominator(scanner, tmp_path):
    start, end = module.window("2026-10-09")
    path = controlled_source(tmp_path, [
        f'BRK.B,{start+2*module.NS},1,1,"12,37"',
        f'LOW,{start},0.01,0,"37"',  # Low price/excluded print still contributes load.
        f'LOW,{start+12},0.02,0,"37"',
        f'BRK.B,{start},1,1,""',
        f'OUTSIDE,{end},1,1,""',
        f'BRK.B,{start+module.NS},1,1,""',
    ])
    result = json.loads(subprocess.check_output([str(scanner), str(path), str(start), str(end)]))
    assert result["symbols_downloaded"] == 3 and result["window_printed_symbols"] == 2
    assert result["source_rows_all_day"] == 6 and result["window_raw_prints"] == 5
    assert result["counts_per_second"][:4] == [2, 1, 1, 0]
    assert sum(result["counts_per_second"]) == 4
    assert result["gzip_eof_crc_verified"]


@pytest.mark.parametrize("bad", ["BAD,wrong,1,1,", "BAD,12,1,1", 'BAD,12,1,1,"unclosed'])
def test_malformed_raw_source_refuses_instead_of_partial_rate(scanner, tmp_path, bad):
    start, end = module.window("2026-10-09")
    path = controlled_source(tmp_path, [bad])
    result = subprocess.run([str(scanner), str(path), str(start), str(end)], capture_output=True)
    assert result.returncode == 2 and not result.stdout


def test_corrupt_gzip_refuses_eof_proof(scanner, tmp_path):
    start, end = module.window("2026-10-09")
    path = controlled_source(tmp_path, [f"LOW,{start},0.01,0,37"])
    damaged = bytearray(path.read_bytes())
    damaged[-8] ^= 1
    path.write_bytes(damaged)
    result = subprocess.run([str(scanner), str(path), str(start), str(end)], capture_output=True)
    assert result.returncode == 2 and not result.stdout


def test_percentile_includes_observed_zero_seconds_and_partial_not_wider_market():
    result = module.summarize_counts([0]*999+[4])
    assert result["peak_symbols_per_second"] == 4 and result["p999_symbols_per_second"] == 0
    start, end = module.window("2026-10-12", "04:10")
    payload = dict(counts_per_second=[0]*19200, downloaded_symbols=["LOW"], symbols_downloaded=1)
    result = module.finish("2026-10-12", payload, start, end, partial=True)
    assert result["seconds"] == 19200 and result["partial_observed_lower_bound"]
    assert not result["complete_requested_input_scope"]
    assert "wider" in result["partial_bound_semantics"]


def test_frozen_sealed_manifest_hash_mismatch_cannot_be_skipped(tmp_path):
    seal = tmp_path / "data/oct12_after16_sealed_partial"
    seal.mkdir(parents=True)
    source = tmp_path / "2026-10-12-LOW.history.jsonl"
    source.write_text("{}")
    (seal / "retained-input-SHA256SUMS").write_text("0"*64+"  "+str(source)+"\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        module.partial_day(tmp_path, None)
