"""Canonical DAY nonworking F consumer against the isolated PostgreSQL service."""

from tests.integration.test_clearwait1_postgres import pg_db as db
from tests.integration.test_falseflip1_postgres_epochs import postgres_factory  # noqa: F401
from tests.unit.test_clearwait1_day_nonworking_consumer import (
    sdk,
    test_canonical_nonworking_exact_identity_and_other_guards,
)

__all__ = ["db", "sdk", "test_canonical_nonworking_exact_identity_and_other_guards"]
