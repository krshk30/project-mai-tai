"""Pinned retained-packet job -> existing causal assembler -> Gibbs inputs.

No strategy, entry, allocation, body, or exit algorithm is implemented here.
Missing completed-bar publication evidence cannot be replaced by DB created_at
or a reconstructed indicator. Historical reference reconstruction is a distinct
Gibbs input lineage; this retained-causal worker never silently grants that proof.
"""

from dataclasses import dataclass
from datetime import date, datetime, time as day_time
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo


JOB_VERSION = "ATRSIM_FIRSTBAR_COHORT_JOB_V1"
PACKET_VERSION = "ATRSIM_RAW_PACKET_BUNDLE_V1"
BAR_VERSION = "ATRSIM_COMPLETED_FLIP_BAR_V1"
CUTOFF_VERSION = "ATRSIM_ENTRY_CUTOFF_V1"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PACKETS = 100000
ASSEMBLER_VERSION = "ATRSIM1_CAUSAL_COHORT_V2"
ASSEMBLER_PINS = {
    "cohort_assembler.py": "c5e52fd771181aca06655a3e63f6c7612bfa57dd13a06be86b3ba118303fe7da",
    "cohort_provenance.py": "dbf3c17871ccdfbf473d8de796aa898ff271e3d5fc8228ca7d97c12a1e32353b",
    "cohort_sources.py": "caf33aec15dc6a34b8eed65832df324e8245ce0fac0768aaa4447b70bb1c57e2",
    "policy_io.py": "ed407a2ddb3f28cd50c17fba8301e455f3862fb51d05eb2902272af697680737",
}
SEED_PINS = {
    "firstbar_reference.py": "b984c5268f5d2f4be0d00302573650251e3989edbe67935e7388d71bc91d3ae4",
    "firstbar_ui_reader.py": "927a7e77b8512c5bb2fc93cb8efd29ef900d05c6a58bf61f8b825a5d2e3d01b1",
}


class MissingFirstBarOperand(ValueError):
    """Finite per-episode UNKNOWN; never a synthetic input or affirmed skip."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_firstbar_job_json_key")
        value[key] = item
    return value


def _constant(value):
    raise ValueError("nonfinite_firstbar_job_json_constant")


def _json(raw):
    return json.loads(raw, parse_float=str, parse_constant=_constant, object_pairs_hook=_unique_object)


class EvidenceFiles:
    """One bounded contained immutable byte cache, not a completeness oracle."""

    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("firstbar_evidence_directory_required")
        self.raw, self.total = {}, 0

    def read(self, reference):
        if (not isinstance(reference, dict) or reference.get("pointer") != "$"
                or not isinstance(reference.get("artifact"), str)
                or not isinstance(reference.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", reference["sha256"]) is None
                or reference.get("kind") not in {"RETAINED", "CONTROLLED", "DERIVED", "REFETCHED_SAME_ENDPOINT"}):
            raise ValueError("whole_pinned_firstbar_evidence_reference_required")
        path = Path(reference["artifact"])
        path = (path if path.is_absolute() else self.root / path).resolve(strict=True)
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ValueError("firstbar_evidence_path_outside_root")
        if path not in self.raw:
            with path.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES or self.total + len(raw) > MAX_TOTAL_BYTES:
                raise ValueError("firstbar_evidence_byte_budget_exceeded")
            self.raw[path] = raw
            self.total += len(raw)
        raw = self.raw[path]
        if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ValueError("firstbar_evidence_hash_mismatch")
        return raw

    def document(self, reference):
        return _json(self.read(reference))

    def verify_tree(self, value, *, depth=0):
        if depth > 64:
            raise ValueError("firstbar_evidence_tree_depth_exceeded")
        if isinstance(value, dict):
            if {"artifact", "sha256", "pointer", "kind"} <= value.keys():
                # Hash the actual file even if the older plan points into it.
                # This does not certify the semantic meaning of its pointer.
                self.read({**value, "pointer": "$"})
            for item in value.values():
                self.verify_tree(item, depth=depth + 1)
        elif isinstance(value, list):
            for item in value:
                self.verify_tree(item, depth=depth + 1)


@dataclass(frozen=True)
class FirstBarJob:
    raw: bytes
    sha256: str


@dataclass(frozen=True)
class PreparedFirstBarJob:
    cohort: object | None
    packets: tuple
    diagnostics: dict


def persist_firstbar_packets(repository, cohort, packets):
    """Keep full raw keys in payload, bounded stable keys in SQL stream_id64.

Inherited retained packet keys can be90 chars for account identity or70 for a
proof; they must not be truncated into the stream_id column. Changed payload at
the same logical key remains a repository conflict, not a different event.
"""
    from .repository import digest

    stream = "retained-firstbar:" + cohort.source_id
    consumer = "firstbar:" + digest(cohort.source_id)
    prior = repository.checkpoint(consumer)
    events = [{"stream": stream, "stream_id": digest(packet["key"]),
               "fields": {"data": json.dumps(packet, sort_keys=True, separators=(",", ":"), allow_nan=False)}}
              for packet in packets]
    return repository.capture(consumer, prior["revision"] if prior else 0,
        events, {stream: digest(packets)}, issues=["RAW_PACKETS_NOT_COVERAGE_OR_NATIVE_ASOF_PROOF"])


def load_firstbar_job(path, expected_sha256):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if (len(raw) > MAX_FILE_BYTES or not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or hashlib.sha256(raw).hexdigest() != expected_sha256):
        raise ValueError("bounded_firstbar_job_hash_mismatch")
    return FirstBarJob(raw, expected_sha256)


def _source_reference(t, value, cutoff, *, latest=None):
    from .cohort_assembler import integer

    if value.get("asof_ns") is None:
        raise MissingFirstBarOperand("UNKNOWN_FIRSTBAR_EVIDENCE_AVAILABILITY")
    asof = integer(value["asof_ns"], minimum=1)
    if asof > cutoff or (latest is not None and asof > latest):
        raise MissingFirstBarOperand("UNKNOWN_FUTURE_OR_LATE_FIRSTBAR_EVIDENCE")
    return t.SourceRef(value["artifact"], value["sha256"], value["pointer"],
                       t.EvidenceKind(value["kind"]), asof)


def verify_firstbar_assembler():
    root = Path(__file__).resolve().parent
    for name, checksum in ASSEMBLER_PINS.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("firstbar_assembler_source_pin_mismatch:" + name)
        module = sys.modules.get(__package__ + "." + Path(name).stem)
        if module is not None and Path(module.__file__).resolve() != root / name:
            raise ValueError("firstbar_assembler_import_root_changed:" + name)


def _reference_seed(files, operand):
    """Byte-bind reference metadata, not native fills or executable case links."""
    if operand is None:
        return None, (), None
    if (not isinstance(operand, dict) or set(operand) != {"root"}
            or not isinstance(operand["root"], str) or not operand["root"]):
        raise ValueError("firstbar_reference_seed_root_required_not_descriptor_or_boolean")
    module_root = Path(__file__).resolve().parent
    for name, checksum in SEED_PINS.items():
        if hashlib.sha256((module_root / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("firstbar_reference_source_pin_mismatch:" + name)
        module = sys.modules.get(__package__ + "." + Path(name).stem)
        if module is not None and Path(module.__file__).resolve() != module_root / name:
            raise ValueError("firstbar_reference_import_root_changed:" + name)
    from .firstbar_reference import bind_reference_seed, reviewed_173_manifest
    from .firstbar_ui_reader import reference_seed_binding
    from .repository import json_value

    root = Path(operand["root"])
    root = (root if root.is_absolute() else files.root / root).resolve(strict=True)
    if not root.is_relative_to(files.root) or not root.is_dir():
        raise ValueError("firstbar_reference_seed_root_outside_evidence_root")
    manifest = reviewed_173_manifest(str(root))
    refs = (manifest.csv_source, manifest.split_source, manifest.selector_source)
    # The canonical metadata mapper hashes CSV/split; hash the actual selector
    # bytes too. Merely knowing its published SHA is not file verification.
    raw = tuple(files.read({**json_value(ref), "pointer": "$"}) for ref in refs)
    seed = bind_reference_seed(raw[0], raw[1], manifest=manifest)
    descriptor = reference_seed_binding(seed)
    return descriptor, refs, {"selected_reference_signals": len(seed.cases),
        "reference_exclusions": len(seed.reference_exclusions),
        "canonical_case_link_status": "UNKNOWN_NOT_PROVED_BY_REFERENCE_METADATA",
        "economic_reproduction": "UNRUN_NOT_REFERENCE_ARITHMETIC"}


def _identity(value):
    from .firstbar_projection import IDENTITY_FIELDS
    from .repository import digest

    if (not isinstance(value, dict) or set(value) != set(IDENTITY_FIELDS)
            or any(not isinstance(value[field], str) or not value[field] for field in IDENTITY_FIELDS)):
        raise ValueError("exact_firstbar_job_identity_required")
    return digest(value)


def _packets(files, descriptors):
    from .repository import digest

    if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= 256:
        raise ValueError("bounded_firstbar_packet_files_required")
    packets, seen = [], set()
    for descriptor in descriptors:
        document = files.document(descriptor)
        if (not isinstance(document, dict) or document.get("version") != PACKET_VERSION
                or not isinstance(document.get("packets"), list)):
            raise ValueError("versioned_firstbar_packet_bundle_required")
        for packet in document["packets"]:
            if (not isinstance(packet, dict) or not isinstance(packet.get("key"), str)
                    or not packet["key"] or packet["key"] in seen or not isinstance(packet.get("row"), dict)
                    or packet.get("kind") not in {"trade", "quote", "bar", "account_identity", "snapshot"}):
                raise ValueError("unique_firstbar_source_packet_required")
            seen.add(packet["key"])
            packets.append(packet)
            if len(packets) > MAX_PACKETS:
                raise ValueError("firstbar_packet_population_budget_exceeded")
    return tuple(packets), digest(packets)


def _completed_input(binding, episode, indexed, files, cutoff, t):
    from .cohort_assembler import integer, price, timestamp_ns
    from .firstbar import CompletedFlipBar
    from .firstbar_replay import FirstBarReplayInput
    from .repository import digest, json_value

    identity = json_value(episode.identity)
    if binding.get("identity") != identity:
        raise ValueError("firstbar_binding_identity_mismatch")
    packet = indexed.get(binding.get("bar_packet_key"))
    if packet is None:
        raise MissingFirstBarOperand("UNKNOWN_COMPLETED_FLIP_BAR_PACKET")
    if packet["kind"] != "bar" or digest(packet) != binding.get("bar_packet_sha256"):
        raise ValueError("firstbar_completed_packet_binding_mismatch")
    row = packet["row"]
    if row.get("bar_time") is None or any(row.get(field) in (None, "") for field in ("high_price", "close_price")):
        raise MissingFirstBarOperand("UNKNOWN_COMPLETED_FLIP_BAR_PRICES_OR_CLOCK")
    start = timestamp_ns(row["bar_time"])
    if (packet["key"] != "bar:" + str(row.get("id"))
            or row.get("symbol") != episode.identity.symbol or row.get("strategy_code") != episode.identity.strategy
            or integer(row.get("interval_secs"), minimum=1) != 60
            or episode.buy_flip.bar_start_ns is None or start != episode.buy_flip.bar_start_ns):
        raise ValueError("firstbar_completed_bar_identity_or_interval_mismatch")
    proof_ref = binding.get("publication_source")
    if not isinstance(proof_ref, dict):
        raise MissingFirstBarOperand("UNKNOWN_COMPLETED_BAR_PUBLICATION")
    proof = files.document(proof_ref)
    if (not isinstance(proof, dict) or proof.get("version") != BAR_VERSION
            or proof.get("basis") != "RECORDED_PUBLICATION" or proof.get("identity") != identity
            or proof.get("packet_key") != packet["key"] or proof.get("packet_sha256") != digest(packet)
            or proof_ref.get("kind") not in {"RETAINED", "CONTROLLED"}):
        raise ValueError("firstbar_recorded_publication_binding_required")
    available = integer(proof.get("available_ns"), minimum=1)
    if available < start + 60_000_000_000 or available > cutoff:
        raise MissingFirstBarOperand("UNKNOWN_COMPLETED_BAR_AVAILABILITY")
    source = _source_reference(t, proof_ref, cutoff, latest=available)
    traces = proof.get("trace_sources")
    if not isinstance(traces, list) or not 1 <= len(traces) <= 32:
        raise MissingFirstBarOperand("UNKNOWN_COMPLETED_BAR_PUBLICATION_TRACE")
    for trace in traces:
        files.read(trace)
        _source_reference(t, trace, cutoff, latest=available)
        if trace["kind"] != proof_ref["kind"]:
            raise ValueError("firstbar_publication_trace_kind_mismatch")
    complete = proof.get("complete")
    if complete is not None and type(complete) is not bool:
        raise ValueError("three_state_firstbar_completeness_required")
    # Only actual stored indicators may supply the line/state; not proof flags.
    indicators = row.get("indicators")
    indicators = indicators if isinstance(indicators, dict) else {}
    line = indicators.get("atr_trail")
    line = None if line in (None, "") else price(line)
    state = indicators.get("atr_state")
    state = state if state in ("long", "short") else None
    if (line is None or state is None or proof.get("gaps") != []
            or row.get("created_at") is None or timestamp_ns(row["created_at"]) > available):
        complete = None
    bar = CompletedFlipBar(episode.identity.symbol, start, available,
        price(row["high_price"]), price(row["close_price"]), line, state, complete, source)

    cutoff_ref = binding.get("entry_cutoff_source")
    if not isinstance(cutoff_ref, dict):
        raise MissingFirstBarOperand("UNKNOWN_DECLARED_SIM_ENTRY_CUTOFF")
    entry = files.document(cutoff_ref)
    if (not isinstance(entry, dict) or entry.get("version") != CUTOFF_VERSION
            or entry.get("basis") != "DECLARED_SIM_RULE" or entry.get("identity") != identity
            or cutoff_ref.get("kind") not in {"DERIVED", "CONTROLLED"}):
        raise ValueError("firstbar_explicit_sim_cutoff_policy_required")
    local_deadline = datetime.combine(date.fromisoformat(episode.identity.trading_day),
        day_time(15, 45), tzinfo=ZoneInfo("America/New_York"))
    expected_cutoff = min(timestamp_ns(local_deadline.isoformat()), episode.session_end.available_ns)
    if integer(entry.get("available_ns"), minimum=1) != expected_cutoff:
        raise ValueError("firstbar_cutoff_not_declared_1545_or_session_end")
    # This is a new SIM rule applied to a reconstructed clock, not historical
    # live configuration. Preserve its actual later asof, never backdate it.
    entry_source = _source_reference(t, cutoff_ref, cutoff)
    return FirstBarReplayInput(episode, bar, t.Boundary(expected_cutoff, entry_source))


def prepare_firstbar_job(job, evidence_root, *, now_ns=None):
    """Read immutable local evidence and construct inputs, NEVER run the engine."""
    from . import types as t
    from .cohort_assembler import assemble_cohort, integer
    from .cohort_provenance import load_proof_packets
    from .engine_binding import ENGINE_HEAD as ASSEMBLER_ENGINE_HEAD, verify_plan_evidence
    from .firstbar_binding import FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION, load_firstbar_engine
    from .firstbar_projection import FIRSTBAR_SEED_LABEL, SealedFirstBarCohort
    from .policy_io import load_execution_policy, POLICY_LABEL
    from .repository import digest, json_value

    if not isinstance(job, FirstBarJob) or hashlib.sha256(job.raw).hexdigest() != job.sha256:
        raise ValueError("immutable_firstbar_job_required")
    body = _json(job.raw)
    if (not isinstance(body, dict) or body.get("version") != JOB_VERSION
            or body.get("engine_dependency_head") != FIRSTBAR_ENGINE_HEAD
            or body.get("population_label") not in ("replay, in-sample", FIRSTBAR_SEED_LABEL)):
        raise ValueError("explicit_versioned_firstbar_job_required")
    if "reference_seed_binding" in body:
        raise ValueError("firstbar_descriptor_must_be_derived_from_actual_seed_files")
    # Reuse the existing exact-file AND loaded-module-root gate. Construction
    # has no ports/resources and does not call simulate/simulate_many.
    load_firstbar_engine()
    verify_firstbar_assembler()
    plan = body["plan"]
    if (not isinstance(plan, dict) or plan.get("version") != ASSEMBLER_VERSION
            or plan.get("engine_dependency_head") != ASSEMBLER_ENGINE_HEAD
            or plan.get("policy_version") != FIRSTBAR_RESULT_VERSION):
        raise ValueError("explicit_existing_assembler_and_firstbar_policy_required")
    observed = integer(body.get("evidence_cutoff_ns"), minimum=1)
    current = time.time_ns() if now_ns is None else integer(now_ns, minimum=1)
    if observed > current:
        raise ValueError("future_firstbar_evidence_cutoff_not_allowed")
    files = EvidenceFiles(evidence_root)
    seed_binding, seed_refs, seed_status = _reference_seed(files, body.get("reference_seed"))
    population_label = FIRSTBAR_SEED_LABEL if seed_binding is not None else "replay, in-sample"
    if body["population_label"] == FIRSTBAR_SEED_LABEL and seed_binding is None:
        raise ValueError("firstbar_seed_label_requires_actual_seed_files")
    raw_packets, _ = _packets(files, body.get("packet_files"))
    proofs = body.get("proof_files", [])
    if not isinstance(proofs, list):
        raise ValueError("firstbar_proof_descriptors_required")
    # Parse proof JSON strictly/bounded before the unchanged retained-proof loader.
    for proof in proofs:
        if not isinstance(proof, dict):
            raise ValueError("firstbar_proof_descriptor_mapping_required")
        files.verify_tree(files.document({**proof, "pointer": "$"}))
    packets = (*raw_packets, *load_proof_packets(proofs, files.root))
    if len(packets) > MAX_PACKETS:
        raise ValueError("firstbar_packet_population_budget_exceeded")
    actual_packet_hash = digest(packets)
    files.verify_tree(plan)
    verified = verify_plan_evidence(plan, files.root)
    policy = load_execution_policy(plan.get("execution_policy"), files.root)
    assembled = assemble_cohort(plan, packets, t, observed_at_ns=observed, execution_policy=policy)
    bindings = body.get("flip_bar_bindings")
    if not isinstance(bindings, list) or len(bindings) > 4096:
        raise ValueError("bounded_firstbar_bindings_required")
    keyed = {}
    for binding in bindings:
        if not isinstance(binding, dict):
            raise ValueError("firstbar_binding_mapping_required")
        key = _identity(binding.get("identity"))
        if key in keyed:
            raise ValueError("duplicate_firstbar_completed_bar_binding")
        keyed[key] = binding
    expected = {_identity(spec["identity"]) for spec in plan["episodes"]}
    if set(keyed) - expected:
        raise ValueError("firstbar_binding_outside_exact_cohort")
    indexed = {packet["key"]: packet for packet in packets}
    inputs, missing = [], []
    for episode in assembled.episodes:
        identity = json_value(episode.identity)
        binding = keyed.get(_identity(identity))
        try:
            if binding is None:
                raise MissingFirstBarOperand("UNKNOWN_COMPLETED_FLIP_BINDING")
            inputs.append(_completed_input(binding, episode, indexed, files, observed, t))
        except (MissingFirstBarOperand, FileNotFoundError) as error:
            missing.append({"identity": identity, "issue": type(error).__name__ if isinstance(error, FileNotFoundError) else str(error)})
    prepared = not missing and assembled.all_overlapping_accounts_included is True
    coverage = {"complete": None, "native_asof": "UNKNOWN", "original_bar_asof": "UNKNOWN",
        "population_label": population_label,
        "reference_seed_binding": seed_binding, "reference_seed_status": seed_status,
        "assembler_coverage": assembled.coverage, "input_issues": missing,
        "job_sha256": job.sha256, "raw_packet_sha256": actual_packet_hash,
        "verified_evidence_files": verified,
        "execution_policy_label": "UNKNOWN" if policy is None else POLICY_LABEL,
        "completed_bar_evidence": [{"identity": json_value(item.episode.identity),
            "complete": item.flip_bar.complete, "available_ns": item.flip_bar.available_ns,
            "source": json_value(item.flip_bar.source)} for item in inputs]}
    binding_refs = tuple(source for item in inputs for source in (item.flip_bar.source, item.entry_cutoff.source))
    cohort = SealedFirstBarCohort(plan["source_id"], job.sha256, tuple(inputs), (*assembled.source_refs, *binding_refs, *seed_refs),
        coverage, True) if prepared else None
    days = sorted({date.fromisoformat(item.identity.trading_day).isoformat() for item in assembled.episodes})
    manifest = {"version": "ATRSIM_FIRSTBAR_PUBLICATION_MANIFEST_V1",
        "source_id": plan["source_id"], "publication_state": "UNPUBLISHED",
        "engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
        "engine_contract_version": FIRSTBAR_RESULT_VERSION,
        "population_label": population_label, "qualification": "UNQUALIFIED_REPLAY",
        "trading_day_start": days[0], "trading_day_end": days[-1],
        "episode_count": len(assembled.episodes),
        "reference_seed_population_count": None if seed_status is None else seed_status["selected_reference_signals"],
        "reference_seed_binding": seed_binding, "reference_seed_status": seed_status,
        "job_sha256": job.sha256, "raw_packet_sha256": actual_packet_hash,
        "modes": ["FIRSTBAR", "FIRSTBAR_X3"]}
    diagnostics = {"status": "PREPARED_ONLY" if prepared else "PREPARED_UNKNOWN_UNPUBLISHABLE",
        "engine_execution": "UNRUN", "engine_dependency_head": FIRSTBAR_ENGINE_HEAD,
        "contract": JOB_VERSION, "job_sha256": job.sha256, "raw_packet_sha256": actual_packet_hash,
        "expected_raw_packet_sha256": body.get("expected_raw_packet_sha256"),
        "episode_count": len(assembled.episodes), "bound_input_count": len(inputs),
        "all_overlapping_accounts_included": assembled.all_overlapping_accounts_included,
        "coverage": coverage, "qualification": "UNQUALIFIED_REPLAY", "publication": manifest}
    return PreparedFirstBarJob(cohort, tuple(packets), diagnostics)
