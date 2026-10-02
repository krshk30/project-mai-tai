"""Stream existing v2 log probes once, low priority; never write production files."""
import gzip
import json
import time
from datetime import UTC, datetime
from pathlib import Path

print(json.dumps({'type': 'metadata', 'at': str(datetime.now(UTC)),
                  'source': '/var/log/project-mai-tai/schwab-1m-v2.log*'}), flush=True)
for path in sorted(Path('/var/log/project-mai-tai').glob('schwab-1m-v2.log*')):
    before = path.stat()
    opener = gzip.open if path.suffix == '.gz' else open
    count = 0
    with opener(path, 'rt', errors='replace') as stream:
        for line_number, line in enumerate(stream, 1):
            if len(line) > 65536:
                raise ValueError('unexpected log line size')
            if '[V2-ATR-PROBE]' in line:
                print(json.dumps({'type': 'probe', 'path': str(path),
                                  'line_number': line_number, 'line': line.rstrip()}))
                count += 1
            if line_number % 10000 == 0:
                time.sleep(.02)
    after = path.stat()
    print(json.dumps({'type': 'file', 'path': str(path), 'probe_count': count,
                      'size_before': before.st_size, 'size_after': after.st_size,
                      'inode_before': before.st_ino, 'inode_after': after.st_ino}), flush=True)
print(json.dumps({'type': 'complete', 'at': str(datetime.now(UTC))}), flush=True)
