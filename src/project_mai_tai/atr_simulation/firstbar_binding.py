"""Explicit additive FIRSTBAR dependency; leaves old VOLBODY bindings unchanged."""

import hashlib
from pathlib import Path
import sys


FIRSTBAR_ENGINE_HEAD = "e712a99800b9cfd716b406e027845f89567647fe"
FIRSTBAR_RESULT_VERSION = "ATRSIM_FIRSTBAR_REPLAY_V2_A2_CEILING_CAUSAL_STOP_PRINT"
FIRSTBAR_FILL_POLICY = "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME"
FIRSTBAR_PINS = {
    "firstbar.py": "29bf8d2282a0d3ee55d43a6676ddc1e817c51a57c41b6ea14fcb866484697302",
    "firstbar_exits.py": "ee38e47e7188c2dec75cb5f58edad7b897841cdb745ed4be8facaedccffb11d2",
    "firstbar_replay.py": "4ef286903bebc2c87cf6d4ddc8c93b51311cfc16228d9d0840f4234b5befc2c9",
    "firstbar_target_rule.py": "a4ed9a1e387a3815f08a5c61e47bbed59c57e8b83bca39f749965bc1fe12d0c6",
}


def verify_firstbar_files(root=None):
    from . import engine_binding

    actual = Path(__file__).resolve().parent
    if Path(engine_binding.__file__).resolve().parent != actual:
        raise ValueError("firstbar_legacy_binding_import_root_changed")
    root = Path(root).resolve() if root is not None else actual
    engine_binding.verify_engine_files(root)
    for name, checksum in FIRSTBAR_PINS.items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != checksum:
            raise ValueError("firstbar_engine_source_pin_mismatch:" + name)
    leaf = root.parent / "broker_adapters/target_price.py"
    if hashlib.sha256(leaf.read_bytes()).hexdigest() != "591a4f6473db1aa200e0b96175a3276d0cd6879102acc5c4dcd6938ff9290e6f":
        raise ValueError("firstbar_live_target_dependency_mismatch")
    return FIRSTBAR_ENGINE_HEAD


def load_firstbar_engine():
    """Only construct the actual pinned Gibbs engine; no execution at import."""
    verify_firstbar_files()
    # The replay module imports both planner and exits before the root checks below.
    from . import engine_binding, firstbar_replay

    root = Path(__file__).resolve().parent
    expected = {**engine_binding.ENGINE_PINS, **FIRSTBAR_PINS}
    for name in expected:
        if name.startswith("../"):
            module_name = "project_mai_tai.momentum_paper.conditions"
        elif name == "__init__.py":
            module_name = __package__
        else:
            module_name = __package__ + "." + Path(name).stem
        module = sys.modules.get(module_name)
        if module is not None and Path(module.__file__).resolve() != (root / name).resolve():
            raise ValueError("firstbar_engine_import_root_changed:" + name)
    if (firstbar_replay.FIRSTBAR_REPLAY_VERSION != FIRSTBAR_RESULT_VERSION
            or firstbar_replay.FirstBarFillPolicy.OBSERVED_PRINT_VOLUME.value != FIRSTBAR_FILL_POLICY):
        raise ValueError("firstbar_engine_public_contract_changed")
    return firstbar_replay.FirstBarReplayEngine(), firstbar_replay
