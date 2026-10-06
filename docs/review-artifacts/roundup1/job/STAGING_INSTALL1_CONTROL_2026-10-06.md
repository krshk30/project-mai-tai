# Parent-Only Install1 Staging And Attended Entry

**NOT EXECUTED.** Only parent may perform production staging/execution, strictly
after16:00 October6 and with required fresh admission evidence. These commands
do not authorize staging now, bypass a holding, invoke a future-dated gate or
perform recovery. No new lane/classifier/moving main can enter this package.

Frozen source:121f8e09f76313bc8ebc6d07566bf67b3473742a.
Manifest:b36f12380d9288df4fae7460079eec85042fe530e573d8f256ca7d38bf24a61b.
Approval:96ebf1c08e99aae242b973e3315161a2624b96f22f06251f5f6a4edd7bcd5c83.
APP4805ddc81184c76b4d5cef5c483c809edb666fe6; BOX7823a6fa baseline.
See RELEASE_VERIFICATION_INSTALL1_CONTROL_2026-10-06.md for exact tests, hashes,
runtime requirements and local independent package reproduction.

## Before Any Production Staging

Parent first independently verifies the exact source/package/test receipt and
obtains fresh release/holding/gate evidence under the standing rules. Neither
local approval nor blocking_acceptance=[] means live admission. Source policy
requires all initial live gates before source/env writes, and then repeats
phase-aware gates before every service action. Actual bot holdings still block.

The following time guard is read-only; parent must run it before transfer. It
refuses16:00:00 exactly, another date and all pre-close times. It is not a flat
gate substitute. The epoch1791316800 is2026-10-06T20:00:00Z/16:00:00ET.

```sh
ssh mai-tai-vps 'test "$(TZ=America/New_York date +%F)" = 2026-10-06 && test "$(date +%s)" -gt 1791316800'
```

Create a local transport from the independently verified parent package. The
transport tar is not an immutable release identity; record its actual digest.
Do not overwrite an existing tar or existing remote destination.

```sh
test ! -e /tmp/roundup1-install1-121f8e09.tar.gz
tar -czf /tmp/roundup1-install1-121f8e09.tar.gz -C /tmp/roundup1-install1-parent-121f8e09 .
shasum -a 256 /tmp/roundup1-install1-121f8e09.tar.gz
ssh mai-tai-vps 'mkdir -m 700 /tmp/roundup1-install1-transfer-121f8e09'
scp /tmp/roundup1-install1-121f8e09.tar.gz mai-tai-vps:/tmp/roundup1-install1-transfer-121f8e09/release.tar.gz
ssh mai-tai-vps 'sha256sum /tmp/roundup1-install1-transfer-121f8e09/release.tar.gz'
```

STOP if the transport digest differs. Only then, parent opens an attended root
shell on the box and executes these literal staging steps. The exact new job
directory must not exist; mkdir refuses collision. No application action here.

```sh
set -eu
test "$(TZ=America/New_York date +%F)" = 2026-10-06
test "$(date +%s)" -gt 1791316800
JOB=/home/trader/roundup1-install1-20261006-121f8e09
mkdir -m 700 "$JOB"
tar --no-same-owner -xzf /tmp/roundup1-install1-transfer-121f8e09/release.tar.gz -C "$JOB"
```

Before importing or executing any staged module, use only trusted interpreter
stdlib to verify exact manifest/approval hashes, inventory, per-artifact bytes,
owner/mode and no symlinks. This is read-only. Failure STOP; no repairs/waivers.

```sh
PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python - "$JOB" <<'PY'
import hashlib, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
sha = lambda data: hashlib.sha256(data).hexdigest()
raw = (root / "release.json").read_bytes()
assert sha(raw) == "b36f12380d9288df4fae7460079eec85042fe530e573d8f256ca7d38bf24a61b"
assert sha((root / "approval.json").read_bytes()) == "96ebf1c08e99aae242b973e3315161a2624b96f22f06251f5f6a4edd7bcd5c83"
manifest = json.loads(raw)
assert manifest["blocking_acceptance"] == []
assert manifest["plan_commit"] == "121f8e09f76313bc8ebc6d07566bf67b3473742a"
assert set(p.name for p in root.iterdir()) == set(manifest["artifacts"]) | {"release.json", "approval.json"}
for name, expected in manifest["artifacts"].items():
    assert pathlib.Path(name).name == name
    assert sha((root / name).read_bytes()) == expected
for path in [root, *root.iterdir()]:
    assert not path.is_symlink()
    assert path.stat().st_uid == 0 and path.stat().st_mode & 0o022 == 0
print("STAGED BYTES VERIFIED; runtime admission NOT yet claimed")
PY
```

Only after parent confirms this verification and fresh intended admission,
invoke the one attended entry. Preserve native UTC systemd output; do not
inherit any MAI_TAI_* overrides. The entry refuses all such overrides, root/
hash/approval/date mismatch, nonblocking lock collision and an existing attempt.

```sh
env -u TZ PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python "$JOB/attended.py" b36f12380d9288df4fae7460079eec85042fe530e573d8f256ca7d38bf24a61b
```

There is no production dry-run switch or fake gate bypass. initial() performs
live read-only admission before source/env changes, but records exclusive
attempt files. The runner then backs up all named targets, updates only exact
APP/env/catalog bytes and follows seven phases. Runtime failures create STOP
with actual states and one existing-adapter notification. Do not rerun, remove
the attempt, edit manifests, issue reset-failed, restart another unit or start
recovery. Parent reports exact partial state for a separately authorized action.
Closeout starts only the daily timer, never tonight's real preopen check.
