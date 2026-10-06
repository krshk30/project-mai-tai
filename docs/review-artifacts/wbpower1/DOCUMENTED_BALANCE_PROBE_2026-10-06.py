"""[codex] One authorized documented-route GET; no remote file or env writes."""
import ast
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import types
from urllib.parse import parse_qs, urlsplit

OFFICIAL_SOURCE = "# Copyright 2022 Webull\n#\n# Licensed under the Apache License, Version 2.0 (the \"License\");\n# you may not use this file except in compliance with the License.\n# You may obtain a copy of the License at\n#\n# \thttp://www.apache.org/licenses/LICENSE-2.0\n#\n# Unless required by applicable law or agreed to in writing, software\n# distributed under the License is distributed on an \"AS IS\" BASIS,\n# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.\n# See the License for the specific language governing permissions and\n# limitations under the License.\n\n# coding=utf-8\nfrom webull.core.request import ApiRequest\n\n\nclass AccountBalanceRequest(ApiRequest):\n    def __init__(self):\n        ApiRequest.__init__(self, \"/trading/assets/balances/get\", version='v3', method=\"GET\", query_params={})\n\n    def set_account_id(self, account_id):\n        self.add_query_param(\"account_id\", account_id)\n\n    def set_total_asset_currency(self, total_asset_currency=\"HKD\"):\n        self.add_query_param(\"total_asset_currency\", total_asset_currency)\n        "
BUDGET_SOURCE = "\"\"\"Bounded list traversal and restart-safe terminal query evidence.\n\nThe rolling ceiling is a conservative application policy. The legacy Webull\nendpoint's actual quota is unmeasured. All permits count HTTP attempts, not scans.\n\"\"\"\n\nfrom __future__ import annotations\n\nimport hashlib\nimport json\nimport threading\nimport time\nfrom collections import deque\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime\nfrom uuid import NAMESPACE_URL, uuid5\nfrom zoneinfo import ZoneInfo\n\nfrom sqlalchemy.exc import IntegrityError\n\nfrom project_mai_tai.db.models import DashboardSnapshot\n\n\nclass QueryBudgetUnavailable(RuntimeError):\n    \"\"\"No query permit now; callers retain UNKNOWN without blocking a deadline.\"\"\"\n\n\nclass QueryBudget:\n    def __init__(self, clock=time.monotonic):\n        self.clock = clock\n        self.lock = threading.Lock()\n        self.attempts: dict[str, deque[float]] = {}\n        self.waiting: dict[str, deque[str]] = {}\n        self.last_wait: dict[tuple[str, str], float] = {}\n        self.terminal_lock = threading.Lock()\n        self.terminal_inflight: set[tuple[str, str, str]] = set()\n\n    def claim(self, endpoint: str, owner: str, *, strict: bool = False) -> None:\n        with self.lock:\n            now = self.clock()\n            attempts = self.attempts.setdefault(endpoint, deque())\n            while attempts and now - attempts[0] >= 2.0:\n                attempts.popleft()\n            queue = self.waiting.setdefault(endpoint, deque())\n            # A removed order/account must not leave an immortal queue head.\n            for queued in list(queue):\n                if now - self.last_wait.get((endpoint, queued), now) >= 30.0:\n                    queue.remove(queued)\n                    self.last_wait.pop((endpoint, queued), None)\n            if owner not in queue:\n                queue.append(owner)\n            self.last_wait[endpoint, owner] = now\n            # Reserve the second detail permit for fresh RPG/EOD/cancel proof.\n            ceiling = 1 if endpoint == \"detail\" and not strict else 2\n            if len(attempts) >= ceiling or (not strict and queue[0] != owner):\n                raise QueryBudgetUnavailable(f\"{endpoint} query budget unavailable\")\n            queue.remove(owner)\n            self.last_wait.pop((endpoint, owner), None)\n            attempts.append(now)\n\n\n_BUDGETS: dict[tuple[str, str], QueryBudget] = {}\n_BUDGET_LOCK = threading.Lock()\n\n\ndef shared_budget(host: str, app_key: str) -> QueryBudget:\n    # Share across accounts, aliases, threads, and adapter instances in this process.\n    with _BUDGET_LOCK:\n        return _BUDGETS.setdefault((host, app_key), QueryBudget())\n\n\n_READERS: dict[tuple[str, str], TodayOrderReader] = {}\n\n\ndef shared_reader(host: str, app_key: str, cycle_seconds: float):\n    budget = shared_budget(host, app_key)\n    with _BUDGET_LOCK:\n        return _READERS.setdefault((host, app_key), TodayOrderReader(budget, cycle_seconds))\n\n\n@dataclass\nclass ListScan:\n    session: str\n    cursor: str = \"\"\n    pages: int = 0\n    complete: bool = False\n    failed: bool = False\n    cycle_at: float = -float(\"inf\")\n    cycle_pages: int = 0\n    rows: dict[str, tuple[float, datetime, dict]] = field(default_factory=dict)\n    cursors: set[str] = field(default_factory=set)\n\n\nclass TodayOrderReader:\n    def __init__(self, budget: QueryBudget, cycle_seconds: float, clock=time.monotonic):\n        self.budget = budget\n        self.cycle_seconds = max(2.0, float(cycle_seconds))\n        self.clock = clock\n        self.lock = threading.Lock()\n        self.scans: dict[str, ListScan] = {}\n\n    def read(self, account_id: str, client_id: str, fetch_page, *, fresh_seconds=2.0):\n        with self.lock:\n            now = self.clock()\n            session = datetime.now(UTC).astimezone(ZoneInfo(\"America/New_York\")).date().isoformat()\n            scan = self.scans.get(account_id)\n            if scan is None or scan.session != session:\n                scan = self.scans[account_id] = ListScan(session)\n            row = scan.rows.get(client_id)\n            if row is not None and 0 <= now - row[0] < fresh_seconds:\n                return self._evidence(account_id, client_id, scan, row)\n            if now - scan.cycle_at >= self.cycle_seconds:\n                scan.cycle_at, scan.cycle_pages = now, 0\n                if scan.complete or scan.failed:\n                    scan = self.scans[account_id] = ListScan(session, cycle_at=now)\n            while scan.cycle_pages < 2 and not (scan.complete or scan.failed):\n                try:\n                    self.budget.claim(\"list-today\", account_id)\n                except QueryBudgetUnavailable:\n                    break\n                scan.cycle_pages += 1\n                try:\n                    status, body = fetch_page(scan.cursor)\n                    acquired = self.clock()\n                    acquired_at = datetime.now(UTC)\n                    if status != 200 or not isinstance(body, dict) or body.get(\"error_code\"):\n                        raise ValueError(\"unreadable today-orders page\")\n                    rows = body.get(\"orders\")\n                    if not isinstance(rows, list) or len(rows) > 100:\n                        raise ValueError(\"invalid today-orders rows\")\n                    has_next = body.get(\"has_next\", body.get(\"hasNext\", False))\n                    if (\"has_next\" not in body and \"hasNext\" not in body) or not isinstance(\n                        has_next, bool\n                    ):\n                        raise ValueError(\"invalid today-orders continuation\")\n                    page = {}\n                    for raw in rows:\n                        if not isinstance(raw, dict):\n                            raise ValueError(\"invalid today-orders row\")\n                        coid = str(raw.get(\"client_order_id\") or raw.get(\"clientOrderId\") or \"\")\n                        if not coid or any(\n                            str(raw[k]) != account_id\n                            for k in (\"account_id\", \"accountId\")\n                            if raw.get(k) is not None\n                        ):\n                            raise ValueError(\"unbound today-orders row\")\n                        old = page.get(coid) or (scan.rows.get(coid) or (None, None, None))[2]\n                        if old is not None and old != raw:\n                            raise ValueError(\"conflicting today-orders identity\")\n                        page[coid] = raw\n                    cursor = (\n                        str(rows[-1].get(\"client_order_id\") or rows[-1].get(\"clientOrderId\") or \"\")\n                        if rows\n                        else \"\"\n                    )\n                    if has_next and (not cursor or cursor == scan.cursor or cursor in scan.cursors):\n                        raise ValueError(\"today-orders cursor did not progress\")\n                    scan.pages += 1\n                    scan.rows.update(\n                        {coid: (acquired, acquired_at, raw) for coid, raw in page.items()}\n                    )\n                    scan.complete = not has_next\n                    if has_next:\n                        scan.cursor = cursor\n                        scan.cursors.add(cursor)\n                        if scan.pages >= 20:\n                            scan.failed = True\n                    row = scan.rows.get(client_id)\n                    if row is not None and 0 <= self.clock() - row[0] < fresh_seconds:\n                        return self._evidence(account_id, client_id, scan, row)\n                except Exception:\n                    # Keep no affirmative absence and never stamp old pages as freshly read.\n                    scan.failed = True\n                    raise\n            return None\n\n    @staticmethod\n    def _evidence(account_id, client_id, scan, row):\n        body = dict(row[2])\n        body[\"_webull_list_evidence\"] = {\n            \"account_id\": account_id,\n            \"client_order_id\": client_id,\n            \"session\": scan.session,\n            \"acquired_at\": row[1].isoformat(),\n            \"pages\": scan.pages,\n            \"complete\": scan.complete,\n            \"cursor\": scan.cursor,\n            \"truncated\": scan.failed,\n        }\n        return body\n\n\ndef terminal_version(account_id: str, client_id: str, body: dict) -> str:\n    identity = {k: v for k, v in body.items() if not k.startswith(\"_webull_\")}\n    encoded = json.dumps([account_id, client_id, identity], sort_keys=True, separators=(\",\", \":\"))\n    return hashlib.sha256(encoded.encode()).hexdigest()\n\n\nclass TerminalProofStore:\n    \"\"\"An immutable proof per evidence version; primary key makes commit retry idempotent.\"\"\"\n\n    def __init__(self, session_factory):\n        self.session_factory = session_factory\n\n    @staticmethod\n    def identity(account_id, client_id, version):\n        return uuid5(NAMESPACE_URL, f\"webull-terminal:{account_id}:{client_id}:{version}\")\n\n    def load(self, account_id, client_id, version):\n        with self.session_factory() as session:\n            row = session.get(DashboardSnapshot, self.identity(account_id, client_id, version))\n            if row is None or row.snapshot_type != \"webull_terminal_read_proof\":\n                return None\n            payload = row.payload\n            if (\n                payload.get(\"account_id\"),\n                payload.get(\"client_order_id\"),\n                payload.get(\"version\"),\n            ) != (account_id, client_id, version):\n                return None\n            return payload\n\n    def save(self, account_id, client_id, version, proof):\n        identity = self.identity(account_id, client_id, version)\n        with self.session_factory() as session:\n            session.add(\n                DashboardSnapshot(\n                    id=identity, snapshot_type=\"webull_terminal_read_proof\", payload=proof\n                )\n            )\n            try:\n                session.commit()\n            except IntegrityError:\n                session.rollback()\n                existing = self.load(account_id, client_id, version)\n                # Acquired times and numeric formatting may differ across a crash\n                # retry; the normalized execution and scope must remain identical.\n                if (\n                    existing is None\n                    or existing.get(\"execution\") != proof.get(\"execution\")\n                    or existing.get(\"source\") != proof.get(\"source\")\n                ):\n                    raise\n"
SDK_ROOT = Path('/home/trader/project-mai-tai/.venv/lib/python3.12/site-packages')
EXPECTED_SDK = {
    'webull/core/client.py': 'cf148188823d283c5ce6d1e94deac97d5fcf28fb7c13d7b7a133a989a900b49c',
    'webull/core/request.py': 'cbc97f1b059d44b3ebf31fb4426ef150d83600443c83aeef18965209b4db3101',
    'webull/core/http/response.py': '882b924f71271d4d8dc04bef564d018e1ff471f355d0fc7b20c73c75cdc803fd',
    'webull/core/retry/retry_policy.py': 'cb52b073b970d4de2c82f47bd407146ecbfc71860b00945d0ccc7b5563bab9bb',
    'webull/core/auth/signers/app_key_signer.py': 'a23746df215e7d4ee896a9d06d11250ae25ff550fb52ab9e144dfdf883ff05f9',
    'webull/core/auth/composer/default_signature_composer.py': '410adb76c097e7430718932132720ecbfa31d0b07b41852c0de2403dce974815',
}
logging.disable(logging.CRITICAL)
sys.dont_write_bytecode = True

def deny_writes(event, args):
    if event == 'open':
        mode, flags = args[1], args[2]
        if isinstance(mode, str) and any(c in mode for c in 'wax+'):
            raise RuntimeError('filesystem_write_denied')
        if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            raise RuntimeError('filesystem_write_denied')
    if event in {'os.mkdir', 'os.remove', 'os.rename', 'os.rmdir'}:
        raise RuntimeError('filesystem_mutation_denied')

sys.addaudithook(deny_writes)
record = {
    'agent': 'codex', 'kind': 'WBPOWER1_documented_balance_route_GET',
    'asof_start_utc': datetime.now(UTC).isoformat(),
    'method': 'GET', 'endpoint': '/trading/assets/balances/get', 'version': 'v3',
    'account_alias': 'live:orb', 'requested_total_asset_currency': 'USD',
    'official_sdk_commit': 'abe5668ce4bc11dd1e8a1aad944d9dbb239bf12d',
    'official_request_path': 'webull/trade/request/v2/get_account_balance_request.py',
    'official_request_sha256': hashlib.sha256(OFFICIAL_SOURCE.encode()).hexdigest(),
    'budget_head': '78ee3503783d59267423e2e7abc21c10cc68ba85',
    'budget_module': 'project_mai_tai.broker_adapters.webull_order_reads',
    'budget_source_sha256': hashlib.sha256(BUDGET_SOURCE.encode()).hexdigest(),
    'budget_scope': 'Reviewed shared registry definitions in this separate SSH probe process; not distributed production enforcement',
    'permit_endpoint': 'account-balance', 'strict': False,
    'refresh_enabled': False, 'retry_enabled': False, 'redirects_enabled': False,
    'filesystem_writes_denied': True, 'environment_writes': False,
    'http_attempts': 0, 'sdk_single_attempts': 0,
}
try:
    for relative, expected in EXPECTED_SDK.items():
        data = (SDK_ROOT / relative).read_bytes()
        if len(data) > 64 * 1024 or hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError('reviewed_SDK_source_changed')
    record['reviewed_installed_source_sha256'] = EXPECTED_SDK
    record['installed_sdk_version'] = importlib.metadata.version('webull-openapi-python-sdk')
    pid = subprocess.check_output(
        ['systemctl', 'show', '--property=MainPID', '--value', 'project-mai-tai-oms.service'],
        text=True, timeout=3).strip()
    if not pid.isdigit() or pid == '0':
        raise RuntimeError('OMS_process_unavailable')
    raw = Path('/proc/' + pid + '/environ').read_bytes()
    if len(raw) > 128 * 1024:
        raise RuntimeError('environment_read_bound')
    env = dict(field.decode().split('=', 1) for field in raw.split(b'\0') if b'=' in field)
    app_key = env.get('MAI_TAI_WEBULL_APP_KEY', '').strip()
    app_secret = env.get('MAI_TAI_WEBULL_APP_SECRET', '').strip()
    account_id = env.get('MAI_TAI_WEBULL_ACCOUNT_ID', '').strip()
    configured_host = env.get('MAI_TAI_WEBULL_BASE_URL', 'https://api.webull.com').strip()
    host = configured_host.split('://')[-1].split('/')[0]
    region = env.get('MAI_TAI_WEBULL_REGION_ID', 'us').strip()
    if not app_key or not app_secret or not account_id or host != 'api.webull.com' or region != 'us':
        raise RuntimeError('existing_exact_US_config_unavailable')

    tree = ast.parse(BUDGET_SOURCE)
    names = {'QueryBudgetUnavailable', 'QueryBudget', 'shared_budget'}
    nodes = [n for n in tree.body if
        isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names or
        isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '_BUDGET_LOCK' for t in n.targets) or
        isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == '_BUDGETS']
    budget_module = types.ModuleType(record['budget_module'])
    budget_module.__dict__.update(time=time, threading=threading, deque=deque)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<reviewed shared budget definitions>', 'exec'),
         budget_module.__dict__)
    sys.modules[budget_module.__name__] = budget_module
    budget = budget_module.shared_budget(host, app_key)
    assert budget is budget_module.shared_budget(host, app_key)
    record['same_process_registry_identity_verified'] = True

    from webull.core.client import ApiClient
    from webull.core.retry.retry_policy import NO_RETRY_POLICY
    from requests import Session
    official = {}
    exec(compile(OFFICIAL_SOURCE, '<pinned official SDK balance request>', 'exec'), official)
    request = official['AccountBalanceRequest']()
    request.set_account_id(account_id)
    request.set_total_asset_currency('USD')
    assert request.get_method() == 'GET'
    assert request.get_action_name() == record['endpoint'] and request.get_version() == 'v3'
    assert request.get_query_params() == {'account_id': account_id, 'total_asset_currency': 'USD'}
    original_send = Session.send

    def guarded_send(session, prepared, **kwargs):
        url = urlsplit(prepared.url)
        query = parse_qs(url.query, strict_parsing=True)
        if (record['http_attempts'] or prepared.method != 'GET' or url.scheme != 'https' or
            url.hostname != host or url.path != record['endpoint'] or
            query != {'account_id': [account_id], 'total_asset_currency': ['USD']} or
            kwargs.get('allow_redirects') is not False):
            raise RuntimeError('not_the_one_authorized_GET')
        retries = session.get_adapter(prepared.url).max_retries
        if retries.total != 0 or retries.connect not in (None, 0, False) or retries.read not in (None, 0, False):
            raise RuntimeError('transport_retry_enabled')
        body = prepared.body or b''
        body = body.encode() if isinstance(body, str) else body
        if body:
            raise RuntimeError('unexpected_GET_body')
        serialized = (prepared.method + ' ' + prepared.path_url + ' HTTP/1.1\r\n' +
            ''.join(str(k) + ': ' + str(v) + '\r\n' for k, v in prepared.headers.items()) +
            '\r\n').encode() + body
        record['request_body_bytes'] = len(body)
        record['request_body_sha256'] = hashlib.sha256(body).hexdigest()
        record['prepared_request_serialization_bytes'] = len(serialized)
        record['prepared_request_serialization_sha256'] = hashlib.sha256(serialized).hexdigest()
        record['request_bytes_scope'] = 'Prepared HTTP/1.1 serialization before transport-added headers/TLS; raw headers and query never persisted'
        record['account_id_query_matches_configured'] = True
        record['signature_algorithm'] = prepared.headers.get('x-signature-algorithm')
        record['x_version_at_send'] = prepared.headers.get('x-version')
        record['access_token_header_present'] = bool(prepared.headers.get('x-access-token'))
        record['transport_retries_total'] = retries.total
        budget.claim('account-balance', account_id, strict=False)
        record['http_attempts'] += 1
        record['http_start_utc'] = datetime.now(UTC).isoformat()
        response = original_send(session, prepared, **kwargs)
        record['http_end_utc'] = datetime.now(UTC).isoformat()
        record['http_status'] = response.status_code
        record['response_body_bytes'] = len(response.content)
        record['response_body_sha256'] = hashlib.sha256(response.content).hexdigest()
        record['response_bytes_scope'] = 'Actual decoded response.content bytes; raw response not persisted'
        if len(response.content) > 64 * 1024:
            raise RuntimeError('response_read_bound')
        try:
            payload = response.json()
        except ValueError:
            record['response_JSON_valid'] = False
            return response
        record['response_JSON_valid'] = True
        record['response_account_identity'] = 'absent'
        if isinstance(payload, dict):
            ids = [payload[k] for k in ('account_id', 'accountId') if k in payload]
            if ids:
                record['response_account_identity'] = 'matches_configured' if all(str(x) == account_id for x in ids) else 'mismatch'
            code = payload.get('error_code')
            if isinstance(code, str) and len(code) <= 80 and all(c.isupper() or c.isdigit() or c in '_-' for c in code):
                record['broker_error_code'] = code
            record['response_total_asset_currency'] = payload.get('total_asset_currency') if payload.get('total_asset_currency') in ('USD','HKD') else 'absent_or_other'
            rows = payload.get('account_currency_assets')
            selected = []
            if isinstance(rows, list):
                record['currency_asset_rows'] = len(rows)
                for index, row in enumerate(rows):
                    if not isinstance(row, dict) or row.get('currency') != 'USD':
                        continue
                    scoped = {'path': '$.account_currency_assets[' + str(index) + ']', 'currency': 'USD', 'fields': {}}
                    for key in ('day_buying_power', 'used_margin_for_open_order', 'margin_power', 'cash_power', 'buying_power'):
                        if key in row:
                            value = row[key]
                            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                                try:
                                    number = Decimal(str(value))
                                    if number.is_finite():
                                        scoped['fields'][key] = {'raw': str(value), 'normalized': str(number)}
                                    else:
                                        scoped['fields'][key] = {'invalid': 'nonfinite'}
                                except (InvalidOperation, ValueError):
                                    scoped['fields'][key] = {'invalid': 'not_decimal'}
                            else:
                                scoped['fields'][key] = {'invalid': 'type'}
                    selected.append(scoped)
            record['selected_USD_power_scope'] = selected
            record['documented_USD_day_power_present'] = any('day_buying_power' in row['fields'] for row in selected)
        return response

    class OneAttemptClient(ApiClient):
        def _handle_single_request(self, *args, **kwargs):
            if record['sdk_single_attempts']:
                raise RuntimeError('second_SDK_attempt_denied')
            record['sdk_single_attempts'] += 1
            return super()._handle_single_request(*args, **kwargs)

    Session.send = guarded_send
    client = OneAttemptClient(app_key, app_secret, region, auto_retry=False, max_retry_num=0,
                              connect_timeout=2, timeout=2)
    client.add_endpoint(region, host)
    assert client._retry_policy is NO_RETRY_POLICY
    record['SDK_NO_RETRY_POLICY_identity_verified'] = True
    def expired(_signum, _frame):
        raise TimeoutError('bounded_GET_timeout')
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 5)
    client.get_response(request)
    record['outcome'] = 'response_returned'
except Exception as exc:
    record['outcome'] = 'broker_refused' if record.get('http_status', 0) >= 400 else 'UNMEASURED_or_preflight_stop'
    record['error_type'] = type(exc).__name__
    if str(exc) in {'reviewed_SDK_source_changed', 'OMS_process_unavailable', 'environment_read_bound',
                    'existing_exact_US_config_unavailable', 'filesystem_write_denied',
                    'filesystem_mutation_denied'}:
        record['preflight_stop_reason'] = str(exc)
finally:
    signal.setitimer(signal.ITIMER_REAL, 0)
record['asof_end_utc'] = datetime.now(UTC).isoformat()
record['reservation_subtraction_semantics'] = 'UNMEASURED; no subtraction, alias fallback or placement permission inferred'
print(json.dumps(record, sort_keys=True), flush=True)
