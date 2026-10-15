#!/usr/bin/env python3
"""Oct12 retained bar/probe provenance only; no provider, token or cache mutation."""
from datetime import datetime, UTC
import hashlib
import json
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo

ET=ZoneInfo("America/New_York")
START=int(datetime(2026,10,12,tzinfo=ET).timestamp()*1000)
SEVEN=START+7*3600*1000
END=START+24*3600*1000
PROBE=re.compile(r"\[V2-MACD-PROBE\] sym=(\S+) ts_ms=(\d+) ")
STAMP=re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3}) ")
WARMUP=re.compile(r"schwab_v2 warmup feed for (\S+): (\d+) bars from (\d+)\.\.(\d+)")


def scan(path):
    info=path.stat()
    if not path.is_file() or path.is_symlink() or info.st_size>32*1024*1024:
        raise ValueError("retained regular log32MiB bound")
    hashed=hashlib.sha256(); offset=number=0; rows={}; before=total=0; arrivals=[]
    with path.open("rb") as stream:
        while offset<info.st_size:
            raw=stream.readline(min(65537,info.st_size-offset))
            if len(raw)>65536:
                raise ValueError("retained line bound")
            number+=1; hashed.update(raw)
            text=raw.decode(errors="replace")
            match=PROBE.search(text); stamp=STAMP.match(text)
            if stamp and "2026-10-12 11:" in stamp[1] and stamp[1][14:16]<"20":
                warmup=WARMUP.search(text)
                marker=next((tag for tag in ("V2-LINE-RESTORE","V2-LINE-SOURCE-STATE","V2-REST-WARMED")
                             if "["+tag+"]" in text),None)
                if (warmup or marker) and len(arrivals)<40:
                    fields=dict(re.findall(r"\b(sym|state|reason|current_bar_ms|bars|outcome|live_append|entry_allowed|n_bars)=([^ ]+)",text))
                    if warmup:
                        fields.update(symbol=warmup[1],bars=int(warmup[2]),first_bar_ms=int(warmup[3]),last_bar_ms=int(warmup[4]))
                    arrivals.append(dict(original_log_stamp_utc=stamp[1]+"."+stamp[2],
                                         marker=marker or "schwab_v2 warmup feed",line=number,byte_offset=offset,fields=fields))
            if match and stamp and START<=int(match[2])<END:
                symbol=match[1]; bar=int(match[2]); total+=1; before+=bar<SEVEN
                evidence=dict(symbol=symbol,bar_start_ms=bar,
                    bar_start_et=datetime.fromtimestamp(bar/1000,UTC).astimezone(ET).isoformat(),
                    original_log_stamp_utc=stamp[1]+"."+stamp[2],line=number,byte_offset=offset)
                if symbol not in rows or bar<rows[symbol]["bar_start_ms"]:
                    rows[symbol]=evidence
                if len(rows)>256:
                    raise ValueError("symbol bound")
            offset+=len(raw)
    return dict(path=str(path),inode=info.st_ino,captured_prefix_bytes=info.st_size,
                captured_prefix_sha256=hashed.hexdigest(),file_grew_during_read=path.stat().st_size!=info.st_size,
                oct12_probe_records=total,before0700_probe_records=before,
                retained_0700_to0720_source_observations=arrivals,
                earliest_per_symbol=sorted(rows.values(),key=lambda x:(x["bar_start_ms"],x["symbol"])))


def history():
    query="""
BEGIN READ ONLY;
SET LOCAL statement_timeout='5s';
SELECT row_to_json(x) FROM (
 SELECT symbol,count(*) AS rows, min(bar_time) AS earliest_bar_utc,
 min(created_at) AS first_retained_created_at_utc,
 min(bar_time AT TIME ZONE 'America/New_York') AS earliest_bar_et,
 count(*) FILTER(WHERE bar_time<'2026-10-12 11:00:00+00') AS before0700_rows,
 count(*) FILTER(WHERE bar_time='2026-10-12 11:00:00+00') AS at0700_rows,
 array_agg(DISTINCT source) AS retained_source_labels
 FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2' AND interval_secs=60
 AND bar_time>='2026-10-12 04:00:00+00' AND bar_time<'2026-10-13 04:00:00+00'
 GROUP BY symbol ORDER BY earliest_bar_utc,symbol LIMIT 257
) x;
ROLLBACK;
"""
    result=subprocess.run(["sudo","-n","-u","postgres","psql","-X","-At","-v","ON_ERROR_STOP=1","project_mai_tai"],
        input=query,text=True,capture_output=True,timeout=8)
    if result.returncode or len(result.stdout)>131072:
        raise ValueError("read-only history query failed/bound")
    rows=[json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    if len(rows)>256:
        raise ValueError("history symbol bound")
    return rows


def first_bars():
    query="""
BEGIN READ ONLY;
SET LOCAL statement_timeout='5s';
SELECT row_to_json(x) FROM (
 SELECT id,symbol,bar_time,created_at,updated_at,source,interval_secs
 FROM strategy_bar_history WHERE strategy_code='schwab_1m_v2'
 AND interval_secs=60 AND bar_time='2026-10-12 11:00:00+00'
 ORDER BY symbol LIMIT 257
) x;
ROLLBACK;
"""
    result=subprocess.run(["sudo","-n","-u","postgres","psql","-X","-At","-v","ON_ERROR_STOP=1","project_mai_tai"],
        input=query,text=True,capture_output=True,timeout=8)
    if result.returncode or len(result.stdout)>131072:
        raise ValueError("read-only first-bar query failed/bound")
    rows=[json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    if len(rows)>256:
        raise ValueError("first-bar symbol bound")
    return rows


def main():
    result=dict(mode="RETAINED_ONLY_NO_PROVIDER_NO_REFRESH",observed_at_utc=datetime.now(UTC).isoformat(),
                day_et="2026-10-12",source_scope="actual retained strategy bars/evaluation probes; not raw broker response or future-day entitlement")
    result["logs"]=[scan(Path("/var/log/project-mai-tai")/name)
                    for name in ("schwab-1m-v2.log-20261013","schwab-1m-v2.log")]
    result["history"]=history()
    result["actual_0700_rows"]=first_bars()
    result["retained_before0700_rows"]=sum(row["before0700_rows"] for row in result["history"])
    result["future_first_bar_guarantee"]="NOT_PROVEN_BY_ONE_RETAINED_DAY"
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()
