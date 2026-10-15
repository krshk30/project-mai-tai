"""Paper-warning pause reporting only; never classifies harm or controls A."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
import math
from typing import Mapping
from uuid import NAMESPACE_URL, uuid5

from project_mai_tai.db.models import SystemIncident

SOURCE = "momwarn_pause_health"
SERVICE = "market-data-gateway"
PAPER_ACCOUNT = "paper:momentum"
PAPER_STRATEGIES = ("momentum_30s_warn", "momentum_60s_warn")
MAX_SAMPLE_AGE_NS = 20_000_000_000
MAX_PAUSE_BYTES = 8192
logger = logging.getLogger(__name__)


def readable_pause_snapshot(metrics: Mapping | None, *, now_ns: int) -> dict | None:
    """Accept only the current bounded gateway sample; absent data stays UNKNOWN."""
    if not isinstance(metrics, Mapping):
        return None
    at, generation, instance = (metrics.get(k) for k in ('observed_ns','generation','gateway_instance_id'))
    if (type(metrics.get('version')) is not int or metrics.get('version') != 1 or metrics.get('source') != SERVICE
            or type(at) is not int or not 0 < at <= now_ns or now_ns-at > MAX_SAMPLE_AGE_NS
            or type(generation) is not int or not 0 <= generation < 2**63
            or not isinstance(instance, str) or not 0 < len(instance) <= 128):
        return None
    snapshot = metrics.get('pause_state')
    if not isinstance(snapshot, dict) or not snapshot:
        return None
    try:
        encoded = json.dumps(snapshot, allow_nan=False, separators=(',',':'))
        if len(encoded.encode()) > MAX_PAUSE_BYTES:
            return None
        return json.loads(encoded)  # Detached from mutable policy state.
    except (TypeError, ValueError, RecursionError):
        return None


def approximate_pause_utc_ns(monotonic_ns: int | None, metrics: Mapping) -> int | None:
    """Paired-observation offset estimate ONLY, never an exact historic UTC clock."""
    utc, mono = metrics.get('observed_ns'), metrics.get('observed_monotonic_ns')
    if (type(monotonic_ns) is not int or type(utc) is not int or type(mono) is not int
            or not 0 <= monotonic_ns <= mono or utc <= 0):
        return None
    estimated = utc + monotonic_ns-mono
    return estimated if estimated > 0 else None


class PauseIncidentStore:
    """Ordinary SystemIncident warning rows, through a caller-supplied timed factory."""
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def record(self, sample: dict) -> int:
        # Thresholds/classification belong to WarningPausePolicy, not this reporter.
        alerts = sample['pause_state']['alert_reasons']
        if not alerts:
            return 0
        at = datetime.fromtimestamp(sample['observed_ns']/1e9, UTC)
        with self.session_factory() as session:
            for kind in alerts:
                episode = sample['observed_ns']//3_600_000_000_000
                if kind == 'pause_over_10min':
                    opened = [row for row in sample['pause_state'].get('history', [])
                              if isinstance(row,dict) and 'end_ns' in row and row['end_ns'] is None
                              and type(row.get('start_ns')) is int and row['start_ns'] >= 0]
                    if len(opened) != 1:
                        continue  # Missing stable episode identity is UNKNOWN, not an invented start.
                    episode = f'monotonic:{opened[0]["start_ns"]}'
                ident = uuid5(NAMESPACE_URL, f'{SOURCE}:{sample["gateway_instance_id"]}:{kind}:{episode}')
                incident = session.get(SystemIncident, ident)
                payload = {'source':SOURCE, 'scope':'PAPER_WARNING_OBSERVABILITY_ONLY',
                    'account_name':PAPER_ACCOUNT, 'strategy_codes':list(PAPER_STRATEGIES),
                    'gateway_instance_id':sample['gateway_instance_id'], 'generation':sample['generation'],
                    'observed_ns':sample['observed_ns'], 'pause_state':sample['pause_state'],
                    'alert_kind':kind, 'times_scope':'POLICY_MONOTONIC_ONLY_NOT_UTC',
                    'observed_monotonic_ns':sample.get('observed_monotonic_ns'),
                    'dedup_scope':('gateway_instance_alert_reason_monotonic_episode' if kind=='pause_over_10min'
                                   else 'gateway_instance_alert_reason_observed_utc_hour')}
                if incident is None:
                    session.add(SystemIncident(id=ident, service_name=SERVICE, severity='warning',
                        title=f'MOMLATCH1: {kind} on paper warning feed', status='open',
                        payload=payload, opened_at=at))
                elif incident.status != 'closed':
                    incident.payload = payload
                # Respect ordinary operator closure; never reopen an acknowledged episode.
            session.commit()
        return len(alerts)


class MomwarnPauseIncidentReporter:
    """One DB worker + one latest pending sample, invoked from the 10s metrics task.

    Main supplies pause_state = Tesla policy.metrics(). observe() never
    waits for SQL and cannot pause/re-arm A or create quote-path work. A timeout
    retains the SAME in-flight job (to_thread cancellation cannot stop SQL).
    The caller must supply a connection/statement/pool-timeout session factory.
    """
    def __init__(self, store: PauseIncidentStore, *, wait_budget_seconds: float = 2):
        if not math.isfinite(wait_budget_seconds) or wait_budget_seconds <= 0:
            raise ValueError('positive finite wait budget required')
        self.store = store
        self.wait_budget_seconds = wait_budget_seconds
        self._pending = None
        self._task = None
        self._closing = False
        self.timeouts = 0
        self.failures = 0
        self._identity = None

    async def observe(self, metrics: Mapping, *, now_ns: int) -> str:
        snapshot = readable_pause_snapshot(metrics, now_ns=now_ns)
        if self._closing or snapshot is None:
            return 'UNKNOWN'
        alerts = snapshot.get('alert_reasons')
        if (not isinstance(alerts,list) or len(alerts)>8
                or any(not isinstance(reason,str) or not 0<len(reason)<=128 for reason in alerts)):
            return 'UNKNOWN'
        identity = (metrics['gateway_instance_id'], metrics['generation'], metrics['observed_ns'])
        if self._identity is not None and (identity[0] != self._identity[0]
                or identity[1] < self._identity[1] or identity[2] < self._identity[2]):
            return 'UNKNOWN'
        self._identity = identity
        snapshot['alert_reasons'] = list(dict.fromkeys(alerts))
        if not snapshot['alert_reasons']:
            self._pending = None  # A newer clean metric supersedes queued, not already-running, SQL.
            return 'NO_ALERT'
        self._pending = ({key:metrics[key] for key in ('observed_ns','generation','gateway_instance_id')}
                         | {'pause_state':snapshot, 'observed_monotonic_ns':metrics.get('observed_monotonic_ns')})
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._drain())
        return 'QUEUED'

    async def _drain(self) -> None:
        while self._pending is not None and not self._closing:
            sample = self._pending
            self._pending = None
            worker = asyncio.create_task(asyncio.to_thread(self.store.record, sample))
            try:
                try:
                    await asyncio.wait_for(asyncio.shield(worker), self.wait_budget_seconds)
                except TimeoutError:
                    self.timeouts += 1
                    logger.warning('[MOMLATCH1-INCIDENT] write_pending=1 no_second_worker=1')
                    await worker
            except asyncio.CancelledError:
                # Keep the worker awaited: cancellation is not proof its SQL stopped.
                await asyncio.shield(worker)
                raise
            except Exception:
                self.failures += 1
                logger.exception('[MOMLATCH1-INCIDENT] write_failed=1 metrics_and_A_unchanged=1')

    async def close(self, *, wait_budget_seconds: float = 2) -> bool:
        self._closing = True
        self._pending = None
        if self._task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), wait_budget_seconds)
            return True
        except TimeoutError:
            return False
