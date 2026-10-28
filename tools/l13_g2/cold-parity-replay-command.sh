#!/bin/sh
set -eu
out=/Users/velkris/.codex/task-queues/analysis/L13-COLD-PARITY-153149-REPLAY-20261022
for file in "$out.log" "$out.xml" "$out.json"; do
    test ! -e "$file" || { printf 'Refusing existing output: %s\n' "$file" >&2; exit 2; }
done
exec env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=src:. \
    L13_G2_CLAIM=P3_RETAINED L13_G2_RETAINED_MANIFEST=tools/l13_g2/retained-p3-input.json \
    L13_G2_RESULT="$out.json" \
    /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -rP --tb=short \
    -o addopts= -p no:cacheprovider \
    tests/unit/test_l13_p3_retained.py::test_actual_retained_packet_real_worker_and_scanner_postwarmup \
    --junitxml="$out.xml" > "$out.log" 2>&1
