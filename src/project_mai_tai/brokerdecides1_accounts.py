"""Bounded local alias scope. This is NOT native account-equivalence proof."""
from sqlalchemy import select

from project_mai_tai.db.models import BrokerAccount

MAX_LOCAL_ACCOUNT_ALIASES = 64


def local_accounts(session, names):
    configured = session.scalars(select(BrokerAccount).where(BrokerAccount.name.in_(names))).all()
    if (len(set(names)) != 2 or len(configured) != 2
            or {account.name for account in configured} != set(names)
            or any(not name.startswith("live:") for name in names)):
        raise ValueError("local_account_identity_UNKNOWN")
    # Environment is deployment metadata (e.g. production), not a live-account
    # identifier. Inactive aliases can still own uncertain wire effects.
    accounts = session.scalars(select(BrokerAccount).where(BrokerAccount.name.like("live:%"))
        .limit(MAX_LOCAL_ACCOUNT_ALIASES + 1)).all()
    if len(accounts) > MAX_LOCAL_ACCOUNT_ALIASES:
        raise ValueError("local_account_alias_census_overflow")
    if not set(names).issubset({account.name for account in accounts}):
        raise ValueError("local_account_alias_coverage_UNKNOWN")
    return accounts
