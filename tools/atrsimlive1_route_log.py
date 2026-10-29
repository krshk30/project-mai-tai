"""Fresh trader-readable TODAY route evidence; no writes or persistent projector."""
from datetime import datetime,time as daytime,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import re
import stat
import subprocess

LOG=Path('/var/log/project-mai-tai/schwab-1m-v2.log')
ET=ZoneInfo('America/New_York')
MAX_BYTES=64*1024**2
MAX_PACKET=2*1024**2
MAX_LINES=30000


def encoded(value):
    return(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()


def sources(base,now):
    """dateext (no dateyesterday): the post-midnight copy bears rotation date.

    Deployed logrotate config edd2fc81 has copytruncate/dateext/delaycompress.
    Before UTC midnight the current file holds the session. Afterwards use the
    uncompressed rotation-date file AND current (20:00..20:05 ET final refresh).
    No directory/history sweep or compressed-file fallback.
    """
    day=now.astimezone(ET).date()
    begin=datetime.combine(day,daytime(4),ET).astimezone(timezone.utc)
    return ([base.with_name(base.name+'-'+now.astimezone(timezone.utc).strftime('%Y%m%d'))]
            if now.astimezone(timezone.utc).date()>begin.date()else[])+[base]


def clock_epoch():
    response=subprocess.run(['/usr/bin/systemctl','show','project-mai-tai-schwab-1m-v2.service',
                             '-p','MainPID','--value'],capture_output=True,timeout=5,check=True)
    pid=response.stdout.decode().strip()
    if re.fullmatch('[1-9][0-9]{0,9}',pid)is None:
        raise ValueError('ROUTE_V2_PID_UNKNOWN')
    with Path('/proc',pid,'environ').open('rb')as stream:environment=stream.read(65537)
    if len(environment)>65536:raise ValueError('ROUTE_CLOCK_ENV_CAP')
    tz=[part.split(b'=',1)[1].decode('ascii')for part in environment.split(b'\0')if part.startswith(b'TZ=')]
    hostutc=Path('/etc/localtime').read_bytes()==Path('/usr/share/zoneinfo/Etc/UTC').read_bytes()
    if len(tz)>1 or not((tz and tz[0]in('UTC','Etc/UTC'))or(not tz and hostutc)):
        raise ValueError('ROUTE_LOG_CLOCK_UNKNOWN')
    return dict(v2_pid=pid,process_TZ=tz,host_UTC=hostutc,
                original_log_clock='UTC_DEFAULT_LOGGING_LOCALTIME_VERIFIED')


def read_today_routes(*,base=LOG,now=None,epoch=None):
    """One invocation/one bounded readset. Missing facts grant no route.

    epoch injection is a controlled-source seam; the real CLI always obtains
    the actual current v2 process/host clock. No descriptor/permission mutation.
    """
    now=now or datetime.now(timezone.utc)
    if not isinstance(now,datetime)or now.utcoffset()is None:return None,'ROUTE_AWARE_CLOCK_REQUIRED'
    try:
        if not isinstance(base,Path)or not base.is_absolute()or'..'in base.parts:
            raise ValueError('ROUTE_PATH_SHAPE')
        epoch=clock_epoch()if epoch is None else epoch
        if (not isinstance(epoch,dict)
                or epoch.get('original_log_clock')!='UTC_DEFAULT_LOGGING_LOCALTIME_VERIFIED'):
            raise ValueError('ROUTE_LOG_CLOCK_UNKNOWN')
        selected=[];artifacts=[];total=0;seen_records=set();duplicates=0
        paths=sources(base,now);day=now.astimezone(ET).date().isoformat()
        for path in paths:
            if any(p.is_symlink()for p in(path,*path.parents)):raise ValueError('ROUTE_SYMLINK')
            try:fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
            except FileNotFoundError:continue
            with os.fdopen(fd,'rb')as stream:
                before=os.fstat(stream.fileno())
                if(not stat.S_ISREG(before.st_mode)or before.st_uid not in(0,os.geteuid())
                        or before.st_mode&0o022 or before.st_size>MAX_BYTES-total):
                    raise ValueError('ROUTE_FILE_OWNER_MODE_OR_CAP')
                raw=stream.read(before.st_size);after=os.fstat(stream.fileno())
            current=path.stat(follow_symlinks=False)
            if(len(raw)!=before.st_size or after.st_size<before.st_size
                    or(before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino)
                    or(before.st_dev,before.st_ino)!=(current.st_dev,current.st_ino)):
                raise ValueError('ROUTE_CHANGED_OR_ROTATED')
            total+=len(raw);checksum=hashlib.sha256(raw).hexdigest()
            artifacts.append(dict(artifact=str(path),inode=before.st_ino,bytes_read=len(raw),
                                  mtime_ns=before.st_mtime_ns,sha256=checksum,
                                  hash_scope='EXACT_CAPTURED_PREFIX_NOT_LATER_APPENDS'))
            for number,line in enumerate(raw.splitlines(keepends=True),1):
                if b'[ATR-COUNTRY-SELECTED]'not in line:continue
                if len(line)>4096 or not line.endswith(b'\n'):raise ValueError('ROUTE_LINE_CAP_OR_PARTIAL')
                text=line.decode('utf-8',errors='strict')
                stamp=re.match(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3,6}) ',text)
                if stamp is None:raise ValueError('ROUTE_TIMESTAMP_UNKNOWN')
                clock=datetime.strptime(stamp[1],'%Y-%m-%d %H:%M:%S,%f').replace(tzinfo=timezone.utc)
                if clock>now or clock.astimezone(ET).date().isoformat()!=day:continue
                record_hash=hashlib.sha256(line).hexdigest()
                if record_hash in seen_records:duplicates+=1;continue
                if len(selected)>=MAX_LINES:raise ValueError('ROUTE_SELECTED_COUNT_CAP')
                seen_records.add(record_hash)
                selected.append(dict(line_number=number,recorded_at=clock.isoformat(),text=text,
                    raw_line_sha256=record_hash,source_artifact=str(path),source_prefix_sha256=checksum))
        if not artifacts:return None,'ROUTE_SOURCES_ABSENT'
        selected_raw=b''.join(encoded(row)for row in selected)
        if len(selected_raw)>MAX_PACKET:raise ValueError('ROUTE_SELECTED_BYTE_CAP')
        # Top artifact/hash truthfully names one ORIGINAL file. The full readset
        # and per-line source references bind facts from the other original file.
        anchor=artifacts[0]
        metadata=dict(epoch,readset_artifacts=artifacts,selected_lines_sha256=hashlib.sha256(selected_raw).hexdigest(),
            selected_source_refs=[{k:row[k]for k in('line_number','recorded_at','raw_line_sha256','source_artifact','source_prefix_sha256')}for row in selected],
            duplicate_copy_records=duplicates,transport='DIRECT_READ_EACH_RUN_NO_PERSISTENT_PROJECTION')
        packet=dict(schema='ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1',trading_day=day,
                    source=dict(artifact=anchor['artifact'],sha256=anchor['sha256'],asof=now.isoformat(),epoch=metadata),lines=selected)
        if len(encoded(packet))>MAX_PACKET:raise ValueError('ROUTE_PROJECTION_BYTE_CAP')
        return packet,'BOUND_DIRECT_TODAY_LOG_READSET'
    except(OSError,ValueError,UnicodeError,subprocess.SubprocessError):
        return None,'ROUTE_SOURCE_UNREADABLE_OR_INVALID'
