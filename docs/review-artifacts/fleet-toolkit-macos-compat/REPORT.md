# Fleet toolkit macOS compatibility and safety report

Date: 2026-08-31

## Outcome

The corrected isolated toolkit passes under Apple Bash 3.2 with native BSD tools:

```text
129 passed, 0 failed
every guard refused its known-bad input, and every control still worked
```

The complete patch applied without offsets to a fresh untouched normalized baseline. The applied
tree matched the corrected normalized tree byte-for-byte before execution and independently passed
`129 passed, 0 failed` under the same controlled environment.

The live toolkit at `/Users/velkris/.claude/mai-tai-fleet` was not edited. Its `.checksums`
SHA-256 remains `74822d944fcc055f4d05b2a38227c74983e81864c7dada43c3ef2a38de95d6e3`.
The source snapshot, normalized baseline, corrected isolated tree, and fresh applied tree retain
those exact `.checksums` bytes. No persistent checksum record was re-recorded.

## Controlled environment

Both final runs used Apple Bash 3.2 with this exact PATH:

```text
/tmp/fleet-macos-update-tools.6HtvPh:/usr/bin:/bin:/usr/sbin:/sbin
```

The private overlay contained links only to the installed `gh` and `python3` dependencies. Apple
system `git`, `awk`, `sed`, `stat`, `date`, `cmp`, and `patch` remained authoritative; no Homebrew
GNU coreutils directory was exposed.

## Safety fixes

### Checksum records and diagnostics

- `checksums.sh record` extracts the digest from either hash implementation and writes one
  canonical `hash  path` record through a temporary file.
- Verification accepts BSD/shasum `hash  path` and GNU binary-mode `hash *path` records.
- Malformed records, missing files, and mismatches fail closed.
- Verification no longer exits from inside its read loop. The promised drift header and failed
  path are printed before the nonzero exit.
- Controlled pairs prove unchanged and drifted behavior for both BSD and GNU output formats.

### Manifest byte gate

- `promote.sh` compares the normalized live and PR manifest streams directly with `cmp -s`.
- Exact balanced bytes complete promotion.
- A single added byte produces `MANIFEST MISMATCH`, creates no archive, and leaves the source
  journal unchanged.
- A generated mutant disables the comparison. The same one-byte mismatch then clears a journal,
  proving the control distinguishes and kills the missing-guard defect.
- Existing VOID, freeze, head, carrier, main, and retry bindings remain fail closed.

### Archived-file fallback

- The fallback locates an archived journal with `find`, then calls `hash_md5` in the parent Bash
  process where the compatibility function is defined.
- The rotation-failure fixture now moves the first journal into the archive and fails the second
  move, forcing and proving the archived-only recovery path.

## Counts

| Platform / state | Passed | Failed | Interpretation |
|---|---:|---:|---|
| macOS untouched, system-only PATH | 69 | 33 | Original untouched reproduction |
| macOS accepted portability patch at prior head | 120 | 0 | Superseded by the safety review |
| macOS corrected isolated tree | 129 | 0 | Native BSD full run |
| macOS fresh application of corrected patch | 129 | 0 | Independent full run; exact tree match |
| Windows Git Bash prior head `f47fca7a` | 116 | 0 | Review evidence that exposed the false-green controls |
| Windows Git Bash corrected exact patch | UNMEASURED | UNMEASURED | Must be rerun independently; no claim made |

The macOS count is higher because its BSD/GNU exact-path controls include Darwin-specific cases.
Windows counts are reported separately and are not inferred from macOS.

## Artifact integrity

- Complete patch SHA-256: `e3e608ab85e1d332728cef4d5408e9ba6d95714669ed40faf8c96f638cf6c554`
- Final `checksums.sh`: `3195502d98394fb053fdf2214e538ecd8ae5a77bd28dbecba075857cef18f60a`
- Final `promote.sh`: `6450195e8499f8e584821968bcc68b0badae18cd160c96b20a9bc10920634275`
- Final `selftest.sh`: `d980ff9f8b19a3ae32c1215787fbec87696fef2391882f2627a3b5d3a4b199ea`
- Final `MACOS.md`: `0007701f4410019d99e458085f2e2cce268dd8f4fcb217097b1b828f990eb727`
- Remaining macOS failures: none.
- Corrected Windows result: `UNMEASURED`; no Windows compatibility claim is made.

The package is review-only. It was not installed, pinned, merged, or promoted.
