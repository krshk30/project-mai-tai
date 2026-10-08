"""Retire only paper ORB via its normal empty subscription replacement."""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

OWNERS = {'strategy-engine', 'schwab-1m-v2', 'orb', 'orb-schwab', 'momentum-paper'}
FIELDS = OWNERS | {'_migration_complete', '_last_applied_id'}


def need(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def canonical(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def identity():
    result = subprocess.run(['systemctl', 'show', 'project-mai-tai-orb.service',
        '--property=MainPID,ActiveState,SubState,UnitFileState,Result'],
        capture_output=True, text=True, check=True, timeout=10)
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


def validate_owners(rows):
    need(set(rows) == FIELDS and rows['_migration_complete'] == '1', 'owner population/marker differs')
    need(sum(len(value) for value in rows.values()) <= 100000, 'owner reply overflow')
    for name in OWNERS:
        symbols = json.loads(rows[name])
        need(isinstance(symbols, list) and len(symbols) <= 1000
             and all(isinstance(symbol, str) for symbol in symbols)
             and len(symbols) == len(set(symbols)), 'malformed owner symbols')
    redis_id(rows['_last_applied_id'])


def redis_id(value):
    need(isinstance(value, str) and re.fullmatch(r'[0-9]{1,20}-[0-9]{1,20}', value) is not None,
         'malformed Redis cursor/request id')
    parts = tuple(map(int, value.split('-')))
    need(all(part <= 2**64 - 1 for part in parts), 'malformed Redis id overflow')
    return parts


def validate_receipt(receipt):
    need(receipt.get('verdict') == 'RETIRED' and receipt.get('normal_replace_count') == 1,
         'retirement not proven')
    before, after = receipt['before']['owners'], receipt['after']['owners']
    validate_owners(before)
    validate_owners(after)
    need(json.loads(after['orb']) == [], 'orb owner not empty')
    need(all(before[name] == after[name] for name in FIELDS - {'orb', '_last_applied_id'}),
         'retirement changed another owner/marker')
    state = receipt['after']['state']
    need(state['MainPID'] == '0' and state['ActiveState'] == 'inactive'
         and state['UnitFileState'] == 'disabled', 'paper ORB not inactive/disabled')
    need(redis_id(after['_last_applied_id']) >= redis_id(receipt.get('request_id'))
         and redis_id(receipt['request_id']) > redis_id(before['_last_applied_id']),
         'replace application cursor unproven')


async def capture(client, settings):
    key = settings.redis_stream_prefix + ':market-data-subscription-owners'
    need(await client.type(key) == 'hash' and await client.hlen(key) == len(FIELDS), 'owner population differs')
    for name in FIELDS:
        need(await client.hstrlen(key, name) <= 30000, 'owner value overflow')
    owners = await client.hgetall(key)
    validate_owners(owners)
    return dict(owners=owners, state=identity(), owners_sha256=hashlib.sha256(canonical(owners)).hexdigest(),
                captured_at_utc=datetime.now(timezone.utc).isoformat())


class OneReplace:
    """Only XADD is exposed to the approved subscription helper; no deletion API."""
    def __init__(self, client):
        self.client, self.ids = client, []

    async def xadd(self, key, fields, **kwargs):
        need(not self.ids, 'duplicate replacement request')
        event = json.loads(fields['data'])
        need(event['source_service'] == 'orb' and event['payload']['consumer_name'] == 'orb'
             and event['payload']['mode'] == 'replace' and event['payload']['symbols'] == [],
             'foreign subscription request')
        request = await asyncio.wait_for(self.client.xadd(key, fields, **kwargs), timeout=5)
        self.ids.append(request)
        return request


def started_in_window(authorization):
    if not isinstance(authorization, dict):
        return False
    stamp = datetime.fromisoformat(authorization['at_utc']).astimezone(ZoneInfo('America/New_York'))
    return (stamp.date().isoformat() == '2026-10-08' and stamp.hour >= 16
            and len(authorization.get('approved_sha', '')) == 40 and authorization.get('attempt'))


async def publish(client, settings, before, *, now=None, authorization=None):
    from project_mai_tai.services.orb_app import OrbService

    local = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo('America/New_York'))
    need(started_in_window(authorization) or (local.date().isoformat() == '2026-10-08' and local.hour >= 16),
         'retirement outside after-close scope')
    need(settings.market_data_subscription_startup_enabled is True, 'normal startup replace disabled')
    state = identity()
    need(state['MainPID'] == '0' and state['ActiveState'] == 'inactive'
         and state['UnitFileState'] == 'disabled', 'retirement requires paper unit stopped and disabled')
    # Publish through the exact approved application's COLDSTART path, never HDEL.
    proxy = OneReplace(client)
    await asyncio.wait_for(OrbService(settings=settings, redis_client=proxy)._sync_gateway_subscription([]), timeout=10)
    need(len(proxy.ids) == 1, 'empty startup replacement was not published')
    deadline = time.monotonic() + 60
    while True:
        after = await asyncio.wait_for(capture(client, settings), timeout=30)
        receipt = dict(verdict='RETIRED', before=before, after=after, normal_replace_count=len(proxy.ids),
                       request_id=proxy.ids[0], source='OrbService._sync_gateway_subscription([])')
        # Other owner changes are a real conflict, not a reason to re-publish.
        need(all(before['owners'][name] == after['owners'][name]
                 for name in FIELDS - {'orb', '_last_applied_id'}), 'another owner changed during retirement')
        if json.loads(after['owners']['orb']) == [] and redis_id(after['owners']['_last_applied_id']) >= redis_id(proxy.ids[0]):
            validate_receipt(receipt)
            return receipt
        need(time.monotonic() < deadline, 'empty replacement not observed within 60 seconds')
        await asyncio.sleep(1)


async def run(args):
    from redis.asyncio import Redis
    from project_mai_tai.settings import Settings

    settings = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
    client = Redis.from_url(settings.redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
    try:
        if args.mode == 'publish':
            # A timeout after XADD is uncertain. Never silently resend on a
            # rerun with the same output; the runner will seal ABORT.
            with args.output.with_suffix(args.output.suffix + '.publish-started').open('xb') as marker:
                marker.write(canonical(dict(before=str(args.before), authorization=str(args.authorization))))
        result = (await asyncio.wait_for(capture(client, settings), timeout=30) if args.mode == 'before'
                  else await publish(client, settings, json.loads(args.before.read_bytes()),
                                     authorization=json.loads(args.authorization.read_bytes())))
        with args.output.open('xb') as stream:
            stream.write(canonical(result))
        print(json.dumps(dict(output=str(args.output), sha256=hashlib.sha256(canonical(result)).hexdigest(),
                              verdict=result.get('verdict', 'READ_ONLY_BEFORE'))))
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('before', 'publish'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--before', type=Path)
    parser.add_argument('--authorization', type=Path)
    args = parser.parse_args()
    need(args.mode == 'before' or (args.before is not None and args.authorization is not None),
         'publish needs exclusive before receipt and first-write authorization')
    need(not args.output.exists() and not args.output.is_symlink(), 'exclusive retirement output already exists')
    asyncio.run(run(args))


if __name__ == '__main__':
    main()
