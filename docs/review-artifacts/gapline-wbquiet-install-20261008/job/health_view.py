"""Runner-local view of the one authorized off-hours heartbeat allowance."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import math
import re
from threading import Thread
from urllib.request import urlopen
from zoneinfo import ZoneInfo


def integer(value):
    if type(value) is int:
        return value
    if type(value) is str and re.fullmatch(r'0|[1-9][0-9]*', value):
        return int(value)
    return None


def true(value):
    return value is True or (type(value) is str and value == 'true')


def admitted_view(payload, now, report):
    # Never modify the provider response or another service's health status.
    value = deepcopy(payload)
    if not isinstance(value, dict) or not isinstance(value.get('services'), list):
        return value
    rows = [row for row in value['services'] if isinstance(row, dict)
            and row.get('service_name') == 'schwab-1m-v2']
    if len(rows) != 1:
        return value
    row = rows[0]
    if row.get('raw_status', row.get('status')) != 'degraded':
        return value
    detail = row.get('details')
    if not isinstance(detail, dict):
        return value
    local = now.astimezone(ZoneInfo('America/New_York'))
    if local.hour < 16:
        return value
    anchor_hour = 16 if local.hour < 20 else 20
    session = 'after_hours' if local.hour < 20 else 'closed'
    try:
        observed = datetime.fromisoformat(str(row.get('observed_at_raw') or row.get('observed_at')).replace('Z', '+00:00'))
        if observed.tzinfo is None or not 0 <= (now - observed).total_seconds() <= 120:
            return value
    except (TypeError, ValueError):
        return value
    age = detail.get('secs_since_last_bar')
    if type(age) is str and re.fullmatch(r'0|[1-9][0-9]*', age):
        age = int(age)
    bound = (local - local.replace(hour=anchor_hour, minute=0, second=0, microsecond=0)).total_seconds() + 300
    count = integer(detail.get('watchlist_size'))
    warmed = integer(detail.get('warmed_size'))
    exceptions = integer(detail.get('loop_exceptions_total'))
    if not (detail.get('data_flow') == 'stalled_offhours_rest_dry'
            and detail.get('market_session') == session
            and detail.get('loop_health') == 'healthy'
            and exceptions == 0
            and true(detail.get('streamer_connected')) and true(detail.get('enabled'))
            and count is not None and count > 0
            and warmed is not None and warmed == count
            and type(age) in (int, float) and math.isfinite(age) and 0 <= age <= bound):
        return value
    report(dict(allowance='STANDING-ALLOWANCE', service='schwab-1m-v2',
                original_status=row.get('status'), original_raw_status=row.get('raw_status'),
                observed_at=observed.isoformat(), details=deepcopy(detail), bar_age_bound=bound))
    row['status'] = row['raw_status'] = 'healthy'
    return value


def load_actual():
    with urlopen('http://127.0.0.1:8100/health', timeout=5) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('health response exceeds bound')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('health response is not an object')
    return value


@contextmanager
def health_view(report, *, loader=load_actual, now=lambda: datetime.now(timezone.utc)):
    """Only deploy_service's health URL changes; native SLA/identity logic stays."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/health':
                self.send_error(404)
                return
            try:
                raw = json.dumps(admitted_view(loader(), now(), report), allow_nan=False).encode()
            except Exception as exc:
                report(dict(health_read='UNREADABLE', error_type=type(exc).__name__))
                self.send_error(502, 'actual health response unreadable')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format, *args):
            return

    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port) + '/health'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=6)
