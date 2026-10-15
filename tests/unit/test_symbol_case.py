import pytest

from project_mai_tai.symbol_case import raw_symbol_is_uppercase


@pytest.mark.parametrize("raw,allowed", [
    ("TPC", True), ("TpC", False), ("BCPC", True), ("BCpC", False),
    ("BRK.B", True), ("BRK.b", False), ("XYZpA", False), ("XYZr", False),
    ("XYZw", False), ("", False), (None, False), (123, False),
    (" TPC ", True), ("123", True),
])
def test_case_only_policy_before_normalization(raw, allowed):
    assert raw_symbol_is_uppercase(raw) is allowed
