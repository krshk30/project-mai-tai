"""Settings whose LIVE value differs from the CODE DEFAULT.

⭐ WHY THIS EXISTS. Three of these were found by accident in a single evening (2026-07-28):
`atr_flip_vol_floor` (5000 in code / 10000 live), `cw_v2_reclaim_gap_bars` (0 / 1), and the
flag-derived entry cap. Each time, reading `settings.py` gave the WRONG live value — and once it
made the operator revise a threshold decision on a false premise ("keep 5K" when production was
already 10K).

⛔ Aligning every default is NOT the fix: flipping one default broke 43 tests, and the injected-
settings seam means a default flip can silently split global-vs-injected readers. The durable fix is
to make the divergence CHEAP TO SEE — run this before quoting any default as a live value.

A bool going False->True is an ordinary feature enable and reads honestly, so `--numeric` filters to
the hazard class: settings where default and live are BOTH numbers but DIFFERENT. Those are the ones
that look plausible and are wrong.

Read-only: no DB, no broker, no writes. Safe during market hours.

usage:  env_default_drift.py [--numeric] [--all]
"""
from __future__ import annotations

import sys

from project_mai_tai.settings import Settings, get_settings

SECRET_HINTS = ("key", "secret", "token", "password", "dsn", "url", "webhook", "credential")


def drift(numeric_only: bool) -> list[tuple[str, object, object]]:
    live = get_settings().model_dump()
    out: list[tuple[str, object, object]] = []
    for name, field in Settings.model_fields.items():
        default = field.default
        current = live.get(name)
        if repr(default).startswith("PydanticUndefined"):
            continue
        if numeric_only:
            if isinstance(default, bool) or isinstance(current, bool):
                continue
            if not isinstance(default, (int, float)) or not isinstance(current, (int, float)):
                continue
        if default != current:
            out.append((name, default, current))
    return sorted(out)


def main() -> int:
    numeric_only = "--all" not in sys.argv
    rows = drift(numeric_only)
    scope = "NUMERIC" if numeric_only else "ALL"
    print(f"{scope} settings where LIVE differs from the code default: {len(rows)}\n")
    for name, default, current in rows:
        if any(h in name.lower() for h in SECRET_HINTS):
            print(f"  {name}\n      settings.py says <redacted>   PRODUCTION runs <redacted>")
        else:
            print(f"  {name}\n      settings.py says {default!r}   PRODUCTION runs {current!r}")
    if numeric_only:
        print("\n(pass --all to include bools/strings; those are usually ordinary feature enables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
