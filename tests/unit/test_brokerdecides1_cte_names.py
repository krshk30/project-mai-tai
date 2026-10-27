"""Construction-only graph identity controls; compilation gates are separate."""

from sqlalchemy.sql import visitors
from sqlalchemy.sql.selectable import CTE

from project_mai_tai.brokerdecides1_set_proof import validated_retirement_ids


EXPECTED = {
    "l12_current_members", "l12_live_counts", "l12_evidence_counts",
    "l12_invalid_ids", "l12_valid_ids",
}


def nodes(query):
    return {id(node): node for node in visitors.iterate(query) if isinstance(node, CTE)}.values()


def test_all_cte_names_are_explicit_and_unique_to_each_node():
    graph = tuple(nodes(validated_retirement_ids()))
    assert len(graph) == 5
    assert {node.name for node in graph} == EXPECTED
    assert all(type(node.name) is str for node in graph)


def test_independent_statements_do_not_share_mutable_cte_objects():
    first = tuple(nodes(validated_retirement_ids()))
    second = tuple(nodes(validated_retirement_ids()))
    assert {node.name for node in first} == {node.name for node in second} == EXPECTED
    assert {id(node) for node in first}.isdisjoint(id(node) for node in second)
