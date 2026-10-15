"""Raw market-symbol case policy, independent of paper or strategy eligibility."""


def raw_symbol_is_uppercase(symbol: object) -> bool:
    """Reject distinct lowercase share-class markers before any normalization."""
    return isinstance(symbol, str) and bool(symbol) and symbol == symbol.upper()
