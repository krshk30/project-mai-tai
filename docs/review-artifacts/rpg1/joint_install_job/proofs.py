"""Read-only, bounded host evidence. CLI capture emits only the host JSON object.

Redis has no write method here. Database connections enforce read-only transactions.
Broker discovery is explicitly incomplete; only GET requests are made, with adapter
refresh disabled. Application idle probes use a recording fake, not real Redis.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
from datetime import timedelta
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import sys
from urllib.parse import quote, unquote, urlsplit

from policy import *
from review_gate import FILE_PATHS, STATUS_CONTRACT, UNIT_NAMES

MAX_REPLY = 1_000_000
IDENTITY_FIELDS = ('MainPID', 'ExecMainStartTimestamp', 'ActiveState', 'SubState', 'NRestarts',
                   'InvocationID', 'ExecMainCode', 'ExecMainStatus', 'Result', 'KillSignal')
STREAMS = ('heartbeats', 'market-data', 'market-data-subscriptions', 'order-events',
           'runtime-controls', 'snapshot-batches', 'strategy-intents', 'strategy-state',
           'strategy-state-isolated')


def command(*args, timeout=30, limit=4_000_000):
    # timeout bounds subprocess lifetime; outputs are small administrative contracts.
    result = subprocess.run(args, capture_output=True, timeout=timeout, env={**os.environ, 'TZ': 'UTC'})
    need(len(result.stdout) + len(result.stderr) <= limit, 'command output oversized: ' + str(args[0]))
    need(result.returncode == 0, f'command rc={result.returncode}: {args[0]}: '
         + result.stderr.decode(errors='replace')[-2000:])
    return result.stdout.decode().strip()


def identity(name):
    args = ['systemctl', 'show', unit(name)]
    for field in IDENTITY_FIELDS:
        args.extend(('-p', field))
    row = dict(line.split('=', 1) for line in command(*args).splitlines())
    need(set(row) == set(IDENTITY_FIELDS), 'incomplete systemd identity: ' + name)
    return row


def settings():
    from project_mai_tai.settings import Settings
    config = Settings(_env_file=str(ENV))
    need(not config.schwab_adapter_token_refresh_enabled, 'original adapter refresh enabled: control must remain sole writer')
    return config


class RESP:
    """Bound lengths before allocation; never use an eager generic Redis decoder."""
    def __init__(self, stream, limit):
        self.stream, self.remaining = stream, limit

    def take(self, length):
        need(0 <= length <= self.remaining, 'Redis response exceeds byte budget')
        self.remaining -= length
        value = self.stream.read(length)
        need(len(value) == length, 'truncated Redis response')
        return value

    def line(self):
        value = self.stream.readline(min(self.remaining + 1, 4097))
        need(value.endswith(b'\r\n') and len(value) <= self.remaining and len(value) <= 4096,
             'oversized/malformed Redis header')
        self.remaining -= len(value)
        return value[:-2]

    def read(self, depth=0):
        need(depth <= 8, 'Redis nesting too deep')
        kind, raw = self.take(1), self.line()
        if kind == b'+':
            return raw.decode()
        if kind == b'-':
            raise Refusal('Redis error: ' + raw.decode()[:200])
        if kind == b':':
            return int(raw)
        if kind == b'$':
            length = int(raw)
            if length == -1:
                return None
            value = self.take(length + 2)
            need(value.endswith(b'\r\n'), 'malformed bulk response')
            return value[:-2].decode()
        if kind == b'*':
            count = int(raw)
            if count == -1:
                return None
            need(0 <= count <= min(8000, self.remaining // 3), 'oversized Redis array')
            return [self.read(depth + 1) for _ in range(count)]
        raise Refusal('unsupported RESP type')


class RedisRead:
    def __init__(self, config):
        self.url = urlsplit(config.redis_url)
        self.prefix = config.redis_stream_prefix
        need(self.url.scheme in {'redis', 'rediss'}, 'unsupported Redis URL')

    def call(self, *args, limit=MAX_REPLY):
        verb = str(args[0]).upper()
        need(verb in {'INFO', 'CONFIG', 'TYPE', 'XLEN', 'HLEN', 'HSTRLEN', 'HGETALL',
                      'XRANGE', 'XREVRANGE'}, 'Redis writes/unknown commands prohibited')
        if verb == 'INFO':
            need(args[1:] in [('memory',), ('stats',)], 'INFO scope')
            limit = min(limit, 100_000)
        elif verb == 'CONFIG':
            need(args[1:] == ('GET', 'save', 'appendonly', 'maxmemory', 'maxmemory-policy'), 'CONFIG scope')
        else:
            key = str(args[1])
            suffix = key.removeprefix(self.prefix + ':')
            need(key.startswith(self.prefix + ':') and suffix in STREAMS + ('market-data-subscription-owners',),
                 'unapproved Redis key')
            need(suffix != 'snapshot-batches' or verb in {'TYPE', 'XLEN'}, 'snapshot payload read prohibited')
            if verb in {'XRANGE', 'XREVRANGE'}:
                need(args[-2] == 'COUNT' and (args[-1] == 1 or
                     (args[-1] == 25 and suffix == 'heartbeats' and verb == 'XREVRANGE')), 'stream read bound')
        def exchange(sockfile, command_args, bound):
            chunks = [str(a).encode() for a in command_args]
            wire = b'*' + str(len(chunks)).encode() + b'\r\n' + b''.join(
                b'$' + str(len(c)).encode() + b'\r\n' + c + b'\r\n' for c in chunks)
            sockfile.write(wire)
            sockfile.flush()
            return RESP(sockfile, bound).read()
        with socket.create_connection((self.url.hostname, self.url.port or 6379), timeout=5) as raw:
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.url.hostname) if self.url.scheme == 'rediss' else raw
            with sock.makefile('rwb') as connection:
                if self.url.password:
                    auth = ('AUTH', unquote(self.url.username), unquote(self.url.password)) if self.url.username else ('AUTH', unquote(self.url.password))
                    need(exchange(connection, auth, 1000) == 'OK', 'Redis authentication')
                db = self.url.path.strip('/') or '0'
                need(db.isdigit(), 'Redis database malformed')
                need(exchange(connection, ('SELECT', db), 1000) == 'OK', 'Redis database selection')
                return exchange(connection, args, limit)

    def rows(self, suffix, *, reverse=False, cursor='-', count=1):
        key = self.prefix + ':' + suffix
        data = self.call('XREVRANGE', key, '+', '-', 'COUNT', count) if reverse else self.call(
            'XRANGE', key, cursor, '+', 'COUNT', count)
        need(isinstance(data, list), 'stream response malformed')
        result = []
        for entry in data:
            need(isinstance(entry, list) and len(entry) == 2 and re.fullmatch(r'\d+-\d+', entry[0]), 'stream ID malformed')
            fields = entry[1]
            need(isinstance(fields, list) and len(fields) % 2 == 0, 'stream fields malformed')
            record = dict(zip(fields[::2], fields[1::2]))
            need(len(record) * 2 == len(fields) and set(record) == {'data'}, 'stream envelope fields unknown')
            result.append((entry[0], json.loads(record['data'])))
        return result


def redis_state(reader, *, evicted=None):
    info = {}
    for section in ('memory', 'stats'):
        raw = reader.call('INFO', section)
        need(isinstance(raw, str), 'INFO malformed')
        info.update(dict(line.split(':', 1) for line in raw.splitlines() if line and not line.startswith('#')))
    memory, count = int(info['used_memory']), int(info['evicted_keys'])
    need(memory <= 1_600_000_000, 'Redis transient memory margin crossed')
    need(evicted is None or count == evicted, 'Redis eviction count changed')
    raw = reader.call('CONFIG', 'GET', 'save', 'appendonly', 'maxmemory', 'maxmemory-policy')
    config = dict(zip(raw[::2], raw[1::2]))
    need(config == {'save': '', 'appendonly': 'no', 'maxmemory': '2147483648', 'maxmemory-policy': 'allkeys-lru'},
         'Redis persistence or memory policy changed; no repair allowed')
    key = reader.prefix + ':market-data-subscription-owners'
    names = sorted(OWNERS | {'_migration_complete', '_last_applied_id'})
    need(reader.call('HLEN', key) == 7, 'owner fields incomplete/unknown')
    lengths = {name: reader.call('HSTRLEN', key, name) for name in names}
    need(all(type(n) is int and n > 0 for n in lengths.values()) and sum(lengths.values()) < 900_000,
         'owner value bounds/fields invalid')
    raw = reader.call('HGETALL', key)
    owners = dict(zip(raw[::2], raw[1::2]))
    need(len(raw) == 14 and set(owners) == set(names) and owners['_migration_complete'] == '1', 'owner race/malformed')
    need(re.fullmatch(r'\d+-\d+', owners['_last_applied_id']), 'owner checkpoint absent')
    sets = {name: symbols(json.loads(owners[name])) for name in OWNERS}
    need(len(sets['momentum-paper']) <= 16 and sets['momentum-paper'] == [], 'paper weekend owner not empty')
    streams = {name: reader.call('TYPE', reader.prefix + ':' + name) for name in STREAMS}
    need(all(t in {'stream', 'none'} for t in streams.values()), 'stream type changed')
    return {'used_memory': memory, 'evicted_keys': count, 'sets': sets,
            'checkpoint': owners['_last_applied_id'], 'streams': streams, 'config': config}


def readonly_engine(config):
    from sqlalchemy import event
    from project_mai_tai.db.session import build_engine
    engine = build_engine(config.database_url, connect_timeout_s=5, statement_timeout_ms=5000)
    @event.listens_for(engine, 'begin')
    def readonly(connection):
        connection.exec_driver_sql('SET TRANSACTION READ ONLY')
    @event.listens_for(engine, 'before_cursor_execute')
    def no_write(connection, cursor, statement, params, context, many):
        need(statement.lstrip().split()[0].upper() in {'SELECT', 'SHOW', 'SET'}, 'probe attempted DB mutation')
    return engine


def durable(config):
    from sqlalchemy import text
    engine = readonly_engine(config)
    result = {}
    queries = {
        'broker_orders': 'SELECT lower(status), count(*) FROM broker_orders GROUP BY lower(status)',
        'trade_intents': 'SELECT lower(status), count(*) FROM trade_intents GROUP BY lower(status)',
        'nfq': "SELECT payload->>'phase', count(*) FROM dashboard_snapshots WHERE snapshot_type='oms_webull_mirror_price_hold' GROUP BY payload->>'phase'",
        'rpg': "SELECT payload->>'phase', count(*) FROM dashboard_snapshots WHERE snapshot_type='atr_reprice_handoff' GROUP BY payload->>'phase'",
    }
    try:
        with engine.connect() as conn:
            for label, query in queries.items():
                rows = conn.execute(text(query)).fetchmany(101)
                need(len(rows) <= 100, 'status inventory oversized')
                values = {status: int(count) for status, count in rows}
                need(set(values) <= set(STATUS_CONTRACT[label]), 'unresolved/unknown ' + label + ': ' + repr(values))
                result[label] = values
            count = conn.execute(text('SELECT count(*) FROM v2_confirmation_exit_evaluations WHERE should_exit AND published_at IS NULL')).scalar_one()
            need(count == 0, 'pending confirmation outbox')
            result['pending_confirmation'] = count
    finally:
        engine.dispose()
    return result


def parse_flat(output):
    # rc0 in the historical helper can mean an explicit manual-position exception.
    # This job has no such exception, and checks BOTH broker counts independently.
    for field in ('schwab_holding_rows', 'webull_holding_rows'):
        values = re.findall(r'\b' + field + r'=(\d+)\b', output)
        need(values and all(x == '0' for x in values), 'strict-flat ' + field + ' is not zero')
    for field, expected in (('open_managed', {'live:schwab_1m_v2': 0, 'live:orb': 0}),
                            ('nonzero_virtual', []), ('net_bot_fills', [])):
        pattern = r'\b' + field + r'=(\{[^\n]*?\}|\[[^\n]*?\])'
        found = re.findall(pattern, output)
        need(found and all(ast.literal_eval(x) == expected for x in found), 'strict-flat ' + field)
    return {'both_broker_holdings': 0, 'managed': 0, 'virtual': 0, 'net_bot_fills': 0,
            'manual_override': False}


def token(config):
    path = Path(config.schwab_token_store_path or '')
    need(path.is_file() and path.stat().st_size <= 65536, 'token store absent/oversized')
    payload = json.loads(path.read_text())
    expiry = stamp(payload['expires_at'])
    need(expiry > now(), 'access token expired; control is the only writer')
    need(config.schwab_token_refresher_enabled and not config.schwab_adapter_token_refresh_enabled,
         'token single-writer setting changed')
    return {'expires_at': expiry.isoformat(), 'margin_seconds': config.schwab_token_refresh_margin_seconds,
            'interval_seconds': config.schwab_token_refresher_check_interval_seconds}


async def broker_orders(config):
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
    from webull.trade.request.get_open_orders_request import OpenOrdersListRequest
    schwab = SchwabBrokerAdapter(config)
    account = schwab.accounts_by_name['live:schwab_1m_v2']
    current = now()
    frm = (current - timedelta(hours=12)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    to = (current + timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')
    query = f'fromEnteredTime={frm}&toEnteredTime={to}&maxResults=500'
    request = '/trader/v1/accounts/' + quote(account.account_hash, safe='') + '/orders?' + query
    status, headers, body = await asyncio.wait_for(schwab._authorized_request_json('GET', request), 20)
    need(200 <= status < 300 and isinstance(body, list) and len(body) < 500, 'Schwab discovery failed/truncated')
    need(len(canonical(body)) <= MAX_REPLY, 'Schwab response too large')
    observed = []
    terminal = {'FILLED', 'CANCELED', 'CANCELLED', 'REJECTED', 'EXPIRED', 'REPLACED'}
    def walk(row, depth=0):
        need(depth <= 5 and isinstance(row, dict) and isinstance(row.get('status'), str), 'Schwab order unreadable')
        if row['status'].upper() not in terminal:
            need(row.get('orderId'), 'Schwab unexplained order lacks identity')
            observed.append({'broker': 'schwab', 'id': str(row['orderId']), 'status': row['status'].upper()})
        children = row.get('childOrderStrategies', [])
        need(isinstance(children, list) and len(children) <= 100, 'Schwab children malformed')
        for child in children:
            walk(child, depth + 1)
    for row in body:
        walk(row)
    webull = WebullBrokerAdapter(config)
    cfg = webull.accounts_by_name['live:orb']
    request_w = OpenOrdersListRequest()
    request_w.set_account_id(cfg.account_id)
    request_w.set_page_size(100)
    response = await asyncio.wait_for(asyncio.to_thread(webull._get_client().get_response, request_w), 20)
    need(200 <= int(getattr(response, 'status_code', 0)) < 300, 'Webull discovery HTTP error')
    orders = webull._body(response)
    need(isinstance(orders, dict) and isinstance(orders.get('orders'), list), 'Webull order body malformed')
    more = orders.get('has_next', orders.get('hasNext', False))
    need(type(more) is bool and not more and len(orders['orders']) < 100, 'Webull discovery truncated')
    need(len(canonical(orders)) <= MAX_REPLY, 'Webull response too large')
    for row in orders['orders']:
        need(isinstance(row, dict), 'Webull order unreadable')
        oid = row.get('order_id', row.get('orderId', row.get('client_order_id', row.get('clientOrderId'))))
        state = row.get('status', row.get('order_status'))
        need(oid and isinstance(state, str) and state, 'Webull order identity/status unreadable')
        observed.append({'broker': 'webull', 'id': str(oid), 'status': state.upper()})
    observed.sort(key=lambda x: (x['broker'], x['id'], x['status']))
    return {'scope': 'INCOMPLETE broker enumeration; bounded discovery only', 'observed': observed,
            'schwab': {'request': request, 'http': status, 'rows': len(body), 'bytes': len(canonical(body))},
            'webull': {'request': '/trade/orders/list-open', 'page_size': 100, 'has_next': more,
                       'rows': len(orders['orders']), 'bytes': len(canonical(orders))}}


class RecordOnlyRedis:
    """Explicit simulator: accepts only the paper weekend empty startup replace."""
    def __init__(self):
        self.calls = []

    async def xadd(self, key, fields, **kwargs):
        event = json.loads(fields['data'])
        need(key.endswith(':market-data-subscriptions') and event['source_service'] == 'momentum-paper'
             and event['payload'] == {'consumer_name': 'momentum-paper', 'mode': 'replace', 'symbols': []},
             'idle simulator attempted nonempty/other publish')
        self.calls.append(event)
        return '0-1'

    def __getattr__(self, name):
        raise Refusal('idle simulator attempted external I/O: ' + name)


class ProbeLogger:
    def exception(self, *args, **kwargs):
        raise Refusal('application read-only probe swallowed an error: ' + str(args[0]))
    error = exception
    def info(self, *args, **kwargs):
        pass
    warning = debug = info


def v2_owned_symbols(factory, config):
    """Direct fail-closed counterpart of v2's three-source ownership union.

    The old pre-COLDSTART polling method catches read errors and returns its empty
    constructor cache. It is therefore never used as an installation flat proof.
    """
    from sqlalchemy import select
    from project_mai_tai.db.models import AccountPosition, BrokerAccount, OmsManagedPosition, VirtualPosition
    accounts = {name for name in (config.strategy_schwab_1m_v2_account_name,
                                 config.strategy_schwab_1m_v2_webull_account_name) if name}
    need(accounts == {'live:schwab_1m_v2', 'live:orb'}, 'v2 account scope differs')
    with factory() as session:
        found = set(session.scalars(select(BrokerAccount.name).where(BrokerAccount.name.in_(accounts))))
        need(found == accounts, 'ownership broker accounts incomplete')
        managed = set(session.scalars(select(OmsManagedPosition.symbol).where(
            OmsManagedPosition.strategy_code == 'schwab_1m_v2', OmsManagedPosition.current_quantity != 0)))
        account = set(session.scalars(select(AccountPosition.symbol).join(BrokerAccount,
            BrokerAccount.id == AccountPosition.broker_account_id).where(
                BrokerAccount.name.in_(accounts), AccountPosition.quantity != 0)))
        virtual = set(session.scalars(select(VirtualPosition.symbol).join(BrokerAccount,
            BrokerAccount.id == VirtualPosition.broker_account_id).where(
                BrokerAccount.name.in_(accounts), VirtualPosition.quantity != 0)))
    need(all(isinstance(x, str) and x for x in managed | account | virtual), 'ownership symbol unreadable')
    # No protected/manual exception: tonight requires every holding to be zero.
    return managed | account | virtual


def input_proofs(config, reader):
    """Actual read-only restore selectors; no service run loop, emitters or feeds.

    Tonight requires an empty strategy/v2 population. Nonempty restoration needs
    separately reviewed richer input proof, never a guessed set from owner hash.
    """
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker
    from unittest.mock import patch
    from project_mai_tai.services.strategy_engine_app import StrategyEngineService, StrategyEngineState
    from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
    from project_mai_tai.services.orb_app import OrbService
    from project_mai_tai.services.orb_schwab_app import OrbSchwabService
    from project_mai_tai.services.momentum_paper_app import MomentumPaperService
    from project_mai_tai.events import StrategyStateSnapshotEvent
    engine = readonly_engine(config)
    factory = sessionmaker(bind=engine)
    simulated = RecordOnlyRedis()
    proof = {}
    try:
        with engine.connect() as conn:
            # A failed read is not an empty restore. No nonempty position fallback.
            need(conn.execute(text('SELECT count(*) FROM virtual_positions WHERE quantity <> 0')).scalar_one() == 0,
                 'nonzero strategy virtual positions')
            snap = conn.execute(text("SELECT payload->>'scanner_session_start_utc' FROM dashboard_snapshots WHERE snapshot_type='scanner_confirmed_last_nonempty'")).fetchmany(2)
            need(len(snap) == 1 and isinstance(snap[0][0], str), 'scanner input unreadable/ambiguous')
        strategy = StrategyEngineService.__new__(StrategyEngineService)
        strategy.settings, strategy.session_factory, strategy.logger = config, factory, ProbeLogger()
        strategy.state = StrategyEngineState(config, session_factory=factory)
        strategy._seed_confirmed_candidates_from_dashboard_snapshot()
        desired = strategy.state.market_data_symbols()
        need(not desired and not strategy.state.schwab_active_symbols(), 'nonempty strategy restore needs reviewed live-input proof')
        proof['strategy-engine'] = {'symbols': [], 'scanner_source_session': snap[0][0],
                                    'selector': 'StrategyEngineState + _seed_confirmed_candidates_from_dashboard_snapshot; flat DB'}
        v2 = SchwabV2BotService(config, session_factory=factory)
        need(v2._market_session(now()) == 'closed' and not v2._within_entry_window(now()), 'v2 weekend gates open')
        held = v2_owned_symbols(factory, config)
        need(not held, 'v2 held input nonempty')
        seed = reader.rows('strategy-state', reverse=True)
        need(len(seed) == 1, 'v2 scanner input missing')
        event = StrategyStateSnapshotEvent.model_validate(seed[0][1])
        selected = v2._extract_confirmed_symbols(event) if v2._strategy_state_event_is_current(event) else []
        need(not selected, 'nonempty v2 input requires reviewed live-input proof')
        proof['schwab-1m-v2'] = {'symbols': [], 'selector': 'current strategy-state scanner selector + position/managed read',
                               'selected': selected, 'held': sorted(held), 'market_session': 'closed', 'entry_gate': False}
        for name, cls in (('orb', OrbService), ('orb-schwab', OrbSchwabService)):
            with patch('project_mai_tai.services.orb_app.logger', ProbeLogger()):
                bot = cls(settings=config, redis_client=simulated, session_factory=factory)
                universe = bot._pre_open_universe(now=now())
                need(universe == [], 'ORB universe not idle')
                if name == 'orb-schwab':
                    need(bot._closed_bars == [] and bot._opening_orders == {}, 'ORB-Schwab input not idle')
                if name == 'orb':
                    bot._restore_paper_lifecycle()
                    desired = bot._paper_market_symbols()
                    need(not desired, 'ORB restored paper lifecycle nonempty')
            proof[name] = {'symbols': [], 'scanner_source_session': snap[0][0],
                           'selector': cls.__name__ + '._pre_open_universe + flat/restored lifecycle', 'universe': universe}
        paper = MomentumPaperService(settings=config, redis_client=simulated, clock=now)
        asyncio.run(paper._tick())
        need(paper._engine is None and paper._gateway_task is None and not paper._subscribed_symbols,
             'paper not disconnected/weekend-idle')
        proof['momentum-paper'] = {'symbols': [], 'selector': 'actual weekend _tick with explicit empty-replace recording fake',
                                   'engine': None, 'gateway': None, 'broker_route': 'none'}
    finally:
        engine.dispose()
    return {name: {'symbols': value['symbols'], 'evidence': json.dumps(value, sort_keys=True),
                   'sha256': hashlib.sha256(canonical(value)).hexdigest()} for name, value in proof.items()}


def archive_intents(reader):
    key = reader.prefix + ':strategy-intents'
    initial = reader.call('XLEN', key)
    need(0 <= initial <= 2000, 'retained intent archive too large')
    result, cursor = [], '-'
    for _ in range(initial):
        row = reader.rows('strategy-intents', cursor=cursor)
        need(len(row) == 1, 'intent archive trimmed/unreadable')
        result.append(row[0])
        cursor = '(' + row[0][0]
    need(not reader.rows('strategy-intents', cursor=cursor) and reader.call('XLEN', key) == initial,
         'intent archive moved while read')
    return result


def flat_checkpoint(config, *, accepted):
    expiry = token(config)
    need(digest(FLAT) == FLAT_HASH, 'flat helper hash drift')
    raw = command(str(REPO / '.venv/bin/python'), str(FLAT), timeout=90)
    flat = parse_flat(raw)
    state = durable(config)
    observed = asyncio.run(broker_orders(config))
    need(observed['observed'] == accepted, 'unexplained broker inventory changed; no implicit waiver')
    return {'at': now().isoformat(), 'token': expiry, 'flat': flat, 'flat_output': raw,
            'durable': state, 'orders': observed}


def capture_host():
    window(now(), prepare=True)
    config = settings()
    token(config)
    durable(config)
    reader = RedisRead(config)
    redis_state(reader)
    orders = asyncio.run(broker_orders(config))
    need(digest(FLAT) == FLAT_HASH, 'flat helper hash drift')
    parse_flat(command(str(REPO / '.venv/bin/python'), str(FLAT), timeout=90))
    files = {}
    for name in sorted(FILE_PATHS):
        path = Path(name)
        need(not path.is_symlink() and path.is_file(), 'pinned file not regular')
        info = path.stat()
        files[name] = {'sha256': digest(path), 'uid': info.st_uid, 'gid': info.st_gid,
                       'mode': stat.S_IMODE(info.st_mode)}
    rows = {name: identity(name) for name in SERVICES}
    need(all(active(row) for row in rows.values()), 'capture requires twelve healthy services')
    inputs = input_proofs(config, reader)
    result = {'captured_at': now().isoformat(), 'services': rows, 'tv_alerts': identity('tv-alerts'),
              'files': files, 'units': {name: hashlib.sha256(command('systemctl', 'cat', name).encode()).hexdigest()
                                       for name in sorted(UNIT_NAMES)},
              'subscription_inputs': inputs, 'accepted_unexplained_orders': orders['observed'],
              'intent_ids': [row[0] for row in archive_intents(reader)], 'status_contract': STATUS_CONTRACT,
              'pager_url': 'https://ntfy.sh/mai-tai-preopen-28806a5a97b7'}
    need({name: identity(name) for name in SERVICES} == rows, 'capture identity race')
    return result


def process_settings(name):
    from project_mai_tai.settings import Settings
    before = identity(name)
    need(active(before), 'process not healthy: ' + name)
    path = Path('/proc') / before['MainPID'] / 'environ'
    with path.open('rb') as stream:
        raw = stream.read(256_001)
    need(len(raw) <= 256_000, 'process environment oversized')
    pairs = [part.decode().split('=', 1) for part in raw.split(b'\0') if b'=' in part]
    env = dict(pairs)
    need(len(env) == len(pairs) and len({k.upper() for k in env}) == len(env), 'duplicate process env')
    original = dict(os.environ)
    try:
        os.environ.clear()
        os.environ.update(env)
        config = Settings(_env_file=None)
    finally:
        os.environ.clear()
        os.environ.update(original)
    need(identity(name) == before, 'process identity moved during Settings read')
    return before, env, config


def loaded_values():
    from project_mai_tai.strategy_core.entry_gate import resolve_entry_window
    sizing = {'strategy_schwab_1m_v2_entry_notional_usd': 600,
              'strategy_schwab_1m_v2_webull_entry_notional_usd': 300,
              'strategy_schwab_1m_v2_entry_max_shares': 1000}
    retained = {'oms_v2_webull_mirror_fresh_price_enabled': True,
                'oms_v2_webull_mirror_quote_max_age_ms': 10000,
                'oms_v2_eh_resting_entry_quote_max_age_ms': 2000,
                'oms_v2_cw_target_pct': 5.0, 'oms_v2_cw_hard_stop_pct': 8.0,
                'oms_v2_cw_floor_exit_enabled': False, 'strategy_schwab_1m_v2_retry_one_enabled': True,
                'oms_v2_eod_oco_transition_enabled': True, 'oms_v2_overnight_flatten_enabled': True,
                'oms_v2_cw_target_stay_enabled': True}
    result, windows = {}, {}
    for name in RESTARTED + ('market-data',):
        row, env, config = process_settings(name)
        expected = {}
        if name in {'oms', 'schwab-1m-v2'}:
            expected.update(sizing)
            windows[name] = list(resolve_entry_window(config))
        if name == 'oms':
            expected.update(retained)
        if name == 'strategy':
            expected['strategy_polygon_30s_enabled'] = False
        if name == 'orb-schwab':
            expected.update(orb_live_schwab_orders_enabled=True, orb_schwab_observe_enabled=False)
        if name == 'market-data':
            expected['redis_snapshot_batch_stream_maxlen'] = 120
        if name in START:
            expected['market_data_subscription_startup_enabled'] = True
        if name == 'schwab-1m-v2':
            expected['strategy_schwab_1m_v2_atr_reprice_handoff_enabled'] = True
        values = {}
        for key, wanted in expected.items():
            value = getattr(config, key)
            need(value == wanted, f'loaded setting mismatch {name}:{key}')
            env_key = 'MAI_TAI_' + key.upper()
            if env_key in FLAGS or key in sizing or key in retained and key.startswith('oms_v2_webull'):
                need(env_key in env and env[env_key].lower() == str(wanted).lower(), 'explicit env missing ' + env_key)
            values[key] = {'value': value, 'source': 'env' if env_key in env else 'default'}
        if name == 'oms':
            for key in FLAGS:
                need(env.get(key) == 'true', 'new OMS explicit flag missing; admission reader is v2 only')
            values['new_flag_env_only'] = dict(FLAGS)
        result[name] = {'identity': row, 'values': values}
    need(windows['oms'] == windows['schwab-1m-v2'], 'configured OMS/v2 entry windows disagree')
    return {'services': result, 'entry_windows': windows}


def scanner_requirements():
    from project_mai_tai.strategy_core.momentum_alerts import MomentumAlertEngine, MomentumAlertConfig
    row, env, config = process_settings('strategy')
    interval = config.market_data_snapshot_interval_seconds
    count = MomentumAlertEngine(MomentumAlertConfig(), scan_interval_secs=interval).get_warmup_status()['squeeze_10min_needs']
    need(interval == 5 and 0 < count <= 120, 'scanner retention/scan-interval mismatch')
    reader = RedisRead(settings())
    retained = reader.call('XLEN', reader.prefix + ':snapshot-batches')
    return {'identity': row, 'scan_interval_seconds': interval, 'squeeze_10min_needs': count,
            'retained_batches_xlen_only': retained, 'snapshot_payload_reads': 0}


def dispatch(operation, request):
    config = settings()
    if operation == 'context':
        # Private pipe to the orchestrator only; never included in evidence/logs.
        return {'redis_url': config.redis_url, 'redis_stream_prefix': config.redis_stream_prefix,
                'static_symbols': config.market_data_static_symbol_list}
    if operation == 'flat':
        return flat_checkpoint(config, accepted=request['accepted'])
    if operation == 'inputs':
        return input_proofs(config, RedisRead(config))
    if operation == 'loaded':
        return loaded_values()
    if operation == 'scanner':
        return scanner_requirements()
    if operation == 'token':
        return token(config)
    raise Refusal('unsupported read-only proof operation')


if __name__ == '__main__':
    # Parent staging invokes capture before approval; stdout is only the host object.
    try:
        sys.path.insert(0, str(REPO / 'src'))
        with contextlib.redirect_stdout(sys.stderr):
            need(len(sys.argv) == 2, 'one read-only operation required')
            result = capture_host() if sys.argv[1] == 'capture' else dispatch(sys.argv[1], json.load(sys.stdin))
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as exc:
        print('READ-ONLY EVIDENCE UNKNOWN: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
