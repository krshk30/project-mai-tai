"""C6 scope identity; deployment environment is never an execution mode."""

CONTROLLED_STRATEGY_CODES = frozenset({"schwab_1m_v2", "orb_schwab", "momentum_warn_live"})


def is_controlled_strategy(code: object) -> bool:
    return isinstance(code, str) and code in CONTROLLED_STRATEGY_CODES


def logical_account_mode(name: object) -> str:
    if not isinstance(name, str) or name != name.strip():
        return "UNKNOWN"
    prefix, separator, suffix = name.partition(":")
    if separator and suffix and prefix in {"live", "paper", "shadow"}:
        return prefix
    return "UNKNOWN"


def live_scope(execution_mode: object, account_name: object) -> bool:
    return execution_mode == "live" and logical_account_mode(account_name) == "live"


def controlled_live_scope(code: object, execution_mode: object, account_name: object) -> bool:
    return is_controlled_strategy(code) and live_scope(execution_mode, account_name)
