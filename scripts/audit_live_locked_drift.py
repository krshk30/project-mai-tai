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

## ⛔⭐⭐ THE SECOND AXIS — AN `unset` KEY DOES NOT RUN THE MIRROR (added 2026-09-09)

This script used to print, for every key with no env override:

    not set in env : N  (the mirrored value IS the live path)

⛔ **That sentence is only true when `settings.py`'s default happens to EQUAL the mirror.** When it
does not, the live path is the CODE DEFAULT and the mirror describes a configuration nothing runs.
Measured on 7eca22a7: **13 of the 26 mirrored keys have a divergent code default**, including
`oms_v2_cw_target_pct` (mirror 5.0, default 2.0), `oms_v2_cw_hard_stop_pct` (8.0 vs 5.0) and
`strategy_schwab_1m_v2_enabled` (True vs False).

⇒ So the exact failure this script exists to catch — an env rebuild dropping a load-bearing line —
made it print the OPPOSITE of the truth and then exit 0 ("No drift"). It did not fail silent; it
failed LOUD, in the wrong direction. A watch that fails to a false clean is worse than no watch.

⛔ **Neither sibling tool covers it either.** `ops/health/env_default_drift.py` compares LIVE
settings against code defaults, so when the env line is LOST live becomes the default and that tool
reports NO drift — it goes quiet precisely when the line goes missing. The loss is invisible to both
unless this script calls it.

⇒ An unset key whose code default DIVERGES from the mirror is now RED and exits non-zero.
A key that is unset but whose default AGREES with the mirror stays green: that is the benign case
the original text described, and keeping it quiet is what stops this from becoming noise.

Exit codes:  0 = mirror matches live  ·  1 = DRIFT (env-set disagreement, OR an unset key whose
             code default diverges from the mirror)  ·  2 = CANNOT SEE (refused)
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


UNKNOWN = object()
"""Sentinel: the mirror names a key that is not a Settings field at all."""


def code_defaults(keys) -> dict[str, object]:
    """key -> the value `settings.py` would use with NO env override at all.

    ⛔⭐ Read from `Settings.model_fields[...].default`, NEVER from an instantiated `Settings()`.
    The cron wrapper SOURCES the service env before running this, so an instance would be filled
    from the very env we are auditing and every default would read back as the live value —
    the check would compare the env against itself and could never come out false.
    """
    from project_mai_tai.settings import Settings

    fields = Settings.model_fields
    out: dict[str, object] = {}
    for key in keys:
        field = fields.get(key)
        out[key] = UNKNOWN if field is None else field.default
    return out


def split_unset(unset: list, defaults: dict[str, object]) -> tuple[list, list, list]:
    """(benign, divergent, unknown) for keys with no env override.

    ⛔⭐⭐ A key with no env override does NOT run the mirror — it runs the CODE DEFAULT.
      * benign   — default == mirror, so the mirrored value really IS the live path.
      * divergent— default != mirror. The mirror describes a config NOTHING RUNS, and this is
        exactly what a rebuilt env file that dropped a load-bearing line looks like. RED.
      * unknown  — the mirror names a key that is not a Settings field. Also RED: a mirror entry
        that cannot reach production is drift by another route.
    """
    benign, divergent, unknown = [], [], []
    for key, mirror in unset:
        default = defaults.get(key, UNKNOWN)
        if default is UNKNOWN:
            unknown.append((key, mirror))
        elif default == mirror and isinstance(default, bool) == isinstance(mirror, bool):
            benign.append((key, mirror))
        else:
            divergent.append((key, mirror, default))
    return benign, divergent, unknown


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
    print(f"  not set in env    : {len(unset)}  (classified against settings.py defaults below)")

    try:
        defaults = code_defaults([k for k, _ in unset])
    except Exception as exc:  # noqa: BLE001 — cannot import means cannot answer
        print(f"⛔ CANNOT SEE — REFUSING: cannot read settings.py defaults: {type(exc).__name__}: {exc}")
        print("   ⛔ Without the code defaults an unset key is UNKNOWN, not benign.")
        return 2
    benign, divergent, unknown = split_unset(unset, defaults)

    if benign:
        print("\n  These have NO env override AND the code default equals the mirror, so the")
        print("  mirrored value really IS the live path for them:")
        for key, value in benign:
            print(f"       {key} = {value!r}")

    if divergent:
        print("\n  *** ⛔ UNSET AND DIVERGENT — the mirror describes a config NOTHING RUNS:")
        for key, mirror, default in divergent:
            print(f"       {key}")
            print(f"           LIVE_LOCKED says   = {mirror!r}   <- what the mirror CLAIMS is live")
            print(f"           settings.py default= {default!r}   <- what is ACTUALLY live")
        print("\n  ⛔ There is no env override for these, so production runs the CODE DEFAULT.")
        print("     This is what a rebuilt env file that dropped a load-bearing line looks like.")
        print("     ⛔ ops/health/env_default_drift.py CANNOT see this: with the line gone, live")
        print("        equals the default, so that tool correctly reports no drift. Only this")
        print("        comparison — mirror vs default — can catch a LOST setting.")

    if unknown:
        print("\n  *** ⛔ MIRRORED KEY IS NOT A SETTINGS FIELD — it can never reach production:")
        for key, mirror in unknown:
            print(f"       {key} = {mirror!r}")

    if drifted:
        print("\n  *** DRIFT — an off-VPS / CI replay studies a configuration we are NOT trading:")
        for key, mirror, live in drifted:
            print(f"       {key}\n           LIVE_LOCKED={mirror!r}   live env={live!r}")
        print("\n  ⛔ On the VPS the ENV WINS, so on-VPS replays are unaffected. This is about")
        print("     off-VPS / CI runs, which the module docstring promises are 'faithful'.")
        print("  ⛔ Before 'fixing' these, read test_env_set_values_beat_live_locked — three of")
        print("     them may be disagreeing DELIBERATELY to keep that regression meaningful.")

    if drifted or divergent or unknown:
        print(
            f"\n⛔ RED — {len(drifted)} env-set drift(s), {len(divergent)} unset-and-divergent, "
            f"{len(unknown)} unknown key(s)."
        )
        return 1

    print("\n  No drift: every env-set flag matches the mirror, and every unset key's code")
    print("  default agrees with it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
