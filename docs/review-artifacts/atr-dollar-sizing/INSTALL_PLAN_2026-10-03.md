# 2026-10-03 combined ATR dollar sizing, NFQ1 and guard install plan

Status: revised for independent review. Operator approved the combined scope on
2026-10-03; execution still requires claude-1's review of this exact plan commit.
The 09:00 ET request was already past when both clocks were read at 09:42 ET.
Operator's replacement instruction: **start after revised-plan review**.
Application SHA: `608339894a1cfb33284e695196df55c18f312889`.
#1086 merged at that SHA; tree `e620eee0c36271a399d062c1a0e749b1b6a5c856`
equals pinned head `b20225fe67421bb125884b1216decdcb8d51f9ba`.
The October 2 app reminder did not produce a box installation. This plan does
not claim that reminder was a systemd timer. Its expired automation is deleted.

## Listable, review-gated box job

- Timer: `project-mai-tai-sizing-nfq1-install-20261003.timer`.
- One-shot: `project-mai-tai-sizing-nfq1-install-20261003.service`.
- Artifacts: `/home/trader/after-hours/2026-10-03/sizing-nfq1-job/`.
- The timer checks once per minute on October 3 only; `Persistent=false`.
  It does not catch up the missed 09:00 execution.
- The unit requires a root-owned mode 0600 `approval.json` naming this exact
  plan commit, application SHA and runner SHA256, with reviewer `claude-1`.
  Codex writes that receipt ONLY after the reviewer approves this exact head.
  Missing receipt means the unit is skipped; that is not an installed application.
- A fixed attempt directory prevents an automatic second run after any abort.
  `RemainAfterExit=yes` prevents another run after success. No automatic recovery.
- `run_install_2026-10-03.sh` is the literal executable companion to this
  document; `verify_install_approval.py` validates the receipt and runner.
  Both are included in the same plan commit for review. No hidden remote script.
- Record `systemctl cat`, timer NEXT, unit FragmentPath and artifact hashes when
  staging. Report review-gated versus running versus completed separately.
- No timer or unit for Monday is installed until this install's verified journal
  exists. Then use the separately approved Monday procedure, with this application
  SHA and observed paper PID/start, in a listable 03:40 ET one-shot/timer.
  Its names will be `project-mai-tai-option-a-guard-start-20261005.service` and
  `project-mai-tai-option-a-guard-start-20261005.timer`; not installed yet.

## Candidate and service scope

- Current verified box HEAD: `fd69004a08c812429b5ba3ac01837c05b132a92b`, clean.
- #1084 is merged at `ed3c84a8e930ea75bf5bddfe1edc45057d8ba26c`.
- #1082 merged at `e57a2fbde0c52332961b39a2b7030d71aa429832` from independently
  pinned head `775e348a962ea050a7d621d1e402d04955960297`.
- #1083 merged at `b8b0dafbdf583ca4af9eddc8dbf922cbf887a482` from pinned
  `edb98dd7929d0ea98e910272ceadcda561caf58b`; bounded XRANGE COUNT 1 only.
- #1086 NFQ1 is included at the application SHA above. Enable its mirror-only
  switch and 10000 ms age explicitly in the env and read them from new OMS /proc.
  Shared reactive/pre-market quote age remains 2000 ms. No gateway/cron changes.
- #1085 RPG1 is EXCLUDED, not pinned and not merged.
- Restart **OMS, schwab-1m-v2 and the strategy companion** in the coordinated
  sequence below. The dollar defaults are ON in code; old v2 and new OMS must not
  be left as a completed deployment because old market opens lack the price basis.
- **No gateway restart in this plan.** Gateway PID 2346625 remains on its existing
  in-memory code. Thus #1084 full-state persistence and #1077 gateway logging do
  not become live through this install. The owner repair remains essential.
- No orb or orb-schwab restart, watch reinstall, migration, replay, paper restart,
  or guard start during this install. The existing guard unit points at the checkout, so the
  merged #1083 file and narrowed #1077 sampler are loaded on its next invocation.
  `MONDAY_GUARD_START_2026-10-05.md` requires separate review and starts only
  Monday's dated guard before 03:50 ET. Active paper prepares at 03:55 and streams at
  04:00 itself; that procedure does not start/restart paper. Do not invoke an
  old treatment date.
- A later gateway restart onto #1077/#1084 needs a separate plan: install and
  hash-verify every narrowed 1008 detector copy first (sampler, fleet-health and
  any installed pager reader), then fresh preserved-owner/content proof. This
  plan does not silently make that later restart safe.

### Unplanned restart and installed 1008 readers

Checkout advance changes disk code, not these running gateway/orb-schwab
processes. An **unplanned auto-restart** would load #1077 timestamped INFO
logging (and, for the gateway, #1084 owner persistence). That is identity drift:
stop the install and page, not a restart silently covered by this plan. Bare
`1008` matching can misclassify INFO counts such as `11008 records` as a kick;
the actual vendor signature is `1008 (policy violation)`. Inspect the line and
code before calling it a gateway policy event; do not disable any watch.

Read-only box inventory on 2026-10-02, to recheck before execution:

| Reader | Installed path and SHA256 before checkout advance | Treatment |
| --- | --- | --- |
| Fleet-health | `/home/trader/project-mai-tai/ops/health/fleet_health_check.py`; `02c248458ff232ca30a5be72935677d8b2c1b2c60f6012f9adf54bc7d981ddbf` | Old bare needle. Root cron invokes this checkout copy every 5 minutes; the next invocation after advance loads the narrowed #1077 code. Hash against APPROVED_SHA and record any in-flight old invocation. |
| Guard's 1008 sampler | `/home/trader/project-mai-tai/scripts/option_a_treatment_1008_sampler.py`; `5e06e2f19c79bc4d211f452465d30a4b7ac4f324ad04705332f668ce97b511aa` | Old bare needle on disk now; next guard invocation loads the narrowed checkout copy. No running guard/sampler found in this audit; recheck for an unexpected old process before execution. |
| INC1 watcher | `/home/trader/unexercised_watch/watch.py`; `45df60c60fe55af89406d20c29de277a44634d6bc6308586a4e0b62e718b8272` | Separate old sha-guarded copy remains untouched. This file has **no 1008 detector**; do not attribute a bare-needle page to it. Its missing new incident sources remain a separate accepted coverage gap. |

Verify both checkout detector files against the exact approved Git blobs after
advance. A long-lived pre-advance reader does not reload itself when its file
changes. Any newly discovered standalone detector copy or old running sampler
requires review, not an unapproved restart. The 10-02 guard is presently failed
(MainPID=0, NRestarts=0); preserve its result and follow Monday's reset-or-note
procedure, never start the old instance.

## Preconditions and evidence

Run only on Saturday 2026-10-03 after exact-plan review. The missed Friday
authorization is not reused. Require a proven prior nightly rotation and stable
log inodes; refuse a start during 19:55-20:10 ET. Keep the install attended
through all three new process identities and final read-only proofs. No weekend
order is submitted to demonstrate the changes.

Record a no-concurrent-deploy check, six service PID/start/NRestarts snapshots
(OMS, strategy, v2, orb, orb-schwab, gateway), paper state, installed guard unit
hashes, old env hash, preopen hash, isolated gate file hashes and git diff paths.
The current preopen hash is
`d58aa3510d16977ad245d98e05e0d0c4dff9fc6db41d58907d22f86e7e46cbd2`.
Any drift needs evidence and review before writing; never silently re-pin it.

Use an O_EXCL evidence directory under `/home/trader/after-hours/2026-10-03/`
and journal `/home/trader/fleet_health/deployments-20261003.md`. Capture outputs
without printing env secrets. Backups are root-owned mode 0600; preserve original
file owner and mode when installing replacements.

### Flatness before every stop/start/restart

Use the existing reviewed helper at
`/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py`, SHA256
`831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e`.
It reads both brokers directly and rejects incomplete/unreadable responses.
For this install, add the following strict wrapper checks to its output: both
open-managed counts zero, `nonzero_virtual=[]`, and `net_bot_fills=[]`.
This removes yesterday's NXL row/net-fill exception. Require Webull holding rows
zero. The existing recorded manual Schwab-holding rule remains allowed only with
zero bot books and fills. Any additional exception requires an operator ruling.

```bash
flat_now() {
  local out rc=0
  out=$(timeout 45s sudo "$REPO/.venv/bin/python" "$FLAT_CHECK" 2>&1) || rc=$?
  printf '%s\nstrict_flat_rc=%s\n' "$out" "$rc" | sudo tee -a "$RUN/steps.log"
  (( rc == 0 )) || return "$rc"
  printf '%s\n' "$out" | grep -Eq '^FRESH_DIRECT_READ .*webull_holding_rows=0 ' || return 2
  printf '%s\n' "$out" | grep -Fq "open_managed={'live:schwab_1m_v2': 0, 'live:orb': 0}" || return 2
  printf '%s\n' "$out" | grep -Fq 'nonzero_virtual=[]' || return 2
  printf '%s\n' "$out" | grep -Fq 'net_bot_fills=[]' || return 2
  entry_orders_clear
}
entry_orders_clear() {
  sudo "$REPO/.venv/bin/python" - <<'PY'
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from project_mai_tai.db.models import BrokerAccount, BrokerOrder
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings
s = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
with Session(build_engine(s.database_url, connect_timeout_s=5, statement_timeout_ms=5000)) as db:
    rows = db.execute(select(BrokerAccount.name, BrokerOrder.id, BrokerOrder.symbol, BrokerOrder.status)
        .join(BrokerAccount, BrokerOrder.broker_account_id == BrokerAccount.id)
        .where(BrokerAccount.name.in_(('live:schwab_1m_v2', 'live:orb')),
               func.lower(BrokerOrder.side) == 'buy',
               func.lower(BrokerOrder.status).in_(('pending', 'submitted', 'accepted', 'partially_filled')))).all()
print(f'working_bot_entry_orders={len(rows)} rows={rows}')
raise SystemExit(1 if rows else 0)
PY
}
```

Also query working bot BUY orders on both accounts (pending/submitted/accepted/
partially_filled); any such order blocks. Record v2's 15:45 entry-window end from
`/proc`, but it does not excuse armed segments: **the v2 restart preflight is
blocking**, independently of fleet flatness. No database write, manual position
row closure or armed-state edit is allowed.

Immediately before stopping v2, run `ops/preflight/preflight_v2_restart.sh` and
require exit 0. A nonempty arm set re-issues the entry cap on restart (Bug 2).
Only the operator may name the exact live set and accept that cost at the
instant of the run. Do not populate an override from a command's output, a prior
run or an unattended retry. If the set moves, stop for a fresh ruling. The
single gate invocation below reads the live published set, checks its freshness,
compares it with the operator's literal set and applies the override together;
there is no separate cached read-and-bypass. Its other gates still apply.

```bash
# Empty for the normal path. Set only to the operator's freshly named exact set.
V2_ARM_OVERRIDE=''
v2_restart_gate() {
  local rc=0
  local args=()
  if [[ -n "$V2_ARM_OVERRIDE" ]]; then
    args=(--operator-override "$V2_ARM_OVERRIDE" --i-accept-bug2)
  fi
  sudo bash "$REPO/ops/preflight/preflight_v2_restart.sh" "${args[@]}" \
    | sudo tee "$RUN/v2-restart-preflight.txt" || rc=$?
  printf 'v2_restart_preflight_rc=%s\n' "$rc" | sudo tee -a "$RUN/steps.log"
  (( rc == 0 )) || return "$rc"
}
```

The shell uses `pipefail`; a blocked gate cannot be hidden by `tee`. Copy the
literal `[OVERRIDE] ... ARMED SEGMENT(S) accepted by the OPERATOR: ...` line and
the ruling into the deployment journal when used. No override is pre-authorized
by this document. A normal zero-arm read needs no override.

Before the first write, rc 1/2 means wait and reread within the approved evening;
after any stop/start, rc 1/2 halts remaining operations and pages with actual
service states. A command success alone is not proof of flatness or readiness.

## Literal checkout and env changes

```bash
set -euo pipefail
REPO=/home/trader/project-mai-tai
BOX_SHA=fd69004a08c812429b5ba3ac01837c05b132a92b
APPROVED_SHA=608339894a1cfb33284e695196df55c18f312889
RUN=/home/trader/after-hours/2026-10-03/sizing-nfq1-install
FLAT_CHECK=/home/trader/after-hours/2026-10-01/option-a-strict-flat-check.py
test "$(TZ=America/New_York date +%F)" = 2026-10-03
NOW_ET=$(TZ=America/New_York date +%H%M)
(( 10#$NOW_ET < 1955 || 10#$NOW_ET >= 2010 ))
[[ "$APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$BOX_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo mkdir -m 0700 "$RUN" # Refuse if an earlier run already owns this path.
install_abort() {
  local rc=$?
  trap - EXIT
  (( rc == 0 )) && return 0
  set +e
  local states message
  states=$(sudo systemctl show -p Id -p MainPID -p ActiveState -p SubState -p NRestarts \
    project-mai-tai-oms.service project-mai-tai-strategy.service project-mai-tai-schwab-1m-v2.service 2>&1)
  message="Sizing install stopped rc=$rc; no automatic recovery attempted; $RUN; $states"
  printf '%s\n' "$message" | sudo tee -a "$RUN/steps.log"
  curl -sS --fail-with-body --connect-timeout 10 --max-time 30 \
    -H 'Title: ATR sizing install needs operator' -H 'Priority: urgent' \
    -d "$message" https://ntfy.sh/mai-tai-preopen-28806a5a97b7 >/dev/null || \
    printf 'PAGE DELIVERY UNKNOWN\n' | sudo tee -a "$RUN/steps.log"
  exit "$rc"
}
trap install_abort EXIT
sudo -u trader git -C "$REPO" fetch origin main:refs/remotes/origin/main
sudo -u trader git -C "$REPO" merge-base --is-ancestor "$BOX_SHA" "$APPROVED_SHA"
sudo -u trader git -C "$REPO" merge-base --is-ancestor "$APPROVED_SHA" origin/main
sudo -u trader git -C "$REPO" diff --name-only "$BOX_SHA" "$APPROVED_SHA" | sudo tee "$RUN/source-paths.txt"
sudo -u trader git -C "$REPO" diff --name-only "$APPROVED_SHA" origin/main | while IFS= read -r p; do
  case "$p" in docs/*) ;; *) printf 'REFUSE unapproved later code %s\n' "$p"; exit 1 ;; esac
done
test "$(sudo sha256sum "$FLAT_CHECK" | awk '{print $1}')" = 831913206678c5bd2fa6718173c4c5d7b5887c62e44217ad10b6776851f0381e
flat_now
# Record working BUY orders=0, armed segments, identities and Redis checkpoint here.
sudo cp -p /etc/project-mai-tai/project-mai-tai.env "$RUN/env.before"
sudo cp -p /home/trader/preopen.sh "$RUN/preopen.before"
sudo sha256sum "$RUN/env.before" "$RUN/preopen.before" | sudo tee "$RUN/backups.sha256"
```

Edit only these values in the backed-up env, refusing duplicate definitions or an
unreadable file. If absent, append them; if present, replace that one definition.
Preserve every other byte and log a redacted diff of just these five keys:

```text
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_NOTIONAL_USD=600
MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ENTRY_NOTIONAL_USD=300
MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_MAX_SHARES=1000
MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED=true
MAI_TAI_OMS_V2_WEBULL_MIRROR_QUOTE_MAX_AGE_MS=10000
```

The two notional fields have `require_process_env=true` in expected_numeric.json
and are checked on both v2 and OMS. Explicit max shares pins the same cap.

```bash
sudo "$REPO/.venv/bin/python" - <<'PY'
import os
import re
from pathlib import Path
p = Path('/etc/project-mai-tai/project-mai-tai.env')
before = p.read_text()
updated = before
changes = {
    'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_NOTIONAL_USD': '600',
    'MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_ENTRY_NOTIONAL_USD': '300',
    'MAI_TAI_STRATEGY_SCHWAB_1M_V2_ENTRY_MAX_SHARES': '1000',
    'MAI_TAI_OMS_V2_WEBULL_MIRROR_FRESH_PRICE_ENABLED': 'true',
    'MAI_TAI_OMS_V2_WEBULL_MIRROR_QUOTE_MAX_AGE_MS': '10000',
}
for key, value in changes.items():
    pattern = re.compile(r'^(?:export[ \t]+)?' + re.escape(key) + r'=.*$', re.M | re.I)
    found = pattern.findall(updated)
    if len(found) > 1:
        raise SystemExit(f'UNKNOWN duplicate env key {key}')
    old = found[0].split('=', 1)[1] if found else '<unset>'
    if found:
        updated = pattern.sub(key + '=' + value, updated)
    else:
        updated += ('' if updated.endswith('\n') else '\n') + key + '=' + value + '\n'
    print(f'{key}: {old} -> {value}')
st = p.stat()
tmp = p.with_name(p.name + '.atr-sizing.tmp')
fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, st.st_mode & 0o777)
with os.fdopen(fd, 'w') as out:
    out.write(updated)
    out.flush()
    os.fsync(out.fileno())
os.chown(tmp, st.st_uid, st.st_gid)
os.replace(tmp, p)
PY
```

```bash
flat_now
sudo -u trader git -C "$REPO" switch --detach "$APPROVED_SHA"
sudo -u trader "$REPO/.venv/bin/python" -m pip install --no-deps --disable-pip-version-check -e "$REPO"
test "$(sudo -u trader git -C "$REPO" rev-parse HEAD)" = "$APPROVED_SHA"
test -z "$(sudo -u trader git -C "$REPO" status --porcelain)"
sudo -u trader "$REPO/.venv/bin/python" -c 'import pathlib, project_mai_tai; p=pathlib.Path(project_mai_tai.__file__).resolve(); print(p); assert str(p)=="/home/trader/project-mai-tai/src/project_mai_tai/__init__.py"'
```

Runtime refresh only changes the trader-owned editable package/entry-point
metadata. It does not install dependencies or touch service definitions.

## Coordinated service sequence

Do not declare success with one member of the pair still on old code. The exact
sequence, with the working-entry query repeated alongside every `flat_now`, is:

```bash
flat_now
v2_restart_gate
sudo systemctl stop project-mai-tai-schwab-1m-v2.service
# Old v2 has exited: the following cursor cannot include its historical output.
V2_LOG=/var/log/project-mai-tai/schwab-1m-v2.log
V2_LOG_ID=$(sudo stat -c '%d:%i' "$V2_LOG")
V2_LOG_OFFSET=$(sudo stat -c '%s' "$V2_LOG")
flat_now
sudo systemctl stop project-mai-tai-strategy.service
flat_now
sudo systemctl restart project-mai-tai-oms.service
sudo systemctl is-active --quiet project-mai-tai-oms.service
# Verify new OMS PID/start, NRestarts=0, healthy post-start heartbeat and flags below.
flat_now
sudo systemctl start project-mai-tai-schwab-1m-v2.service
sudo systemctl is-active --quiet project-mai-tai-schwab-1m-v2.service
V2_NEW_PID=$(sudo systemctl show -p MainPID --value project-mai-tai-schwab-1m-v2.service)
v2_post_restart_check startup
# Verify loaded dollar settings below; repeat the log/identity proof at close-out.
flat_now
redis_strategy_proof before "$RUN/strategy-redis-before.json"
sudo systemctl start project-mai-tai-strategy.service
sudo systemctl is-active --quiet project-mai-tai-strategy.service
STRATEGY_AFTER=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
redis_strategy_proof after "$RUN/strategy-redis-before.json" "$STRATEGY_AFTER"
v2_post_restart_check closeout
```

Install an EXIT handler around this attended block that journals actual states of
all three services and pages on any abort. Do not silently start an old/new mixed
pair after an error. No rollback or extra restart is pre-authorized by this plan;
an abort after checkout advance may leave old in-memory code on a new checkout,
and an abort after stopping a unit may leave it stopped. Stay attended, name that
state in the page, and obtain the operator's recovery instruction.

### Mandatory v2 post-restart checklist

Define this read-only function before the sequence. It records only bytes
appended after the old v2 exited, with inode/size/PID stability checks; it does
not count earlier tracebacks as current-process errors. A traceback stops the
install for code/design inspection and a classified report, not a raw verdict.

```bash
v2_post_restart_check() {
  sudo "$REPO/.venv/bin/python" - "$V2_LOG" "$V2_LOG_ID" "$V2_LOG_OFFSET" \
    "$V2_NEW_PID" "$RUN/v2-$1.log" <<'PY'
import subprocess
import sys
from pathlib import Path
path, expected_id, offset, expected_pid, output = sys.argv[1:]
unit = 'project-mai-tai-schwab-1m-v2.service'
def identity():
    raw = subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID',
        '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts',
        '-p', 'ExecMainStartTimestamp'], text=True)
    return dict(line.split('=', 1) for line in raw.splitlines())
before = identity()
assert before['ActiveState'] == 'active' and before['SubState'] == 'running', before
assert int(expected_pid) > 0 and before['MainPID'] == expected_pid, before
assert before['NRestarts'] == '0' and before['ExecMainStartTimestamp'], before
p = Path(path)
st = p.stat()
assert f'{st.st_dev}:{st.st_ino}' == expected_id, 'UNKNOWN v2 log rotated'
length = st.st_size - int(offset)
assert 0 <= length <= 10_000_000, 'UNKNOWN v2 log range'
with p.open('rb') as stream:
    stream.seek(int(offset))
    data = stream.read(length)
after = p.stat()
assert (after.st_dev, after.st_ino) == (st.st_dev, st.st_ino) and after.st_size >= st.st_size
assert identity() == before, 'UNKNOWN v2 identity changed during check'
with Path(output).open('xb') as out:
    out.write(data)
text = data.decode('utf-8', errors='replace')
traces = text.count('Traceback (most recent call last):')
holds = [line for line in text.splitlines() if '[V2-BOOT-HOLD]' in line]
warmup = [line for line in text.splitlines() if
          'schwab_v2 warmup feed for ' in line or '[V2-REST-WARMED]' in line]
print(f'V2_POST_RESTART identity={before} tracebacks={traces} bytes={length} path={output}')
print('V2_BOOT_HOLD latest=' + (holds[-1] if holds else 'UNEXERCISED no marker yet'))
print(f'V2_WARMUP_LINES count={len(warmup)}')
print('\n'.join(warmup))
assert traces == 0, 'UNKNOWN until current-process traceback is inspected against code/design'
PY
}
```

Require active/running, NRestarts=0, a new PID/start and zero new-process
tracebacks at startup and close-out. For BOOT-HOLD paste the literal
`[V2-BOOT-HOLD] released` line when observed; do not replace it with `is-active`.
The code's `_try_complete_boot_state_restoration` requires readable exclusions,
a nonempty selected/tradeable scanner population, successful DB seeding for each
selected symbol and resolved warm-up gates. With a stable seeded population but
no fresh bars, `_release_seeded_boot_warmup_on_timeout` can release that warm-up
gate after **369 seconds** (69 + 300), checked on the 5-second state cycle;
`_cw_boot_hold_check` still requires no dangerous uncapped reconstructed segment.
It is not an unconditional 369-second boot timer.

An empty overnight watchlist therefore keeps BOOT-HOLD held **EXPECTED BY DESIGN**;
there is no promised overnight release time. Record the live population and
exact hold reason, not PASS. Codex-2 owns the read-only follow-up from Monday
04:00 ET / the first nonempty watchlist: verify REST `schwab_v2 warmup feed for`
lines for each added name, `[V2-REST-WARMED]` (or the explicitly logged bounded
fallback), and the literal hold release. Any unresolved hold when the entry
window opens is investigated and paged with the missing condition; no restart
or forced release. Assign this follow-up when scheduling the approved install.

The Saturday install is outside the normal Schwab trading-bar session. This is
not proof of historical continuity: record actual bar activity rather than
assuming the weekend repaired prior holes. Claude-1 still owns Monday 2026-10-05
about 07:10 ET bar continuity. Overnight/empty-population warm-up remains pending.

### Strategy Redis proof function

Define this function before running the sequence. `before` writes one new small
metadata file; `after` checks immediately and once per second until initialization
is proven. It never reads snapshot-batches. INFO/HGETALL are small metadata;
the heartbeat call is capped at 25 small events, not snapshot payloads.

```bash
redis_strategy_proof() {
  sudo "$REPO/.venv/bin/python" - "$@" <<'PY'
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from redis import Redis
from project_mai_tai.settings import Settings
s = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
r = Redis.from_url(s.redis_url, decode_responses=True, socket_timeout=5)
required = {'strategy-engine', 'schwab-1m-v2', 'orb', 'orb-schwab', 'momentum-paper'}
mode, path = sys.argv[1:3]
def capture():
    stats, memory = r.info('stats'), r.info('memory')
    owners = r.hgetall(f'{s.redis_stream_prefix}:market-data-subscription-owners')
    consumers = {k for k in owners if not k.startswith('_')}
    if not required <= consumers or owners.get('_migration_complete') != '1':
        raise RuntimeError('owner fields or migration marker missing')
    if not re.fullmatch(r'\d+-\d+', owners.get('_last_applied_id', '')):
        raise RuntimeError('owner checkpoint missing or malformed')
    for name in consumers:
        values = json.loads(owners[name])
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise RuntimeError(f'unreadable owner {name}')
    result = {'evicted_keys': int(stats['evicted_keys']), 'used_memory': int(memory['used_memory']),
              'owners': owners, 'at_utc': datetime.now(UTC).isoformat()}
    print(json.dumps(result), flush=True)
    if result['used_memory'] > 1_600_000_000:
        raise RuntimeError('Redis used_memory above 1.6 GB')
    return result
try:
    if mode == 'before':
        current = capture()
        with Path(path).open('x') as out:
            json.dump(current, out)
    elif mode == 'after':
        before = json.loads(Path(path).read_text())
        start = datetime.fromisoformat(sys.argv[3].replace('Z', '+00:00'))
        deadline = time.monotonic() + 180
        while True:
            current = capture()
            if current['evicted_keys'] != before['evicted_keys']:
                raise RuntimeError('Redis eviction count changed during strategy restart')
            prior = {k for k in before['owners'] if not k.startswith('_')}
            if not prior <= current['owners'].keys():
                raise RuntimeError('previous consumer disappeared')
            healthy = False
            for _, fields in r.xrevrange(f'{s.redis_stream_prefix}:heartbeats', count=25):
                event = json.loads(fields['data'])
                if event.get('source_service') == 'strategy-engine':
                    stamp = datetime.fromisoformat(event['produced_at'].replace('Z', '+00:00'))
                    age = (datetime.now(UTC) - stamp).total_seconds()
                    healthy = (stamp > start and 0 <= age < 30 and event['payload']['status'] == 'healthy')
                    break
            if healthy:
                final = capture()
                if final['evicted_keys'] != before['evicted_keys']:
                    raise RuntimeError('Redis eviction count changed at final read')
                print('STRATEGY_REDIS_PROOF PASS eviction_unchanged=1 all_consumers=1 migration_complete=1')
                break
            if time.monotonic() >= deadline:
                raise RuntimeError('new healthy strategy heartbeat not proven within 180 seconds')
            time.sleep(1)
    else:
        raise RuntimeError('unknown Redis proof mode')
except Exception as exc:
    print(f'UNKNOWN strategy Redis proof: {type(exc).__name__}: {exc}', flush=True)
    raise SystemExit(2)
finally:
    r.close()
PY
}
```

### Mandatory strategy Redis checkpoint

At preflight and immediately before strategy start, record `INFO stats` evicted_keys,
`INFO memory` used_memory and `HGETALL mai_tai:market-data-subscription-owners`.
Require memory <=1,600,000,000, `_migration_complete=1` and all five consumer fields:
`strategy-engine`, `schwab-1m-v2`, `orb`, `orb-schwab`, `momentum-paper`. Require valid
symbol lists and a valid stream checkpoint; record an empty list as a real owner.
These small metadata reads do not fetch any snapshot batches.

**Immediately after the strategy start**, require evicted_keys unchanged from its
pre-step value, every prior consumer still present, and `_migration_complete=1`.
Repeat each second until a healthy strategy-engine heartbeat produced after its
new start confirms initialization completed, up to 180 seconds; check once more
after it. Heartbeat reads are small events, XREVRANGE COUNT 25 maximum. Record the
new strategy warm-up log evidence. A mere active systemd state is insufficient:
warm-up runs asynchronously after startup and can read roughly 660-720 MB.

Any eviction increase, missing/malformed owner, vanished migration marker, memory
above the bound, unreadable evidence, or warm-up timeout => **UNKNOWN, STOP remaining
install actions and page**. Do not repair Redis or retry a service here. The running
gateway is still pre-#1084 and will not repair a lost full hash itself. Read and
record the six process identities again; any unintended PID change also stops.

## Loaded values and single preopen re-pin

Read `/proc/<new MainPID>/environ` directly, with a second MainPID read proving it
did not move during inspection. On both new v2 and new OMS, paste the three sizing
env values: 600, 300, 1000. Also construct Settings from only that process env with
`_env_file=None` (do not fall back to the disk env), to report loaded/default values.
New OMS /proc must explicitly contain NFQ1 true and 10000 ms; its effective
shared reactive age must remain 2000 ms. Both new OMS and v2 must explicitly
contain 600/300/1000. OMS must retain target 5.0, hard stop 8.0, floor=false, RETRY_ONE=true,
EOD_OCO_TRANSITION=true, OVERNIGHT_FLATTEN=true, ORB live=true and
oms_v2_cw_target_stay_enabled=true. Verify polygon_30s=false on new strategy.

Back up the three isolated flag files and install from the final approved Git blob:

```bash
for name in expected_flags_check.py expected_flags.json expected_numeric.json; do
  source="$REPO/ops/health/$name"
  target="/home/trader/restart_evidence/$name"
  if sudo test -e "$target"; then sudo cp -p "$target" "$RUN/$name.before"; fi
  sudo install -o root -g root -m 0644 "$source" "$target"
  test "$(sudo -u trader git -C "$REPO" show "$APPROVED_SHA:ops/health/$name" | sha256sum | awk '{print $1}')" = "$(sudo sha256sum "$target" | awk '{print $1}')"
  sudo sha256sum "$target" | sudo tee -a "$RUN/installed.sha256"
done
sudo "$REPO/.venv/bin/python" /home/trader/restart_evidence/expected_flags_check.py \
  --catalog /home/trader/restart_evidence/expected_flags.json \
  --numeric-catalog /home/trader/restart_evidence/expected_numeric.json \
  | sudo tee "$RUN/flags-and-numeric.txt"
```

Run the numeric-only audit too, and paste every value and its final call:

```bash
sudo "$REPO/.venv/bin/python" - <<'PY' | sudo tee "$RUN/numeric-only.txt"
import runpy
from pathlib import Path
ns = runpy.run_path('/home/trader/restart_evidence/expected_flags_check.py')
entries = ns['load_numeric_catalog'](Path('/home/trader/restart_evidence/expected_numeric.json'))
rc, lines = ns['audit'](entries)
print('\n'.join(lines))
raise SystemExit(rc)
PY
```

Require seven numeric readings to match: three sizing settings on two services
plus OMS mirror quote age 10000; `Final call: PASS; checked=7/7`. Both notionals
must be process-env sourced, and the separate /proc proof also requires explicit
max-shares and both NFQ settings. Capture FLAGGATE's own full denominator and
actual call. Do not claim readiness when either checker is UNKNOWN.

Make **one** backed-up change to `/home/trader/preopen.sh`: date 2026-10-05, final
application SHA, freshly verified OMS/strategy/v2 PIDs and starts. Retain the exact
orb, orb-schwab and gateway identities because they were not restarted. Keep the
existing snapshot, install-record classifications, watch checks and three-way
routing. Its flag checker automatically loads expected_numeric.json beside it.
Change the dated report output path to 20261005, preserve trader ownership/mode
0700, run `bash -n`, log the entire diff and SHA256. Run the standalone numeric
and flag checks during this install; do not alter the date to make Monday's full gate pass
on Saturday. Monday's 06:20 run as trader is to be scheduled only after the operator
accepts the completed install evidence.

```bash
sudo "$REPO/.venv/bin/python" - "$APPROVED_SHA" <<'PY'
import hashlib
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
p = Path('/home/trader/preopen.sh')
original = p.read_bytes()
if hashlib.sha256(original).hexdigest() != 'd58aa3510d16977ad245d98e05e0d0c4dff9fc6db41d58907d22f86e7e46cbd2':
    raise SystemExit('UNKNOWN preopen drift before single repin')
text = original.decode()
def old(name):
    found = re.findall(r'^' + name + r'=(.*)$', text, re.M)
    if len(found) != 1:
        raise SystemExit(f'UNKNOWN preopen key {name}')
    return shlex.split(found[0])[0]
changes = {'EXPECTED_DATE': '2026-10-05', 'EXPECTED_SHA': sys.argv[1],
           'REPORT': '/home/trader/known_defect_regression_watch/v2-restart-evidence-20261005.md'}
for service, pid_key, start_key, restarted in (
    ('oms', 'EXPECTED_OMS_PID', 'EXPECTED_OMS_START', True),
    ('strategy', 'EXPECTED_STRATEGY_PID', 'EXPECTED_STRATEGY_START', True),
    ('schwab-1m-v2', 'EXPECTED_PID', 'EXPECTED_START', True),
    ('orb', 'EXPECTED_ORB_PID', 'EXPECTED_ORB_START', False),
    ('orb-schwab', 'EXPECTED_ORB_SCHWAB_PID', 'EXPECTED_ORB_SCHWAB_START', False),
    ('market-data', 'EXPECTED_MARKET_DATA_PID', 'EXPECTED_MARKET_DATA_START', False),
):
    unit = f'project-mai-tai-{service}.service'
    output = subprocess.check_output(['systemctl', 'show', unit, '-p', 'MainPID', '-p',
        'ExecMainStartTimestamp', '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts'], text=True)
    values = dict(line.split('=', 1) for line in output.splitlines())
    pid, start = values['MainPID'], values['ExecMainStartTimestamp']
    if (int(pid) <= 0 or not start or values['ActiveState'] != 'active' or
            values['SubState'] != 'running' or values['NRestarts'] != '0'):
        raise SystemExit(f'UNKNOWN {service} identity {values}')
    if restarted:
        if pid == old(pid_key) or start == old(start_key):
            raise SystemExit(f'UNKNOWN {service} restart identity did not change')
        changes[pid_key], changes[start_key] = pid, start
    elif pid != old(pid_key) or start != old(start_key):
        raise SystemExit(f'UNKNOWN unintended {service} identity drift')
for key, value in changes.items():
    text, count = re.subn(r'^' + key + r'=.*$', key + '=' + shlex.quote(value), text, flags=re.M)
    if count != 1:
        raise SystemExit(f'UNKNOWN preopen replacement {key}')
text = text.replace('# Fail-closed pre-open gate for 2026-10-02.',
                    '# Fail-closed pre-open gate for 2026-10-05.', 1)
tmp = p.with_name('preopen.20261005.pending')
st = p.stat()
with tmp.open('x') as out:
    out.write(text)
subprocess.run(['bash', '-n', str(tmp)], check=True)
os.chown(tmp, st.st_uid, st.st_gid)
os.chmod(tmp, 0o700)
os.replace(tmp, p)
print('PREOPEN_REPIN date=2026-10-05 sha256=' + hashlib.sha256(p.read_bytes()).hexdigest())
PY
sudo diff -u "$RUN/preopen.before" /home/trader/preopen.sh | sudo tee "$RUN/preopen.diff" || test "${PIPESTATUS[0]}" = 1
sudo sha256sum /home/trader/preopen.sh | sudo tee "$RUN/preopen.sha256"
```

The old isolated restart checker and untimestamped gateway logs can still yield
UNKNOWN. #1077 merging does not make that proof green; report its actual call.

## Completion evidence and accepted limits

Report exact application/plan SHAs; new OMS/v2/strategy PIDs and start times;
unchanged gateway/orb/orb-schwab/paper identities; env before/after hashes and only
the five-key diff; loaded sizing values on both processes; Redis before/after
numbers and all owner fields; flag/numeric catalogs and checker hashes; numeric
7/7 lines; preopen backup/diff/hash; v2 preflight rc and any literal operator
override; both new-process v2 log checks and BOOT-HOLD/warm-up outcome or named
Monday verifier; installed detector inventory; all raw paths. Review the
separate `MONDAY_GUARD_START_2026-10-05.md` before authorizing its one-shot.

Case b, native OCO child activation/scaling after a partial parent, is accepted
**UNEXERCISED** by the operator. Unit partial-fill tests are not live exercise.
PARA1 is cancelled. Dollar sizing starts with the first eligible Monday session;
no artificial orders are submitted to prove it during this install. Installed INC1 watch
coverage, including oms_v2_cw_target_cancel_unconfirmed, is not expanded here.

Offline validation: **25 tests passed** in `test_install_job_2026_10_03.py`.
All 11 Bash blocks and the executable runner pass `bash -n`; their embedded
Python blocks parse. Tests cover absent/mismatched review, each modified artifact,
expired date, empty artifact manifest, the three intended new identities and all
five untouched identities, and the literal scoped service sequence. These are
offline proofs, not an executed installation or live readiness result.
The runner, timer, identity/rotation checks, and NFQ env
addition require review at this exact new head; the old plan approval alone
does not cover them. No production execution is claimed by local tests.

### Runner evidence and review boundary

The new executable companion declares the approved proof functions before using
them, holds `/run/lock/project-mai-tai-deploy.lock`, and records actual service
states on any abort. It takes the pinned October 3 09:44 ET identity census as
its preflight expectation (OMS 2386879, strategy 2387016, v2 1664453, ORB 1665228,
orb-schwab 2387072, gateway 2346625, paper 2477935, reconciler 1626620; starts
are literal in `install_evidence_2026-10-03.py`). Drift refuses execution.

The helper requires the October 3 logrotate status and rotated copy, and proves
the live gateway stdout inode is still the current log inode. It records the
v2 window settings from its process environment. No bulk snapshot read is used.
Before an env/checkout write, a flatness rc 1/2 may wait 300 seconds and reread
inside October 3; any such result after a service action aborts and pages.
The armed-segment restart gate never retries or obtains its own override.

The new OMS must publish a healthy `oms-risk` heartbeat after its actual new
start, within 180 seconds, before continuing. After the strategy start the
original Redis proof is mandatory. The final identity check requires all three
intended PIDs/starts to be new and the other five to be unchanged. Explicit
`/proc` checks, the full FLAGGATE, and numeric 7/7 precede the single preopen
re-pin. All evidence is appended to `deployments-20261003.md`; failed proofs are
recorded as stopped/incomplete, not INSTALL COMPLETE. An abort does not authorize
a repair or additional restart. The operator remains attended.

`approval.json` is not distributed with the job. After review, its exact content
is `release.json` plus `reviewer: claude-1` and `decision: APPROVED`, written as
root mode 0600 with exclusive creation. The release file names the final plan
commit, the fixed application SHA, and SHA256 of each artifact. The timer is
enabled while the receipt is absent so the operator can inspect it; unit starts
are skipped until review. No date is extended automatically, and a missed date
does not run at the next boot. Completion must be independently checked before
the Monday procedure is installed as a separate box timer.
