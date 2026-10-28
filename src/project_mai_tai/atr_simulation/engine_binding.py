"""Reviewed placement-book successor bytes; no competing execution implementation."""

from collections.abc import Mapping
import hashlib
from pathlib import Path
import sys


ENGINE_HEAD = "5e3aba0dfea1bce93b50c598e522b8b67902e19e"
IO_COMPONENT_HEAD = "ba08c1d2798a77ecc801b2f999547aad11593988"
ENGINE_PINS = {
    "__init__.py": "714cc37aadd4fc7c3087a589d9a8534198786c7778f10040f8eb9acc9a99852a",
    "types.py": "90f8dfac9a732c1fd247e5c10f2af18c7d3360ef987f43826ce3b8f708cef4ef",
    "engine.py": "696e506a561500e391bccf5b3fff529a5b47fa1fc8f6a2fd764e35759dc28f32",
    "entry.py": "840895efc7a944cd39370dad19962c76b3a0ce0dcf1ff60dcbf94aa408feae23",
    "exits.py": "2853179ba83b8ba5bb137beeeac38c4a3696a1d6fd2325366c67f5bb5799d5f1",
    "volume_body.py": "6963a659f23466db66e2bfea1d44a4fed36e017ad8d336e219fc12c46ffa0e57",
    "fill_models.py": "a749f396dd2a3b7b5b4697b27991a5874f9a43cdf03be0cb12dc2155cd5047b8",
    "execution_eligibility.py": "3f6b657a69efa79e81fb73cf390051e296c20a876910b87857ee79861db582cc",
    "../momentum_paper/conditions.py": "e9da2f80e45439fdcc5a9d32565af7d62f8b6559a02f85e55eb14e12f182edee",
}


def verify_engine_files(root=None):
    root = Path(root) if root is not None else Path(__file__).resolve().parent
    for name, expected in ENGINE_PINS.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("engine_source_pin_mismatch:" + name)
    return ENGINE_HEAD


def load_engine():
    verify_engine_files()
    from .engine import SimulationEngine
    from . import types

    # Both constructors must resolve beside the exact files checked above.
    if any(Path(module.__file__).resolve().parent != Path(__file__).resolve().parent for module in (
            types, sys.modules[SimulationEngine.__module__])):
        raise ValueError("engine_import_root_changed")
    return SimulationEngine(), types


def verify_plan_evidence(plan, evidence_root):
    """Verify referenced bytes, not their completeness/as-of/native interpretation."""
    root = Path(evidence_root).resolve(strict=True)
    checked = {}

    def visit(value):
        if isinstance(value, dict):
            if {"artifact", "sha256", "pointer", "kind"} <= value.keys():
                artifact = Path(value["artifact"])
                path = (artifact if artifact.is_absolute() else root / artifact).resolve(strict=True)
                if not path.is_relative_to(root) or not path.is_file():
                    raise ValueError("evidence_path_outside_declared_root")
                pair = (str(path), value["sha256"])
                if pair not in checked:
                    if path.stat().st_size > 128 * 1024 * 1024:
                        raise ValueError("evidence_file_budget_exceeded")
                    checksum = hashlib.sha256()
                    with path.open("rb") as source:
                        while chunk := source.read(1024 * 1024):
                            checksum.update(chunk)
                    if checksum.hexdigest() != value["sha256"]:
                        raise ValueError("actual_evidence_file_hash_mismatch")
                    checked[pair] = {"path": str(path), "sha256": value["sha256"]}
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(plan)
    if not checked:
        raise ValueError("actual_plan_evidence_files_required")
    return tuple(checked.values())


def bound_simulation_view(stored, **kwargs):
    from .read_model import simulation_view

    view = simulation_view(stored, **kwargs)
    run = stored.get("run")
    coverage = run.get("coverage") if isinstance(run, Mapping) else None
    # A module import is not proof that already-stored rows used this engine.
    view.pop("position_api_binding_head", None)
    if isinstance(coverage, Mapping) and coverage.get("engine_dependency_head") == ENGINE_HEAD:
        view["position_api_binding_head"] = ENGINE_HEAD
    return view
