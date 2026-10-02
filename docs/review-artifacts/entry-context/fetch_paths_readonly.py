"""Sequential study-only REST reads; no Redis, database or service mutation."""
import base64
import json
import sys
import time
from datetime import UTC, datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from project_mai_tai.settings import Settings

jobs = json.loads(base64.b64decode(sys.argv[1]))
s = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')
assert s.massive_api_key
ET = ZoneInfo('America/New_York')
freeze = datetime(2026,10,2,14,40,tzinfo=ET)
with httpx.Client(headers={'Authorization':'Bearer '+s.massive_api_key}, timeout=20) as client:
    for index, job in enumerate(jobs):
        at = datetime.fromisoformat(job['start'])
        end = at.astimezone(ET).replace(hour=19,minute=55,second=0,microsecond=0)
        asof = min(datetime.now(UTC), freeze)
        end = min(end, asof)
        first_ms = int(at.timestamp())*1000
        last_ms = int(end.timestamp()*1000)-1
        url = f'https://api.massive.com/v2/aggs/ticker/{job["symbol"]}/range/1/second/{first_ms}/{last_ms}'
        result = {**job,'asof':str(asof),'from_ms':first_ms,'to_ms':last_ms,
                  'source':'Massive independently requested unadjusted 1s','pages':[], 'bars':[], 'complete':False}
        try:
            for page in range(10):
                assert urlparse(url).hostname in {'api.massive.com','api.polygon.io'}
                params = {'adjusted':'false','sort':'asc','limit':50000} if page==0 else None
                with client.stream('GET',url,params=params) as response:
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body)>16_000_000: raise ValueError('reply exceeds 16 MB')
                    result['pages'].append({'http_status':response.status_code,'bytes':len(body)})
                    if response.status_code!=200: raise ValueError('HTTP '+str(response.status_code))
                    payload=json.loads(body)
                if payload.get('status') not in ('OK','DELAYED'): raise ValueError('bad provider status')
                result['bars'].extend(payload.get('results',[]))
                if not payload.get('next_url'):
                    result['complete']=True
                    break
                url=payload['next_url']
                time.sleep(1)
            if not result['complete']: raise ValueError('pagination exceeded ten pages')
        except Exception as exc:
            result['error']=type(exc).__name__+':'+str(exc).split('https:')[0]
        print(json.dumps(result),flush=True)
        print(f'path {index+1}/{len(jobs)} {job["day"]} {job["symbol"]} complete={result["complete"]} bars={len(result["bars"])}',file=sys.stderr,flush=True)
        time.sleep(1)
