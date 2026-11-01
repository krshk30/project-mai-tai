from __future__ import annotations

import gc
from pathlib import Path
import sys

import pytest


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


@pytest.fixture(autouse=True)
def timing_gc_isolation(request):
    """Keep the pre-scenario heap out of collections for explicit timing gates."""
    if request.node.get_closest_marker("timing") is None:
        yield
        return
    gc.collect()
    try:
        gc.freeze()
        yield
    finally:
        gc.unfreeze()
