"""Single runtime source mutation per isolated pytest process; no file rewrites."""
import inspect
import os
import textwrap

from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.oms.mirror_retained_hold import MirrorRetainedHoldMixin


MUTATIONS = {
    "tick_sql": (MirrorRetainedHoldMixin, "_mirrorhold_schedule",
        'if not self._mirrorhold_enabled():',
        'self._mirrorhold_prepare_queue(symbol, None)\n    if not self._mirrorhold_enabled():'),
    "publish_before_commit": (MirrorRetainedHoldMixin, "_mirrorhold_cache_after_commit",
        'session.info[pending_key][key] = deepcopy(data)',
        'session.info[pending_key][key] = deepcopy(data)\n    publish(session)'),
    "rollback_publish": (MirrorRetainedHoldMixin, "_mirrorhold_cache_after_commit",
        'def rollback(_session):\n            _session.info[pending_key].clear()',
        'def rollback(_session):\n            pass'),
    "stale_revision": (MirrorRetainedHoldMixin, "_mirrorhold_cache_after_commit",
        'if revisions.get(owner, -1) > snapshot["revision"]:', 'if False:'),
    "symbol_isolation": (MirrorRetainedHoldMixin, "_mirrorhold_schedule",
        'tuple(self._mirrorhold_cache().get(symbol, {}).items())',
        'tuple(pair for bucket in self._mirrorhold_cache().values() for pair in bucket.items())'),
    "worker_dedup": (OmsRiskService, "_schedule_symbol_tick_work", 'if key in tasks:', 'if False:'),
    "revision_budget": (MirrorRetainedHoldMixin, "_mirrorhold_schedule",
        'and (key, data["revision"]) not in self.__dict__.get("_mirrorhold_tick_attempts", set())', ''),
    "retirement_phase": (MirrorRetainedHoldMixin, "_mirrorhold_prepare_queue",
        'if reason or data["phase"] != "held" or symbol is None:',
        'if reason or symbol is None:'),
    "shutdown_invalidate": (MirrorRetainedHoldMixin, "_mirrorhold_evaluate_off_loop",
        'if self.__dict__.get("_symbol_tick_work_closing", False):', 'if False:'),
    "hold_log": (MirrorRetainedHoldMixin, "_nfq_pre_submit",
        'self._nfq_log(event, "held", "no_fresh_quote")', 'pass'),
    "sync_refresh": (OmsRiskService, "sync_broker_orders",
        'await self._refresh_drift_working_cache()', 'pass'),
    "drift_revalidate": (OmsRiskService, "_dispatch_drift_cancel",
        'candidates = await self._run_db(\n        lambda session: self._collect_drift_cancel_candidates(\n            session, symbol, quote, tolerance_dollars\n        ),\n        commit=False,\n    )',
        'candidates = list(self._drift_working_by_symbol.get(symbol, ()))'),
    "dirty_wakeup": (OmsRiskService, "_schedule_symbol_tick_work", 'if key in dirty:', 'if False:'),
}


def pytest_configure(config):
    name = os.environ["HOTFIX1_MUTATION"]
    cls, method, old, new = MUTATIONS[name]
    original = getattr(cls, method)
    source = textwrap.dedent(inspect.getsource(original))
    if source.count(old) != 1:
        raise RuntimeError(f"mutation {name} anchor count {source.count(old)}")
    namespace = {}
    exec(compile(source.replace(old, new), f"<HOTFIX1 {name}>", "exec"), original.__globals__, namespace)
    setattr(cls, method, namespace[method])
