"""Zero-ID request CAS on real PG; broker responses remain controlled inputs."""

from tests.integration.test_clearwait1_postgres import pg_db as controlled_db
from tests.integration.test_falseflip1_postgres_epochs import postgres_factory  # noqa: F401
from tests.unit.test_clearwait1_zero_id_broker_books import (
    sdk as controlled_sdk,
    test_zero_id_actual_bot_caller_requires_both_complete_broker_readers,
    test_zero_id_complete_books_clear_only_exact_no_dispatch_request,
)

db, sdk = controlled_db, controlled_sdk

__all__ = [
    "test_zero_id_actual_bot_caller_requires_both_complete_broker_readers",
    "test_zero_id_complete_books_clear_only_exact_no_dispatch_request",
]
