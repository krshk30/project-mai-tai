"""Exact three 8159 method bodies; Main-only CONTROLLED branch-parity fixture.

No original CHOW DB/native/process proof is inferred from this source copy.
"""
from __future__ import annotations
import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from sqlalchemy import select
from sqlalchemy.orm import Session
from project_mai_tai.broker_adapters.protocols import ExitPairReleaseResult
from project_mai_tai.db.models import SystemIncident
from project_mai_tai.oms.service import SERVICE_NAME

# Main installs the same explicit CONTROLLED clock into the candidate module.
def utcnow():
    return datetime(2026, 10, 30, 20, 1, tzinfo=UTC)

class RetainedEod8159:
    async def _release_v2_eod_oco(
        self, acct: str, symbol: str, *, expected_row_id: str
    ) -> ExitPairReleaseResult:
        if not self._is_v2_webull_account(acct):
            result = await self._release_native_oco_for_cw_flip(
                acct, symbol, expected_row_id=expected_row_id
            )
            return ExitPairReleaseResult(outcome=result)

        if await self._legacy_webull_partial_handover_refused(acct, symbol):
            return ExitPairReleaseResult(outcome="unanswerable")

        def _base(session: Session) -> tuple[str, str, str]:
            row = self.store.get_open_managed_position(
                session, broker_account_name=acct, symbol=symbol
            )
            if row is None or str(row.id) != expected_row_id:
                return "", "", ""
            entry = self._find_oco_entry_order(session, acct, symbol)
            return self._oco_exit_base_for_entry(
                entry, broker_account_name=acct, symbol=symbol
            ), str(getattr(entry, "broker_order_id", "") or ""), str(getattr(entry, "id", "") or "")

        try:
            base, entry_oid, entry_identity = await self._run_db(_base, commit=False)
            if not base:
                return ExitPairReleaseResult(outcome="unanswerable")
            release = await self.broker_adapter.release_exit_pair_for_close(
                broker_account_name=acct, symbol=symbol, base_client_order_id=base
            )
            expected_episode = (expected_row_id, entry_identity, base)
            if await self._legacy_webull_partial_handover_refused(
                acct, symbol, release=release, expected=expected_episode,
            ):
                return replace(release, outcome="unanswerable")
            if release.outcome == "resolved_by_fill":
                return replace(release, entry_broker_order_id=entry_oid,
                               exit_base_client_order_id=base)
            confirm = getattr(self.broker_adapter, "confirm_exit_pair_terminal", None)
            if confirm is None:
                return ExitPairReleaseResult(outcome="unanswerable")
            confirmed = await confirm(
                broker_account_name=acct, symbol=symbol, base_client_order_id=base
            )
            if await self._legacy_webull_partial_handover_refused(
                acct, symbol, release=confirmed, expected=expected_episode,
            ):
                return replace(confirmed, outcome="unanswerable")
            return confirmed
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "[OMS-V2-EOD-OCO-TRANSITION] %s %s row=%s outcome=UNKNOWN "
                "reason=broker_read_or_cancel_error",
                acct, symbol, expected_row_id,
            )
            return ExitPairReleaseResult(outcome="unanswerable")

    async def _v2_eod_oco_transition(self) -> None:
        """Cancel and broker-confirm the current row's RTH legs before software takes over."""
        if not bool(getattr(getattr(self, "settings", None), "oms_v2_eod_oco_transition_enabled", False)):
            return
        if not self._v2_eod_oco_transition_due():
            return
        session_day = self._session_day_et()
        armed = getattr(self, "_native_oco_armed_confirmed_at", {})
        resolving = getattr(self, "_native_oco_resolving", {})
        now = utcnow()
        grace_seconds = float(
            getattr(self.settings, "oms_native_oco_resolve_grace_seconds", 90)
        )
        for acct, symbol in list(self._managed_v2_symbols):
            key = (session_day, acct, symbol)
            inflight = self.__dict__.setdefault("_v2_eod_oco_transition_inflight", set())
            if key in inflight:
                continue
            try:
                snapshot = await self._run_db(
                    lambda session: self._read_v2_managed_snapshot(session, acct, symbol, True),
                    commit=False,
                )
            except Exception:
                self.logger.exception(
                    "[OMS-V2-EOD-OCO-TRANSITION] %s %s outcome=UNKNOWN reason=row_unreadable",
                    acct, symbol,
                )
                await self._page_v2_eod_handover_unreadable(acct, symbol)
                continue
            if snapshot is None:
                self._managed_v2_symbols.discard((acct, symbol))
                continue
            if self._v2_eod_oco_transition_rows.get(key) == snapshot.managed_row_id:
                await self._evaluate_v2_managed_exit(acct, symbol)
                continue
            retry_key = (key, snapshot.managed_row_id)
            last_try = self.__dict__.setdefault("_v2_eod_oco_last_try", {}).get(retry_key)
            if last_try is not None and time.monotonic() - last_try < self._EXIT_RESERVATION_RETRY_SECONDS:
                continue
            resolved_at = resolving.get((acct, symbol))
            if resolved_at is not None and (now - resolved_at).total_seconds() <= grace_seconds:
                self.logger.info(
                    "[OMS-V2-EOD-OCO-TRANSITION] %s %s outcome=RESOLUTION_GRACE "
                    "age_s=%.1f grace_s=%.1f; waiting for the broker child fill",
                    acct, symbol, (now - resolved_at).total_seconds(), grace_seconds,
                )
                continue
            inflight.add(key)
            try:
                self.__dict__.setdefault("_v2_eod_oco_last_try", {})[retry_key] = time.monotonic()
                release = await self._release_v2_eod_oco(
                    acct, symbol, expected_row_id=snapshot.managed_row_id
                )
                if release.outcome == "resolved_by_fill":
                    recorded = await self._close_resolved_oco_managed_row(
                        acct, symbol, detail=self._exit_pair_fill_detail(release),
                        expected_row_id=snapshot.managed_row_id,
                    )
                    if not recorded:
                        release = ExitPairReleaseResult(outcome="unanswerable")
                if release.outcome == "released":
                    def _current_row_id(session: Session) -> str:
                        row = self.store.get_open_managed_position(
                            session, broker_account_name=acct, symbol=symbol
                        )
                        return str(row.id) if row is not None else ""

                    try:
                        current_row_id = await self._run_db(
                            _current_row_id, commit=False,
                        )
                    except Exception:
                        release = ExitPairReleaseResult(outcome="unanswerable")
                    else:
                        if current_row_id != snapshot.managed_row_id:
                            self.logger.warning(
                                "[OMS-V2-EOD-OCO-TRANSITION] %s %s old_row=%s current_row=%s "
                                "outcome=POSITION_CHANGED_DURING_BROKER_READ; no handover latch",
                                acct, symbol, snapshot.managed_row_id, current_row_id or "-",
                            )
                            continue
                await self._run_db(
                    lambda session: self._sync_v2_eod_release_incident(
                        session, acct, symbol, snapshot.managed_row_id, release
                    ),
                    commit=True,
                )
                if release.outcome == "resolved_by_fill":
                    continue
                if release.outcome != "released":
                    self.logger.error(
                        "[OMS-V2-EOD-OCO-TRANSITION] %s %s row=%s outcome=%s "
                        "software_sell=BLOCKED incident_route=INC1",
                        acct, symbol, snapshot.managed_row_id, release.outcome,
                    )
                    continue
            finally:
                inflight.discard(key)
            await self._carry_v2_native_oco_floor_at_handover(acct, symbol)
            self._v2_eod_oco_transition_rows[key] = snapshot.managed_row_id
            self._v2_eod_oco_transitioned.add(key)
            self.__dict__.get("_v2_native_oco_high_bid", {}).pop((acct, symbol), None)
            # Drop any live broker-armed confirmation / resolution-grace entry so the ladder is not
            # deferred waiting for either to lapse. The stand-down short-circuit (keyed on this same
            # latch) is the durable guard; these pops just avoid stale log churn from the sync refresh.
            armed.pop((acct, symbol), None)
            resolving.pop((acct, symbol), None)
            self.logger.info(
                "[OMS-V2-EOD-OCO-TRANSITION] %s %s row=%s broker_legs=CONFIRMED_GONE "
                "software_exit=ENABLED",
                acct, symbol, snapshot.managed_row_id,
            )
            # Intake stood down while the broker pair owned this quote. After confirmed
            # handover, use the existing bound/fresh capture, never bypass its admission.
            quote = self._latest_quotes_by_symbol.get(symbol)
            if quote:
                self._capture_cw_target_decision(acct, symbol, quote)
            await self._evaluate_v2_managed_exit(acct, symbol)

    def _sync_v2_eod_release_incident(
        self, session: Session, acct: str, symbol: str, row_id: str,
        release: ExitPairReleaseResult,
    ) -> None:
        if release.outcome in {"released", "resolved_by_fill"}:
            incidents = session.scalars(
                select(SystemIncident).where(
                    SystemIncident.service_name == SERVICE_NAME,
                    SystemIncident.status == "open",
                )
            ).all()
            for incident in incidents:
                payload = incident.payload if isinstance(incident.payload, dict) else {}
                if (
                    payload.get("source") == self._EXIT_RELEASE_INCIDENT_SOURCE
                    and payload.get("broker_account_name") == acct
                    and payload.get("symbol") == symbol
                    and payload.get("managed_row_id") == row_id
                    and not payload.get("unaccounted_child_state")
                ):
                    incident.status = "closed"
                    incident.closed_at = utcnow()
        row = self.store.get_open_managed_position(
            session, broker_account_name=acct, symbol=symbol
        )
        if row is not None and str(row.id) == row_id:
            self._sync_exit_release_incident(
                session, row, release, reason="V2_EOD_OCO_TRANSITION", protective=False
            )
