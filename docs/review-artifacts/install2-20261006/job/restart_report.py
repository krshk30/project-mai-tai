"""Run the pinned official collector; disclose exact accepted-open LINESRC errors."""
import contextlib
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import re
import sys

from linesrc_disposition import classify
from release_policy import canonical, digest, need

SOURCE = Path("/home/trader/project-mai-tai/ops/health/v2_restart_evidence.py")


def render(raw, receipts):
    if not receipts:
        return raw
    count = len(receipts)
    need(len(re.findall(r"^Final call: ", raw, re.M)) == 1, "official verdict ambiguous")
    if re.search(r"^Final call: (PASS|EXPECTED BY DESIGN);", raw, re.M):
        raw = re.sub(r"^Final call: (PASS|EXPECTED BY DESIGN);.*$",
                     "Final call: ACCEPTED_OPEN_LINESRC1; reviewed after16 anchored history errors="
                     + str(count) + "; other checks measured; not zero-error/PASS", raw, flags=re.M)
        raw = re.sub(r"^Overall: PASS ", "Overall: ACCEPTED_OPEN_LINESRC1 ", raw, flags=re.M)
    # No FAIL or UNKNOWN is waived. Even an accepted-open error cannot hide another failure.
    raw = raw.replace("| Tracebacks |", "| Unaccepted Tracebacks |")
    return raw + "\nAccepted Open LINESRC1:\n" + "\n".join(
        "- " + item["at_utc"] + " " + item["reason"] + " raw_sha256=" + item["raw_sha256"]
        for item in receipts) + "\n"


def render_held(raw, proof, now):
    from post_proof import overnight_hold
    held = overnight_hold(proof['lines'], proof['state'], now)
    expected = ['no post-restart restoration_complete=1 line',
                'no literal post-restart BOOT-HOLD release with restoration_complete=1']
    need(raw.count('Final call: ') == 1 and '\nUnknowns:' not in raw, 'other official unknown; no hold admission')
    need('\nFailures:\n' in raw, 'official failure inventory absent')
    failures = [line[2:] for line in raw.split('\nFailures:\n', 1)[1].splitlines() if line.startswith('- ')]
    need(failures == expected, 'other official failure; no hold admission')
    rows = [line for line in raw.splitlines() if line.startswith('| ') and not line.startswith('| Check |')]
    fail_names = [line.split('|')[1].strip() for line in rows if line.endswith('| FAIL |')]
    need(fail_names == ['REST warmup', 'BOOT-HOLD released']
         and not any(line.endswith('| UNKNOWN |') for line in rows)
         and any(line.startswith('| Bar continuity |') and line.endswith('| N/A_OFF_SESSION |') for line in rows),
         'official scope is not exact overnight pair')
    result = raw.split('\nFailures:\n', 1)[0]
    result = re.sub(r'^Overall:.*$', 'Overall: ACCEPTED_HELD_OFFSESSION (not restoration PASS)', result, flags=re.M)
    result = re.sub(r'^Final call:.*$', 'Final call: ACCEPTED_HELD_OFFSESSION; held, expected no scheduled bars, population='
                    + str(held['population']) + '; restoration/release UNMEASURED; next scheduled-bar proof required', result, flags=re.M)
    for name in ('REST warmup', 'BOOT-HOLD released'):
        result = re.sub(r'^(\| ' + re.escape(name) + r' \| .*\| )FAIL( \|)$', r'\1HELD_EXPECTED_OFFSESSION\2', result, flags=re.M)
    return result + '\nLiteral overnight evidence:\n- ' + held['source_line'] + '\nOriginal collector failures preserved in official-unaccepted-scope receipt; not called PASS.\n'


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    args = list(args)
    held_path = None
    if '--overnight-hold-proof' in args:
        index = args.index('--overnight-hold-proof')
        held_path = Path(args[index + 1])
        del args[index:index + 2]
    need("report" in args and "--output" in args, "wrapper report/output required")
    output = Path(args[args.index("--output") + 1])
    spec = importlib.util.spec_from_file_location("install2_official_collector", SOURCE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    original, receipts, unclassified = module.parse_log_files, [], []

    def reviewed(files, *, since, service=None):
        files = [(name, list(lines)) for name, lines in files]
        filtered = []
        for name, lines in files:
            selected, source_indexes = [], []
            stamp = None
            for i, line in enumerate(lines):
                match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})", line)
                if match:
                    stamp = datetime.strptime(match[1], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)
                if stamp is not None and stamp >= since:
                    selected.append(line.rstrip("\n"))
                    source_indexes.append(i)
            try:
                admitted, headers = classify(service, selected)
            except Exception as exc:
                # A classification refusal must not suppress the official failure report.
                admitted, headers = [], set()
                unclassified.append(dict(service=service, path=name, reason=str(exc),
                                         raw_sha256=digest(("\n".join(selected) + "\n").encode())))
            receipts.extend(admitted)
            remove = {source_indexes[index] for index in headers}
            filtered.append((name, [line for i, line in enumerate(lines) if i not in remove]))
        return original(filtered, since=since, service=service)

    module.parse_log_files = reviewed
    capture, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(errors):
        rc = module.main(args)
    raw = capture.getvalue()
    if not raw and rc == 2:
        # The official collector can return UNKNOWN before writing a report. Keep its
        # actual reason and publish a dated UNKNOWN, never a synthetic success.
        raw = ("# V2 Restart Evidence\n\nGenerated: " + module.format_moment(datetime.now(timezone.utc))
            + "\nOverall: UNKNOWN (collector could not establish scope)\n"
            + "Final call: UNKNOWN; official collector returned unreadable evidence\n\nUnknowns:\n- "
            + errors.getvalue().strip().replace("\n", "\n- ") + "\n")
        output.write_text(raw)
    need(len(raw.encode()) <= 3_000_000 and output.is_file(), "official report missing/overflow")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    official = output.with_name(output.name + "." + run_id + ".official-unaccepted-scope.txt")
    from daily import exclusive
    exclusive(official, raw.encode())
    rendered = render(raw, [row for row in receipts if row['classification'] == 'ACCEPTED_OPEN_LINESRC1'])
    # Optional root-pinned acknowledgement admits one measured identity, never a future restart.
    if Path('/home/trader/preopen-daily/upgrade-ack.json').is_file():
        from upgrade_ack import current, render as render_upgrade
        rendered, acknowledged = render_upgrade(rendered, current())
        if acknowledged:
            rc = 0
    fallback = [row for row in receipts if row['classification'] == 'EXPECTED_BY_DESIGN_SEEDED_BOOT_FALLBACK']
    if fallback:
        rendered += '\nExpected seeded boot fallback (not fresh-bar restoration):\n' + '\n'.join(
            '- ' + row['at_utc'] + ' ' + row['reason'] + ' raw_sha256=' + row['raw_sha256'] for row in fallback) + '\n'
    if unclassified:
        rendered += '\nUnclassified log evidence (not waived; official parser received original lines):\n'
        rendered += json.dumps(unclassified, sort_keys=True) + '\n'
    if held_path is not None:
        rendered = render_held(rendered, json.loads(held_path.read_bytes()), datetime.now(timezone.utc))
        rc = 0
    output.write_text(rendered)
    exclusive(output.with_name(output.name + "." + run_id + ".linesrc.json"), canonical(dict(
        accepted_open_count=len(receipts), classification="ACCEPTED_OPEN_LINESRC1" if receipts else "NONE",
        receipts=receipts, unclassified=unclassified, collector_stderr=errors.getvalue(),
        official_unaccepted_scope_sha256=digest(raw.encode()))))
    print(rendered, end="")
    return rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("UNKNOWN restart disposition error_type=" + type(exc).__name__ + " reason=" + str(exc), file=sys.stderr)
        raise SystemExit(2)
