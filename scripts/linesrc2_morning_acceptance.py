"""Enforce the 60-probe gate; recorded bars are not historical REST envelopes."""
from pathlib import Path
import re
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-s",
         "tests/unit/test_linesrc2_live_fallback.py::"
         "test_recorded_oct8_bars_with_0701_error_publish_without_fabricated_prices",
         "--disable-warnings"],
        cwd=root, capture_output=True, text=True, timeout=120,
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    match = re.search(r"LINESRC2 recorded Oct8: rows=(\d+) symbols=(\d+) probes=(\d+)",
                      result.stdout)
    if result.returncode or match is None:
        print("LINESRC2 ACCEPTANCE REFUSE: replay failed or receipt missing")
        return 2
    count = int(match.group(3))
    if count < 60:
        print(f"LINESRC2 ACCEPTANCE REFUSE: probes={count} required=60; keep flag OFF")
        return 1
    print(f"LINESRC2 ACCEPTANCE PASS: probes={count} required=60; response failure controlled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
