"""Explicit isolated FIRSTBAR packet job; no bot/OMS/page hook or provider port.

Default is prepare-only with no database connection. Main alone authorizes
execution and supplies a dedicated SIM role/DSN and fresh receipt path. No
migration, table creation, source refresh, automatic retry or LIVE write exists.
"""

import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import stat
import sys


def _sim_engine(dsn_file, expected_role):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    path = Path(dsn_file).resolve(strict=True)
    metadata = path.stat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > 8192):
        raise ValueError("private_bounded_sim_dsn_file_required")
    dsn = path.read_text().strip()
    url = make_url(dsn)
    if (url.drivername != "postgresql+psycopg" or not expected_role
            or url.username != expected_role or not url.database):
        raise ValueError("explicit_dedicated_postgresql_sim_role_required")
    return create_engine(url, pool_size=1, max_overflow=0, pool_timeout=1,
        connect_args={"connect_timeout": 2,
            "options": "-c statement_timeout=750 -c lock_timeout=250 -c timezone=UTC",
            "application_name": "atrsim_firstbar_worker_v1"})


def _write(output, receipt):
    from project_mai_tai.atr_simulation.repository import json_value

    json.dump(json_value(receipt), output, sort_keys=True, separators=(",", ":"), allow_nan=False)
    output.write("\n")
    output.flush()
    os.fsync(output.fileno())


def run_job(args):
    """Fresh process / one job / one cohort execution; no port until validated."""
    from project_mai_tai.atr_simulation.firstbar_cohort_job import load_firstbar_job, prepare_firstbar_job

    # Reserve the immutable receipt before any execution so it cannot fail on an
    # old path only after a commit. Nothing replaces an earlier raw failure.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    receipt = {"status": "STARTED", "phase": "LOAD_JOB", "engine_execution": "UNRUN",
               "qualification": "UNQUALIFIED_REPLAY", "external_provider_ports": "NONE_IMPLEMENTED"}
    engine = None
    failure = None
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        try:
            job = load_firstbar_job(args.job_file, args.job_sha256)
            receipt["phase"] = "PREPARE_LOCAL_EVIDENCE"
            prepared = prepare_firstbar_job(job, args.evidence_root)
            receipt.update(deepcopy(prepared.diagnostics))
            if not args.execute:
                receipt["phase"] = "PREPARE_CLOSED_NO_DATABASE"
            else:
                if prepared.cohort is None:
                    raise ValueError("firstbar_incomplete_cohort_cannot_publish")
                if (prepared.diagnostics["expected_raw_packet_sha256"]
                        != prepared.diagnostics["raw_packet_sha256"]):
                    raise ValueError("firstbar_execute_requires_exact_packet_population_pin")
                if not args.sim_dsn_file or not args.sim_role:
                    raise ValueError("firstbar_execute_requires_explicit_sim_role_and_dsn")
                from project_mai_tai.atr_simulation.firstbar_cohort_job import persist_firstbar_packets
                from project_mai_tai.atr_simulation.firstbar_projection import publish_firstbar_cohort
                from project_mai_tai.atr_simulation.repository import SimulationRepository

                receipt["phase"] = "OPEN_SIM_STORE"
                engine = _sim_engine(args.sim_dsn_file, args.sim_role)
                repository = SimulationRepository(engine)
                receipt["phase"] = "CAPTURE_SIM_INBOX"
                persist_firstbar_packets(repository, prepared.cohort, prepared.packets)
                receipt["phase"] = "PUBLISH_ONE_CANONICAL_COHORT"
                receipt["engine_execution"] = "STARTED_OUTCOME_NOT_YET_CONFIRMED"
                rows = publish_firstbar_cohort(repository, prepared.cohort)
                counts = Counter((row["mode"], row["outcome_kind"]) for row in rows)
                manifest = receipt["publication"]
                manifest.update(publication_state="COMMITTED_OR_IDENTICAL_SOURCE",
                    row_count=len(rows), known_page_outcomes={mode: {
                        kind: counts[(mode, kind)] for kind in ("TRADE", "SKIP", "UNKNOWN")}
                        for mode in manifest["modes"]},
                    commit_verification="TRANSACTION_RETURN_ONLY_FRESH_READBACK_UNRUN")
                receipt.update(status="SIMULATION_PUBLISHED", phase="PUBLISH_RETURNED",
                    engine_execution="simulate_many_once", acceptance="NOT_NATIVE_REFERENCE_OR_INSTALL_ACCEPTANCE")
        except BaseException as error:
            # A connection/commit error may be uncertain. Never delete/retry or
            # leak credentials/SQL parameters through a driver exception string.
            failure = error
            receipt.update(status="ERROR_OR_UNCERTAIN", error_type=type(error).__name__,
                error_code="REDACTED_ERROR_SEE_PHASE_AND_TYPE", automatic_retry=False)
            if receipt.get("publication") is not None and receipt["publication"]["publication_state"] == "UNPUBLISHED":
                if receipt["phase"] in {"PUBLISH_ONE_CANONICAL_COHORT", "PUBLISH_RETURNED"}:
                    receipt["publication"]["publication_state"] = "UNKNOWN_UNTIL_ACTUAL_READBACK"
        finally:
            if engine is not None:
                try:
                    engine.dispose()
                    receipt["store_engine_disposed"] = True
                except BaseException as error:
                    receipt.update(status="ERROR_OR_UNCERTAIN", cleanup_error_type=type(error).__name__,
                        store_engine_disposed=False)
                    if failure is None:
                        failure = error
            else:
                receipt["store_engine_created"] = False
            _write(output, receipt)
        if failure is not None:
            raise failure
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-file", required=True)
    parser.add_argument("--job-sha256", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--sim-dsn-file")
    parser.add_argument("--sim-role")
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        run_job(args)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        # Never print a DB exception (possibly containing credentials/params) to
        # a service journal. The immutable private receipt records phase/type.
        print("FIRSTBAR job failed; inspect its private receipt (details redacted).", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    main()
