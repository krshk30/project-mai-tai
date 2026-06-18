import os, json, time, pickle
from urllib.parse import urlencode
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from datetime import datetime, timezone, timedelta
import psycopg
from statistics import median

BASE=os.environ["ORB_BASE"].rstrip("/"); DSN=os.environ["ORB_DSN"]
TP="/var/lib/macd-webhook-server/data/schwab_tokens.json"; CACHE="/tmp/orb_bars.pkl"
DAYS=["2026-06-10","2026-06-11","2026-06-12","2026-06-15","2026-06-16","2026-06-17","2026-06-18"]
QTY=10; EXCLUDE={("2026-06-15","RGNT")}
PRIOR={"A_ema9":269.7,"B_ema20":334.7,"B_vwap":352.8,"C_2xema9":421.7,"D_giveback":161.8,"E2_2of3":235.6,"E3_3of3":470.0,"F_ladder":143.4}
def tok(): return json.loads(open(TP).read())["access_token"]
conn=psycopg.connect(DSN)
cache=pickle.load(open(CACHE,"rb")) if os.path.exists(CACHE) else {}

def universe(day):
    r=conn.execute("SELECT symbol FROM trade_intents WHERE (created_at AT TIME ZONE 'UTC')::date=%s GROUP BY 1 HAVING count(*)>=2",(day,)).fetchall()
    return [x[0] for x in r]

def fetch(sym,day):
    k=(day,sym)
    if k in cache: return cache[k]
    d=datetime.strptime(day,"%Y-%m-%d").replace(tzinfo=timezone.utc)
    s=int((d+timedelta(hours=12)).timestamp()*1000); e=int((d+timedelta(hours=15,minutes=30)).timestamp()*1000)
    p=urlencode(dict(symbol=sym,periodType="day",frequencyType="minute",frequency=1,startDate=s,endDate=e,needExtendedHoursData="true"))
    try:
        body=urlopen(Request(f"{BASE}/marketdata/v1/pricehistory?{p}",headers={"Authorization":f"Bearer {tok()}","Accept":"application/json"}),timeout=30).read().decode(); time.sleep(0.33)
    except HTTPError:
        body='{}'
    cs=json.loads(body).get("candles") or []
    o=[dict(t=datetime.fromtimestamp(int(c["datetime"])/1000,tz=timezone.utc),o=float(c["open"]),h=float(c["high"]),l=float(c["low"]),c=float(c["close"]),v=int(c.get("volume",0) or 0)) for c in cs]
    o.sort(key=lambda b:b["t"]); cache[k]=o; return o

def enrich(bars,OP):
    def ema(s,n):
        k=2/(n+1); e=None; o=[]
        for x in s:
            e=x if e is None else x*k+e*(1-k); o.append(e)
        return o
    cl=[b["c"] for b in bars]; e9=ema(cl,9); e20=ema(cl,20); cpv=0.0; cv=0; out=[]
    for i,b in enumerate(bars):
        b=dict(b); b["ema9"]=e9[i]; b["ema20"]=e20[i]
        if b["t"]>=OP:
            tp=(b["h"]+b["l"]+b["c"])/3; cpv+=tp*b["v"]; cv+=b["v"]; b["vwap"]=cpv/cv if cv>0 else b["c"]
        else:
            b["vwap"]=None
        out.append(b)
    return out

def atr_at(b,i,n=14):
    tr=[max(b[j]["h"]-b[j]["l"],abs(b[j]["h"]-b[j-1]["c"]),abs(b[j]["l"]-b[j-1]["c"])) for j in range(max(1,i-n+1),i+1)]
    return sum(tr)/len(tr) if tr else b[i]["h"]-b[i]["l"]

def find_entry(bars,OP):
    oe=OP+timedelta(minutes=5); ob=[b for b in bars if OP<=b["t"]<oe]
    if not ob: return None
    ORh=max(b["h"] for b in ob); ORl=min(b["l"] for b in ob); avgV=sum(b["v"] for b in ob)/len(ob); ORw=ORh-ORl; wp=ORw/ORl*100 if ORl>0 else 0
    if wp>12 or wp<2: return None
    co=OP+timedelta(minutes=60)
    for i,b in enumerate(bars):
        if b["t"]<oe or b["t"]>co: continue
        if b["c"]>ORh and b["v"]>=1.5*avgV and b["vwap"] is not None and b["c"]>b["vwap"] and b["c"]>b["ema9"]:
            return dict(i=i,t=b["t"],p=b["c"],ORh=ORh,ORl=ORl,ORw=ORw)
    return None

def fin(ep,en,ex,mfe,mae):
    ret=(ex[1]-ep)/ep*100; mfp=mfe/ep*100
    return dict(et=en["t"],ep=ep,xt=ex[0],xp=ex[1],xr=ex[2],ret=ret,pnl=(ex[1]-ep)*QTY,mfe=mfp,mae=mae/ep*100,cap=(ret/mfp if mfp>0 else 0),gb=mfp-ret)

def sim_close(bars,en,rule):
    ep=en["p"]; ei=en["i"]; hard=en["ORl"]-0.5*atr_at(bars,ei); bvol=bars[ei]["v"]
    seg=[b for b in bars[ei+1:] if b["t"]<=en["t"]+timedelta(minutes=60)]
    rem=1.0; pnl=0.0; ex_t=None; ex_r=None; consec=0; mfe=0; mae=0; scaled=False; hwm=ep
    for idx,b in enumerate(seg):
        mfe=max(mfe,b["h"]-ep); mae=min(mae,b["l"]-ep); hwm=max(hwm,b["h"])
        if b["l"]<=hard:
            pnl+=rem*(hard-ep); ex_t=b["t"]; ex_r="HARD"; rem=0; break
        if rule=="F" and not scaled and b["h"]>=ep+en["ORw"]:
            pnl+=0.5*en["ORw"]; rem=0.5; scaled=True; hard=max(hard,ep)
        prior=seg[max(0,idx-2):idx]; swing=min([x["l"] for x in prior]) if prior else en["ORl"]
        consec=consec+1 if b["c"]<b["ema9"] else 0
        n3=(1 if b["c"]<swing else 0)+(1 if b["v"]<0.5*bvol else 0)+(1 if b["c"]<b["o"] else 0)
        if rule=="A": sig=b["c"]<b["ema9"]
        elif rule=="B20": sig=b["c"]<b["ema20"]
        elif rule=="Bvwap": sig=b["vwap"] is not None and b["c"]<b["vwap"]
        elif rule=="C": sig=consec>=2
        elif rule=="D": sig=b["c"]<hwm-0.5*(hwm-ep) or b["c"]<swing
        elif rule in ("E2","F"): sig=n3>=2
        elif rule=="E3": sig=n3>=3
        else: sig=False
        if sig:
            pnl+=rem*(b["c"]-ep); ex_t=b["t"]; ex_r=rule; rem=0; break
    if rem>0:
        if seg:
            pnl+=rem*(seg[-1]["c"]-ep); ex_t=seg[-1]["t"]; ex_r="TIME"
        else:
            ex_t=en["t"]; ex_r="NOFWD"
    return fin(ep,en,(ex_t,ep+pnl,ex_r),mfe,mae)

def sim_trail(bars,en,pct):
    ep=en["p"]; ei=en["i"]; stop=ep*(1-pct/100); hwm=ep
    seg=[b for b in bars[ei+1:] if b["t"]<=en["t"]+timedelta(minutes=60)]
    ex=None; mfe=0; mae=0
    for b in seg:
        mfe=max(mfe,b["h"]-ep); mae=min(mae,b["l"]-ep)
        if b["l"]<=stop:
            fill=b["o"] if b["o"]<stop else stop; ex=(b["t"],fill,"TRAIL"); break
        hwm=max(hwm,b["h"]); stop=max(stop,hwm*(1-pct/100))
    if ex is None:
        ex=(seg[-1]["t"],seg[-1]["c"],"TIME") if seg else (en["t"],ep,"NOFWD")
    return fin(ep,en,ex,mfe,mae)

def sim_combo(bars,en,pct=8):
    # TRAIL-pct% (intrabar hard stop) OR 2 consecutive closes < EMA9, whichever fires first
    ep=en["p"]; ei=en["i"]; stop=ep*(1-pct/100); hwm=ep; consec=0
    seg=[b for b in bars[ei+1:] if b["t"]<=en["t"]+timedelta(minutes=60)]
    ex=None; mfe=0; mae=0
    for b in seg:
        mfe=max(mfe,b["h"]-ep); mae=min(mae,b["l"]-ep)
        if b["l"]<=stop:
            fill=b["o"] if b["o"]<stop else stop; ex=(b["t"],fill,"TRAIL"); break
        consec=consec+1 if b["c"]<b["ema9"] else 0
        if consec>=2:
            ex=(b["t"],b["c"],"2xEMA9"); break
        hwm=max(hwm,b["h"]); stop=max(stop,hwm*(1-pct/100))
    if ex is None:
        ex=(seg[-1]["t"],seg[-1]["c"],"TIME") if seg else (en["t"],ep,"NOFWD")
    return fin(ep,en,ex,mfe,mae)

ENTRIES=[]
for day in DAYS:
    OP=datetime.strptime(day,"%Y-%m-%d").replace(tzinfo=timezone.utc)+timedelta(hours=13,minutes=30)
    for s in universe(day):
        if (day,s) in EXCLUDE: continue
        bars=enrich(fetch(s,day),OP)
        if len([b for b in bars if OP<=b["t"]<OP+timedelta(minutes=5)])<5: continue
        en=find_entry(bars,OP)
        if en: ENTRIES.append((day,s,bars,en))
pickle.dump(cache,open(CACHE,"wb"))

EXITS=[("TRAIL-3%",("t",3)),("TRAIL-5%",("t",5)),("TRAIL-8%",("t",8)),
       ("A_ema9",("c","A")),("B_ema20",("c","B20")),("B_vwap",("c","Bvwap")),
       ("C_2xema9",("c","C")),("D_giveback",("c","D")),("E2_2of3",("c","E2")),
       ("E3_3of3",("c","E3")),("F_ladder",("c","F")),("COMBO_T8+C",("k",8))]
def run1(et,kind):
    _,_,bars,en=et
    if kind[0]=="t": return sim_trail(bars,en,kind[1])
    if kind[0]=="k": return sim_combo(bars,en,kind[1])
    return sim_close(bars,en,kind[1])
def runner(kind,tgt):
    for et in ENTRIES:
        if et[1]==tgt:
            r=run1(et,kind); return r["ret"],r["cap"]
    return None,None

print(f"entries (RGNT-06-15 excluded) = {len(ENTRIES)}")
rows=[]
for name,kind in EXITS:
    res=[run1(et,kind) for et in ENTRIES]
    n=len(res); w=sum(1 for r in res if r["ret"]>0); tot=sum(r["ret"] for r in res)
    caps=[r["cap"] for r in res if r["mfe"]>0]; gb=sum(r["gb"] for r in res)/n
    rows.append(dict(name=name,n=n,win=w/n*100,avg=tot/n,tot=tot,mc=median(caps) if caps else 0,gb=gb,cr=runner(kind,"CRVO"),at=runner(kind,"ATPC")))

print("\n"+"="*122)
print("MASTER EXIT COMPARISON  --  RGNT(06-15) EXCLUDED, all 11 exits, same basis")
print("="*122)
print(f"{'Exit':12}{'#Tr':>4}{'Win%':>6}{'AvgR%':>7}{'TotR%':>8}{'MedCap':>8}{'GiveBk':>8}  {'CRVO':>13} {'ATPC':>13}  {'TotR(RGNTin)':>13}")
for r in rows:
    pri=PRIOR.get(r["name"]); pri=f"{pri:.0f}" if pri is not None else "(new)"
    hc=f"{r['cr'][0]:+.0f}%(c{r['cr'][1]:.2f})" if r['cr'][0] is not None else "-"
    ha=f"{r['at'][0]:+.0f}%(c{r['at'][1]:.2f})" if r['at'][0] is not None else "-"
    print(f"{r['name']:12}{r['n']:>4}{r['win']:>6.0f}{r['avg']:>7.1f}{r['tot']:>8.1f}{r['mc']:>8.2f}{r['gb']:>8.1f}  {hc:>13} {ha:>13}  {pri:>13}")

print("\n--- RANK by WIN% ---")
for r in sorted(rows,key=lambda x:-x["win"]): print(f"  {r['name']:12} win {r['win']:.0f}%   medCap {r['mc']:.2f}")
print("--- RANK by MEDIAN CAPTURE ---")
for r in sorted(rows,key=lambda x:-x["mc"]): print(f"  {r['name']:12} medCap {r['mc']:.2f}   win {r['win']:.0f}%")
print("--- RANK by TOTAL RET (monster-sensitive, least robust) ---")
for r in sorted(rows,key=lambda x:-x["tot"]): print(f"  {r['name']:12} tot {r['tot']:.0f}%   (RGNT-in was {PRIOR.get(r['name'],'new')})")

trail=[r for r in rows if r["name"].startswith("TRAIL")]; C=[r for r in rows if r["name"]=="C_2xema9"][0]
bw=max(trail,key=lambda x:x["win"]); bc=max(trail,key=lambda x:x["mc"])
print("\n--- VERDICT (robust metrics, RGNT out) ---")
print(f"  best trailing by win%   : {bw['name']} {bw['win']:.0f}%  vs C {C['win']:.0f}%  -> trailing {'BEATS' if bw['win']>C['win'] else ('TIES' if bw['win']==C['win'] else 'LOSES')}")
print(f"  best trailing by medCap : {bc['name']} {bc['mc']:.2f} vs C {C['mc']:.2f} -> trailing {'BEATS' if bc['mc']>C['mc'] else 'LOSES'}")
print(f"  C total ret {C['tot']:.0f}% vs best-trailing total {max(t['tot'] for t in trail):.0f}%  (totals are monster-driven; least robust)")

print("\n=== PER-RUNNER: CRVO / ATPC / CCTG / CAST under every exit (ret% / capture) ===")
for tgt in ["CRVO","ATPC","CCTG","CAST"]:
    for et in ENTRIES:
        if et[1]==tgt:
            parts=[]
            for name,kind in EXITS:
                r=run1(et,kind); parts.append(f"{name.replace('_',':').split(':')[0]+(':'+name.split('_')[1] if '_' in name else '')[:0]}")
            line=f"{tgt} [{et[0]}]:"
            for name,kind in EXITS:
                r=run1(et,kind); short=name.split('_')[0] if not name.startswith('TRAIL') else name
                line+=f"  {short}={r['ret']:+.0f}%/c{r['cap']:.2f}"
            print(line)
