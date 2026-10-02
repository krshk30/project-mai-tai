"""Local-only scorer for the frozen study; never modifies production."""
import argparse
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
def dt(x): return datetime.fromisoformat(x)

def feature(bars, anchor):
    if len(bars) != 10: return None, 'fewer_than_10'
    values = []
    for b in bars:
        h,l,c = map(float,(b['high_price'], b['low_price'], b['close_price']))
        if not all(math.isfinite(x) for x in (h,l,c)) or c<=0 or l<=0 or h<l or not l<=c<=h:
            return None, 'invalid_ohlc'
        values.append(100*(h-l)/c)
    raw = statistics.mean(values)
    if any(dt(b['created_at'])>anchor or dt(b['updated_at'])>anchor for b in bars):
        return raw, 'not_asof'
    return raw, 'timestamp_eligible'

def load_db(path):
    records=[json.loads(x) for x in Path(path).read_text().splitlines()]
    assert records[-1]['type']=='complete', 'incomplete DB extract'
    fills=next(x['rows'] for x in records if x['type']=='fills')
    positions=[x['row'] for x in records if x['type']=='position']
    counts=Counter(p['buy']['order_id'] for p in positions if p.get('buy'))
    for p in positions:
        b=p.get('buy')
        p['cohort']='replication' if p['account']=='live:schwab_1m_v2' else 'holdout'
        p['match_reason']='ok'
        if b is None: p['match_reason']='no_buy'
        elif p['match_tie'] or counts[b['order_id']]>1: p['match_reason']='ambiguous_buy'
        elif p['account']=='live:orb' and p['schwab_twin']: p['match_reason']='schwab_twin'
        if b:
            p['at']=b['time']; p['price']=float(b['price'])
            p['feature'],p['feature_status']=feature(p.get('entry_bars',[])[:10],dt(b['time']))
            placed=b['intent_at'] or b['submitted_at']
            p['placement_feature'],p['placement_status']=feature(p.get('placement_bars',[]),dt(placed)) if placed else (None,'no_timestamp')
            p['calm']=p['feature']<1.5 if p['feature'] is not None else None
            at=dt(p['at']).astimezone(ET)
            p['day']=str(at.date())
            minute=at.hour*60+at.minute
            p['block']=('07:00-09:30' if 420<=minute<570 else '09:30-11:00' if 570<=minute<660
                        else '11:00-16:00' if 660<=minute<960 else 'outside')
    return records[0],positions,fills

def real_cycles(fills):
    lanes=defaultdict(list)
    for f in fills: lanes[(f['account'],f['symbol'])].append(f)
    out={}
    for lane, fs in lanes.items():
        balance=0.0; buys=[]; cost=proceeds=0.0; invalid=False
        for f in sorted(fs,key=lambda x:(dt(x['filled_at']),x['id'])):
            q,price=float(f['quantity']),float(f['price'])
            if q<=0 or price<=0: invalid=True; continue
            if f['side'].lower()=='buy':
                if abs(balance)<1e-7: buys=[]; cost=proceeds=0; invalid=False
                balance+=q; cost+=q*price; buys.append(f['order_id'])
            elif f['side'].lower()=='sell':
                balance-=q; proceeds+=q*price
                if balance < -1e-7:
                    invalid=True; balance=0; buys=[]; cost=proceeds=0
                elif abs(balance)<1e-7 and cost>0:
                    if not invalid and len(set(buys))==1:
                        out[buys[0]]={'actual_return':100*(proceeds-cost)/cost,'actual_dollars':proceeds-cost}
                    balance=0; buys=[]; cost=proceeds=0
    return out

def score_path(p, path):
    if not path or not path.get('complete'): return {'outcome':'path_unavailable'}
    at=dt(p['at']); entry_ms=at.timestamp()*1000
    end=at.astimezone(ET).replace(hour=19,minute=55,second=0,microsecond=0)
    horizon=end.timestamp()*1000
    upper=p['price']*1.05; lower=p['price']*.92
    bars=sorted(path['bars'],key=lambda x:x['t'])
    endpoint=None
    for b in bars:
        t=b['t']
        if t+1000<=entry_ms or t>horizon: continue
        hitup=b['h']>=upper; hitdown=b['l']<=lower
        if t<entry_ms:
            if hitup or hitdown: return {'outcome':'entry_second_ambiguous'}
            continue
        if hitup and hitdown: return {'outcome':'same_second_ambiguous'}
        if hitup: return {'outcome':'target','uniform':5.0,'hit_time_ms':t}
        if hitdown: return {'outcome':'stop','uniform':-8.0,'hit_time_ms':t}
        if t+1000<=horizon: endpoint=b
    if dt(path['asof']).timestamp()*1000<horizon: return {'outcome':'horizon_not_closed'}
    if endpoint is None or horizon-(endpoint['t']+1000)>300000: return {'outcome':'endpoint_unavailable'}
    return {'outcome':'eod','uniform':100*(endpoint['c']/p['price']-1)}

def stats(rows,key):
    vals=[p[key] for p in rows if key in p]
    n=len(vals)
    if not n: return {'n':0,'mean':None,'se':None,'cluster_se':None}
    mean=statistics.mean(vals); se=statistics.stdev(vals)/math.sqrt(n) if n>1 else None
    groups=defaultdict(list)
    for p in rows:
        if key in p: groups[p['symbol']].append(p[key])
    rng=random.Random(20261002)
    samples=[]
    if len(groups)>1:
        names=sorted(groups)
        for _ in range(2000):
            draw=[v for name in rng.choices(names,k=len(names)) for v in groups[name]]
            samples.append(statistics.mean(draw))
    return dict(n=n,mean=mean,se=se,cluster_se=statistics.stdev(samples) if samples else None)

def table(rows):
    result={}
    for label, calm in [('calm',True),('other',False)]:
        group=[p for p in rows if p['calm']==calm]
        measured=[p for p in group if 'uniform' in p]
        n=len(measured); wins=sum(p['outcome']=='target' for p in measured)
        rate=wins/n if n else None
        result[label]={'entries':len(group),'wins':wins,'win_rate':rate,
            'win_se': math.sqrt(rate*(1-rate)/n) if n else None,
            'uniform':stats(group,'uniform'),'actual_return':stats(group,'actual_return'),
            'actual_dollars':stats(group,'actual_dollars'),
            'actual_matched_uniform_return':stats(measured,'actual_return')}
    a,b=result['calm']['uniform'], result['other']['uniform']
    result['gap']=a['mean']-b['mean'] if a['n'] and b['n'] else None
    result['gap_se']=math.hypot(a['se'],b['se']) if a['se'] is not None and b['se'] is not None else None
    return result

def analyze(db, paths):
    meta,positions,fills=load_db(db)
    cycles=real_cycles(fills)
    records={}
    if Path(paths).exists():
        for line in Path(paths).read_text().splitlines():
            p=json.loads(line); records[(p['symbol'],p['day'])]=p
    for p in positions:
        if p['match_reason']!='ok': continue
        p.update(cycles.get(p['buy']['order_id'],{}))
        p.update(score_path(p,records.get((p['symbol'],p['day']))))
    report={'metadata':meta, 'exclusions':dict(Counter(p['match_reason'] for p in positions)),
            'raw_path_days':len(records),'cohorts':{}}
    for cohort in ('holdout','replication'):
        pop=[p for p in positions if p['cohort']==cohort and p['match_reason']=='ok']
        eligible=[p for p in pop if p['feature_status']=='timestamp_eligible']
        t=table(eligible)
        measured=[p for p in eligible if 'uniform' in p]
        contributions=defaultdict(float)
        for p in measured:
            if p['calm']: contributions[p['symbol']]+=p['uniform']
        best=min(contributions,key=lambda x:(-contributions[x],x)) if contributions else None
        dropped=table([p for p in eligible if p['symbol']!=best])
        blocks={b:table([p for p in eligible if p['block']==b]) for b in ('07:00-09:30','09:30-11:00','11:00-16:00')}
        a,b=t['calm'],t['other']
        evaluable=a['uniform']['n']>=40 and b['uniform']['n']>0 and dropped['gap'] is not None and sum(x['gap'] is not None for x in blocks.values())>=2
        passed=(evaluable and a['win_rate']>=.6 and b['win_rate']<=.55 and t['gap']>=1 and dropped['gap']>0 and sum(x['gap'] is not None and x['gap']>0 for x in blocks.values())>=2)
        report['cohorts'][cohort]={'population':len(pop),'feature_status':dict(Counter(p['feature_status'] for p in pop)),
            'placement_status':dict(Counter(p['placement_status'] for p in pop)),
            'early_0710':dict(Counter(p['feature_status'] for p in pop if dt(p['at']).astimezone(ET).hour*60+dt(p['at']).astimezone(ET).minute<430)),
            'outcomes':dict(Counter(p['outcome'] for p in eligible)), 'table':t,
            'best_name':best,'best_name_contribution':contributions.get(best), 'drop_best':dropped,'blocks':blocks,
            'proxy_gate':'PASS' if passed else 'FAIL' if evaluable else 'UNMEASURED',
            'halves':{label:table([p for p in eligible if (p['day']<'2026-09-14')==early]) for label,early in [('Aug03-Sep13',True),('Sep14-Oct02',False)]},
            'classification_changed':sum((p['placement_feature']<1.5)!=p['calm'] for p in eligible if p['placement_status']=='timestamp_eligible'),
            'placement_comparable':sum(p['placement_status']=='timestamp_eligible' for p in eligible)}
    return report,positions

if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('db'); ap.add_argument('--paths'); ap.add_argument('--jobs'); ap.add_argument('--output')
    args=ap.parse_args()
    if args.jobs:
        meta,positions,fills=load_db(args.db)
        grouped={}
        for p in positions:
            if p['match_reason']!='ok' or p['feature_status']!='timestamp_eligible': continue
            key=(p['symbol'],p['day']); at=dt(p['at'])
            if key not in grouped or at<dt(grouped[key]['start']): grouped[key]={'symbol':p['symbol'],'day':p['day'],'start':str(at)}
        Path(args.jobs).write_text(json.dumps(sorted(grouped.values(),key=lambda x:(x['day'],x['symbol']))))
        print(json.dumps({'jobs':len(grouped),'metadata':meta,'match':dict(Counter(p['match_reason'] for p in positions)),
            'feature':dict(Counter(p.get('feature_status') for p in positions if p['match_reason']=='ok'))},default=str))
    else:
        report,positions=analyze(args.db,args.paths)
        Path(args.output).write_text(json.dumps(report,indent=2,default=str))
        Path(args.output+'.trades.json').write_text(json.dumps(positions,indent=2,default=str))
        print(json.dumps(report,indent=2,default=str))
