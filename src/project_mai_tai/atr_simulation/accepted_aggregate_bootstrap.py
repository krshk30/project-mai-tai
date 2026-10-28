"""Startup-only binding for the one reviewer-accepted recorded baseline.

No model imports or execution. No database connection occurs during preparation.
The original producer pins remain provenance, never current consumer pins.
"""

import hashlib
import os
from pathlib import Path
import stat

from .aggregate_entry_publication import AggregateProducerBinding, prepare_aggregate_publication
from .aggregate_entry_read_model import prepare_aggregate_read_binding
from .aggregate_entry_reader import AggregateEntryUIReader, publication_read_binding


RECEIPT_SHA256 = "265b847b615df1efbb85cbf3b053bd9d597198309bdebd89d270ca458b0698e8"
RECEIPT_BYTES = 34886838
PUBLICATION_SHA256 = "856a2601bd43a92dc2f92a1a2589d44814f25f775872474fc0d10c8c1ff2b9a3"
PRODUCER_ROOT = "/private/tmp/mai-tai-l14-aggregate-confirmation-exit-20261024"
PRODUCER_HEAD = "14b3e6198ad713d145f326a7388eb8298f748e55"
PRODUCER_PINS = {
    "src/project_mai_tai/__init__.py": "55e80a8d3b711db24ec795bd95b523b32cf3f0200f38c25fbd0d7c557d01b326",
    "src/project_mai_tai/atr_simulation/__init__.py": "714cc37aadd4fc7c3087a589d9a8534198786c7778f10040f8eb9acc9a99852a",
    "src/project_mai_tai/atr_simulation/execution_eligibility.py": "3f6b657a69efa79e81fb73cf390051e296c20a876910b87857ee79861db582cc",
    "src/project_mai_tai/atr_simulation/exits.py": "2853179ba83b8ba5bb137beeeac38c4a3696a1d6fd2325366c67f5bb5799d5f1",
    "src/project_mai_tai/atr_simulation/firstbar.py": "29bf8d2282a0d3ee55d43a6676ddc1e817c51a57c41b6ea14fcb866484697302",
    "src/project_mai_tai/atr_simulation/firstbar_exits.py": "e895419f79346594133f619395c6f2d2cacf923662ff3a328a1eb5943c04d503",
    "src/project_mai_tai/atr_simulation/firstbar_live_baseline.py": "2cd68bf9102fc840544c173036e2086548d5be64ddb8a2d6834cbf8c9ff7e472",
    "src/project_mai_tai/atr_simulation/firstbar_reference.py": "b984c5268f5d2f4be0d00302573650251e3989edbe67935e7388d71bc91d3ae4",
    "src/project_mai_tai/atr_simulation/firstbar_replay.py": "4ef286903bebc2c87cf6d4ddc8c93b51311cfc16228d9d0840f4234b5befc2c9",
    "src/project_mai_tai/atr_simulation/firstbar_study.py": "f2bf4d031672af56d86ec9e63c866b2140ac6fe299a83f52040d0167bbeaf169",
    "src/project_mai_tai/atr_simulation/firstbar_target_rule.py": "a4ed9a1e387a3815f08a5c61e47bbed59c57e8b83bca39f749965bc1fe12d0c6",
    "src/project_mai_tai/atr_simulation/types.py": "391bf7ec27bd3bbe03420d0130832fe2ba7150d2c491b13a9f418708874f7d98",
    "src/project_mai_tai/backtest/__init__.py": "a73144c95e49ee92c0a464edd2d75886423979c4bafde6dc3672f5aeec69b420",
    "src/project_mai_tai/backtest/atr_oracle.py": "22ead27b69dc89cafc75e6dc1c97238a9cd8f82d48b4e4c2665bdb4889fa60d7",
    "src/project_mai_tai/broker_adapters/target_price.py": "591a4f6473db1aa200e0b96175a3276d0cd6879102acc5c4dcd6938ff9290e6f",
    "src/project_mai_tai/momentum_paper/conditions.py": "e9da2f80e45439fdcc5a9d32565af7d62f8b6559a02f85e55eb14e12f182edee",
    "tools/atr_firstbar_live_baseline.py": "357696feca9b5d2d403ec4a6b9fd67492a8bad66686f6f22ab34e65b2ee93d6d"
}
# Physical deployment copies may move; immutable stored provenance must not.
RETAINED_ARTIFACT = "/private/tmp/OCT24-FIRSTBAR-RECORDED-AGGREGATE-14B-CONFIRMATION-FIRST-20261024.json"


def accepted_receipt_bytes(path):
    path = Path(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("absolute_canonical_accepted_receipt_required")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_size != RECEIPT_BYTES
                or info.st_mode & 0o022):
            raise ValueError("bounded_readonly_regular_accepted_receipt_required")
        raw = stream.read(RECEIPT_BYTES + 1)
    if len(raw) != RECEIPT_BYTES or hashlib.sha256(raw).hexdigest() != RECEIPT_SHA256:
        raise ValueError("reviewed_accepted_receipt_bytes_changed")
    return raw


def configured_aggregate_port(database_url, receipt_path, *,
                              realized_receipt_path=None, realized_receipt_sha256=None):
    """Called before serving, not by a request; None is completely inert.

    Invalid explicit configuration refuses startup. A foreign or changed receipt
    must never silently select a different generation or zero-valued baseline.
    """
    if receipt_path is None:
        if realized_receipt_path is not None or realized_receipt_sha256 is not None:
            raise ValueError("realized_requires_accepted_aggregate_binding")
        return None
    raw = accepted_receipt_bytes(receipt_path)
    producer = AggregateProducerBinding(PRODUCER_ROOT, PRODUCER_HEAD,
        tuple(sorted(PRODUCER_PINS.items())),
        (("live:orb", "webull"), ("live:schwab_1m_v2", "schwab")), "schwab_1m_v2")
    reference = dict(artifact=RETAINED_ARTIFACT, pointer="$", sha256=RECEIPT_SHA256,
                     kind="RETAINED", asof_ns=None)
    prepared = prepare_aggregate_publication(raw, receipt_source=reference, producer=producer)
    if prepared.publication_sha256 != PUBLICATION_SHA256:
        raise ValueError("accepted_publication_changed_before_reader_creation")
    projection = prepare_aggregate_read_binding(raw, receipt_source=reference, producer=producer,
        expected_receipt_sha256=RECEIPT_SHA256, source_id="aggregate-entry:" + RECEIPT_SHA256)
    from .realized_display import configured_realized_display
    realized = configured_realized_display(realized_receipt_path, realized_receipt_sha256,
                                          aggregate_binding=projection)
    reader = AggregateEntryUIReader(database_url,
        binding=publication_read_binding(prepared, producer=producer))
    from project_mai_tai.services.atr_aggregate_routes import AggregateSimulationReadPort
    return AggregateSimulationReadPort(reader, projection, realized)
