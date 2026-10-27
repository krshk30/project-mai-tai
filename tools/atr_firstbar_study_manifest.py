"""Main-only supplied-study file declaration; never market capture completeness."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from project_mai_tai.atr_simulation.firstbar_reference import bind_reference_seed, reviewed_173_manifest
from project_mai_tai.atr_simulation.firstbar_study import _boundary


def build_study_manifest(root):
    root = Path(root).resolve(strict=True)
    manifest = reviewed_173_manifest(str(root))
    selector = (root / "four/agg4.py").read_bytes()
    if hashlib.sha256(selector).hexdigest() != manifest.selector_source.sha256:
        raise ValueError("reference_selector_changed")
    seed = bind_reference_seed((root / "four_columns.csv").read_bytes(),
        (root / "four/raw_split.json").read_bytes(), manifest=manifest)
    identities = sorted({case.reference_key[:2] for case in seed.cases})
    if len(seed.cases) != 173 or len(identities) != 77:
        raise ValueError("reviewed_seed_population_changed")
    rows = []
    for symbol, day in identities:
        path = (root / "fbh/mtape" / f"{symbol}_{day}.t").resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("contained_existing_study_file_required")
        raw = path.read_bytes()
        if not raw:
            raise ValueError("empty_supplied_file_requires_explicit_unknown_declaration")
        rows.append({"symbol": symbol, "day": day,
            "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
            "records": len(raw.splitlines()),
            "start_ns": _boundary(day, 4), "end_ns": _boundary(day, 20),
            "complete": True, "gaps": [],
            "history_label": "replay, in-sample; underlying capture completeness UNKNOWN",
            "evidence": {"scope": "ALL_SUPPLIED_FILE_BYTES_RECORDS_ONLY",
                "ruling": "C1 LATEST 2026-10-23 20:29 ET, answer 20:20",
                "artifact": str(path), "underlying_market_capture_complete": None,
                "producer_completion_evidence": "UNKNOWN", "live_coverage_unchanged": True}})
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output).absolute()
    if not output.parent.resolve(strict=True).is_relative_to(Path("/private/tmp")):
        raise ValueError("private_output_required")
    rows = build_study_manifest(args.reference_root)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(rows, stream, sort_keys=True, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == "__main__":
    main()
