"""P6 — does the replay's LIVE_LOCKED mirror still match what production actually runs?

⛔⭐⭐ WHY THIS CANNOT BE A UNIT TEST. `LIVE_LOCKED` claims to encode "the live-LOCKED spec values
... so an off-VPS / CI replay is faithful WITHOUT an env file". Whether that claim is TRUE is a fact
about the box, not about the repo — CI has no env file, so CI can never detect the drift. The only
place the question can be answered is where production's env lives. Hence a script, run on the box.

This is the #592 staleness defect generalised: a hand-maintained mirror of production config that
nothing compares against production. It has already gone stale once (measured 2026-07-28) and the
fix at the time changed the MECHANISM (fallback-not-override, so the env wins on the VPS) without
correcting the encoded VALUES.

## ⛔ WHAT IT FOUND ON 2026-08-19 — AND WHY THAT IS NOT SIMPLY A BUG

Three LIVE_LOCKED entries disagree with the live env:

    strategy_schwab_1m_v2_cw_v2_reclaim_enabled        LIVE_LOCKED False | live true
    strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled  LIVE_LOCKED False | live true
    oms_v2_eh_entry_enabled                           LIVE_LOCKED False | live true

They are the SAME three named in the 07-28 measurement, and they are disagreeing ON PURPOSE:
`test_env_set_values_beat_live_locked` asserts `LIVE_LOCKED.get(key) is False` with the message
"only a meaningful test while LIVE_LOCKED disagrees", and `eh_enabled=False` is a no-op (the overlay
only ADDS flags when True), so LIVE_LOCKED's EH=False is what makes that switch meaningful at all.

⛔ BUT TWO CONTRACTS IN THE SAME FILE THEN CONTRADICT EACH OTHER:
  * the docstring + `test_live_locked_still_applies_with_no_env` — "the fallback must still deliver
    the live regime IN FULL" off-VPS / in CI;
  * the deliberate disagreement above, which makes an off-VPS replay run reclaim OFF and both EH
    paths OFF while production runs all three ON.

Consequence, in the file's own words: "Reclaim off alone drops `max_entries_per_flip` from 2 to 1,
so the replay could not even model the second entry in a segment." Reclaim is also a materially
WORSE population live (reclaims 38% win / −4.98% vs firsts 58% / +1.93%), so an off-VPS replay that
silently omits it does not just differ — it flatters.

⇒ ON-VPS replays are FAITHFUL (the env wins). OFF-VPS / CI replays are NOT, on three flags.
⇒ The resolution is to DECOUPLE: let the regression test build its own disagreement on a key it
  controls, then let LIVE_LOCKED mirror live. That changes what every off-VPS backtest produces, so
  it is the operator's call, not a silent edit — this script only makes the drift impossible to miss.

Exit codes:  0 = mirror matches live  ·  1 = DRIFT  ·  2 = CANNOT SEE (refused)
"""

from __future__ import annotations

import argparse
import os
import re
import sys

DEFAULT_ENV_FILE = "/etc/project-mai-tai/project-mai-tai.env"


def parse_env_file(text: str) -> dict[str, str]:
    """MAI_TAI_* assignments from an env file. Later wins, matching systemd's own behaviour."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(MAI_TAI_[A-Z0-9_]+)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return out


def coerce_matches(mirror_value: object, env_text: str) -> bool:
    """Does the env's TEXT mean the same thing as the mirror's typed value?

    ⛔ Compared by MEANING, not by string. `True` vs `"true"` is a match; `2` vs `"2"` is a match.
    A naive string compare would report drift on every boolean in the list and the report would be
    ignored within a day — a noisy detector is a disabled detector.
    """
    if isinstance(mirror_value, bool):
        return env_text.strip().lower() in ({"true"} if mirror_value else {"false"})
    if isinstance(mirror_value, (int, float)):
        try:
            return float(env_text) == float(mirror_value)
        except ValueError:
            return False
    return env_text.strip() == str(mirror_value).strip()


def audit(live_locked: dict[str, object], env: dict[str, str]) -> tuple[list, list, list]:
    """(drifted, agreed, unset). `unset` means the mirror value IS the live path."""
    drifted, agreed, unset = [], [], []
    for key, value in sorted(live_locked.items()):
        env_key = "MAI_TAI_" + key.upper()
        if env_key not in env:
            unset.append((key, value))
        elif coerce_matches(value, env[env_key]):
            agreed.append((key, value))
        else:
            drifted.append((key, value, env[env_key]))
    return drifted, agreed, unset


def main() -> int:
    ap = argparse.ArgumentParser(description="P6 — LIVE_LOCKED vs the live env (read-only)")
    ap.add_argument("--env-file", default=os.environ.get("MAI_TAI_ENV_FILE", DEFAULT_ENV_FILE))
    args = ap.parse_args()

    try:
        from project_mai_tai.backtest.replay import LIVE_LOCKED
    except Exception as exc:  # noqa: BLE001 — cannot import means cannot answer
        print(f"⛔ CANNOT SEE — REFUSING: cannot import LIVE_LOCKED: {type(exc).__name__}: {exc}")
        return 2

    try:
        with open(args.env_file, encoding="utf-8") as fh:
            env = parse_env_file(fh.read())
    except OSError as exc:
        print(f"⛔ CANNOT SEE — REFUSING: cannot read {args.env_file}: {exc}")
        print("   ⛔ An unreadable env is UNKNOWN, not 'no overrides'. Run this ON THE BOX.")
        return 2
    if not env:
        print(f"⛔ CANNOT SEE — REFUSING: {args.env_file} yielded no MAI_TAI_* assignments.")
        print("   ⛔ An empty parse reads exactly like a box with no overrides. It is not.")
        return 2

    drifted, agreed, unset = audit(LIVE_LOCKED, env)
    print(f"LIVE_LOCKED audit — {len(LIVE_LOCKED)} mirrored setting(s) vs {args.env_file}")
    print(f"  env-set and AGREE : {len(agreed)}")
    print(f"  env-set and DRIFT : {len(drifted)}")
    print(f"  not set in env    : {len(unset)}  (the mirrored value IS the live path)")

    if unset:
        print("\n  ⛔ These have NO env override, so LIVE_LOCKED is the ONLY live path for them.")
        print("     A change to one of these is invisible to every env-based check:")
        for key, value in unset:
            print(f"       {key} = {value!r}")

    if drifted:
        print("\n  *** DRIFT — an off-VPS / CI replay studies a configuration we are NOT trading:")
        for key, mirror, live in drifted:
            print(f"       {key}\n           LIVE_LOCKED={mirror!r}   live env={live!r}")
        print("\n  ⛔ On the VPS the ENV WINS, so on-VPS replays are unaffected. This is about")
        print("     off-VPS / CI runs, which the module docstring promises are 'faithful'.")
        print("  ⛔ Before 'fixing' these, read test_env_set_values_beat_live_locked — three of")
        print("     them may be disagreeing DELIBERATELY to keep that regression meaningful.")
        return 1

    print("\n  No drift: every env-set flag matches the mirror.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
