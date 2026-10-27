#!/bin/sh
set -eu
out=/Users/velkris/.codex/task-queues/analysis/L13-COLD-PARITY-153149-CONTROLS-20261022
for file in "$out.log" "$out.xml"; do
    test ! -e "$file" || { printf 'Refusing existing output: %s\n' "$file" >&2; exit 2; }
done
exec env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS -u MAI_TAI_DATABASE_URL -u DATABASE_URL -u PGDSN -u PGPASSWORD \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=src:. \
    /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -rP --tb=short \
    -o addopts= -p no:cacheprovider tests/unit/test_l13_p3_retained.py \
    tests/unit/test_l13_p3_cold_parity.py -k 'not actual_retained_packet' \
    --junitxml="$out.xml" > "$out.log" 2>&1
