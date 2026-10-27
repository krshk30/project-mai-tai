"""Verified standalone live target leaf, never broker_adapters package import."""

from functools import lru_cache
import hashlib
from pathlib import Path


TARGET_RULE_SHA256 = "591a4f6473db1aa200e0b96175a3276d0cd6879102acc5c4dcd6938ff9290e6f"
EXIT_POLICY_VERSION = "C1_20261023_2129_A2_CEILING_FIRST_ELIGIBLE_STOP_PRINT"


def target_rule_path():
    return Path(__file__).resolve().parents[1] / "broker_adapters/target_price.py"


@lru_cache(maxsize=1)
def _sealed_rule(raw, filename):
    namespace = {"__name__": "_atrsim_verified_standalone_target_rule"}
    exec(compile(raw, filename, "exec"), namespace)
    return namespace["target_limit_price"]


def load_target_rule(path=None):
    path = Path(path) if path is not None else target_rule_path()
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != TARGET_RULE_SHA256:
        raise ValueError("firstbar_live_target_rule_source_mismatch")
    return _sealed_rule(raw, str(path))


def target_rule_source_pin():
    path = target_rule_path()
    load_target_rule(path)
    return str(path.resolve()), TARGET_RULE_SHA256
