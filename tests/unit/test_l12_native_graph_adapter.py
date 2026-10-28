"""Controlled native packets only, not actual PG graph/completeness evidence."""

from copy import deepcopy

import pytest

from tools import l12_native_graph_adapter as adapter
from tools import l12_native_schema_graph as native
from tools import l12_whole_schema_semantics as semantic

PINS = {"tools/l12_native_schema_graph.py": "a" * 64,
    "tools/l12_native_graph_adapter.py": "b" * 64, "tools/l12_whole_schema_semantics.py": "c" * 64}
EPOCH = dict(database="controlled_private", role="controlled", system_identifier="123",
    postmaster_started="2026-10-24 00:00:00+00", server_version_num="160015")


def packet():
    layout = []
    no_oid = {"pg_attribute", "pg_index", "pg_sequence", "pg_range", "pg_aggregate", "pg_ts_config_map",
        "pg_partitioned_table", "pg_inherits", "pg_description", "pg_seclabel", "pg_init_privs"}
    for catalog in native.CATALOGS:
        fields = set(adapter.CATALOG_MINIMUM[catalog].split()) | set(native.OID_FIELDS.get(catalog, {})) | set(native.OID_VECTORS.get(catalog, {}))
        fields |= adapter.PHYSICAL.get(catalog, set())
        if catalog not in no_oid:
            fields.add("oid")
        for ordinal, field in enumerate(sorted(fields), 1):
            kind = ("oid" if field in native.OID_FIELDS.get(catalog, {}) or field == "oid" else
                "_oid" if field in native.OID_VECTORS.get(catalog, {}) else
                "pg_node_tree" if field in adapter.DEPARSED.get(catalog, {}) else "text")
            layout.append(dict(catalog=catalog, field=field, attnum=ordinal, type_name=kind, formatted_type=kind))
    sections = {catalog: [] for catalog in native.CATALOGS}
    sections["pg_namespace"] = [dict(
        **{"class": "pg_catalog.pg_namespace"}, oid=10, subid=0,
        raw=dict(oid=10, nspname="public", nspowner=20, nspacl=None),
        typed_fields=dict(nspowner=20), typed_vectors={})]
    return dict(version="L12_NATIVE_WHOLE_SCHEMA_SOURCE_GRAPH_V1", role="PRIVATE_BASELINE",
        source_sha256="a" * 64, context=dict(database="controlled_private", role="controlled",
            postmaster_started="2026-10-24 00:00:00+00", version="160015", read_only="on",
            isolation="repeatable read", snapshot="1:1:", observed_at="CONTROLLED_NOT_ACTUAL_CLOCK"),
        selected=[{"class": "pg_catalog.pg_namespace", "oid": 10, "subid": 0}], sections=sections,
        dependencies=[], native_identities=[dict(**{"class": "pg_catalog.pg_namespace"}, oid=10, subid=0,
            type="schema", object_names=["public"], object_args=[]),
            dict(**{"class": "pg_catalog.pg_authid"}, oid=20, subid=0,
                type="role", object_names=["controlled_owner"], object_args=[])],
        catalog_layout=layout, raw_node_tree_fields={name: list(fields) for name, fields in native.RAW_NODE_TREES.items()},
        status="NATIVE_GRAPH_SOURCE_PENDING_SEMANTIC_ADAPTER_AND_ROLLBACK",
        canonical_approval=False, permission_granted=False, rollback_verified=False)


def convert(value, *, epoch=EPOCH):
    raw = semantic.canonical(value)
    return adapter.adapt(raw, semantic.digest(value), source_pins=PINS, profile_review_sha256="d" * 64,
        closure_audit_sha256="e" * 64, epoch=epoch)


def normalized(converted):
    value = converted["graph"]
    return semantic.Graph(semantic.canonical(value), semantic.digest(value), value["role"], converted["profile"], {}).normalized()[0]


def with_table(value):
    fields = {row["field"] for row in value["catalog_layout"] if row["catalog"] == "pg_class"}
    raw = {field: None for field in fields}
    typed = {field: 0 for field in native.OID_FIELDS["pg_class"]}
    typed.update(relnamespace=10, relowner=20)
    raw.update(typed)
    raw.update(oid=100, relname="controlled_table", relkind="r", relpersistence="p", relnatts=0,
        relchecks=0, relhasrules=False, relhastriggers=False, relhassubclass=False, relrowsecurity=False,
        relforcerowsecurity=False, relispopulated=True, relreplident="d", relispartition=False,
        relfilenode=100, relpages=0, reltuples=-1.0, relallvisible=0, relfrozenxid="100", relminmxid="1")
    value["sections"]["pg_class"] = [dict(**{"class": "pg_catalog.pg_class"}, oid=100, subid=0,
        raw=raw, typed_fields=typed, typed_vectors={}, definition=None, partition_bound=None)]
    value["selected"].append({"class": "pg_catalog.pg_class", "oid": 100, "subid": 0})
    value["native_identities"].append(dict(**{"class": "pg_catalog.pg_class"}, oid=100, subid=0,
        type="table", object_names=["public", "controlled_table"], object_args=[]))
    return value


def test_end_to_end_raw_packet_preserved_and_graph_not_permission():
    value = packet()
    original = deepcopy(value)
    converted = convert(value)
    assert value == original
    assert converted["permission_granted"] is False and converted["canonical_approval"] is False
    assert converted["profile"]["version"] == "L12_SOURCE_CLOSED_NATIVE_RAW_ADAPTER_PROFILE_V2"
    nodes = list(normalized(converted).values())
    assert len(nodes) == 1
    assert nodes[0]["payload"]["state"]["nspname"] == "public"
    assert nodes[0]["payload"]["state"]["nspacl"] is None


def test_actual_native_identity_not_bare_numeric_oid_determines_schema_equality():
    left, right = packet(), packet()
    right["selected"][0]["oid"] = 110
    row = right["sections"]["pg_namespace"][0]
    row["oid"] = row["raw"]["oid"] = 110
    row["raw"]["nspowner"] = row["typed_fields"]["nspowner"] = 220
    right["native_identities"][0]["oid"] = 110
    right["native_identities"][1]["oid"] = 220
    assert normalized(convert(left)) == normalized(convert(right))


@pytest.mark.parametrize("fault", ["owner", "acl", "extra_scalar"])
def test_owner_ACL_or_extra_semantic_field_not_dropped(fault):
    left, right = packet(), packet()
    if fault == "owner":
        right["native_identities"][1]["object_names"] = ["DIFFERENT_OWNER"]
    elif fault == "acl":
        right["sections"]["pg_namespace"][0]["raw"]["nspacl"] = ["controlled=U/owner"]
    else:
        for value in (left, right):
            value["catalog_layout"].append(dict(catalog="pg_namespace", field="extra_semantic",
                attnum=99, type_name="text", formatted_type="text"))
            value["sections"]["pg_namespace"][0]["raw"]["extra_semantic"] = "UNCHANGED"
        right["sections"]["pg_namespace"][0]["raw"]["extra_semantic"] = "CHANGED"
    assert normalized(convert(left)) != normalized(convert(right))


def test_missing_required_field_from_both_layout_and_raw_still_refused():
    value = packet()
    value["catalog_layout"] = [row for row in value["catalog_layout"]
        if (row["catalog"], row["field"]) != ("pg_namespace", "nspname")]
    del value["sections"]["pg_namespace"][0]["raw"]["nspname"]
    with pytest.raises(ValueError, match="mandatory_native_schema_fields_missing_from_layout_STOP"):
        convert(value)


@pytest.mark.parametrize("fault", ["section", "selected", "resolution", "typed_contradiction"])
def test_incomplete_or_contradictory_native_packet_STOP(fault):
    value = packet()
    if fault == "section":
        del value["sections"]["pg_index"]
    elif fault == "selected":
        value["selected"].append({"class": "pg_catalog.pg_class", "oid": 999, "subid": 0})
    elif fault == "resolution":
        value["native_identities"].pop()
    else:
        value["sections"]["pg_namespace"][0]["typed_fields"]["nspowner"] = 999
    with pytest.raises(ValueError):
        convert(value)


@pytest.mark.parametrize("change", [dict(role="PRODUCTION_BASELINE"), dict(permission_granted=True),
    dict(rollback_verified=True), dict(source_sha256="f" * 64)])
def test_no_private_to_production_relabel_or_claimed_permission(change):
    value = packet()
    value.update(change)
    with pytest.raises(ValueError, match="exact_raw_producer_packet_not_permission_or_production_relabel_required"):
        convert(value)


def test_epoch_mismatch_STOP():
    changed = dict(EPOCH, postmaster_started="FOREIGN_EPOCH")
    with pytest.raises(ValueError, match="actual_same_private_epoch_database_role_readonly_snapshot_required"):
        convert(packet(), epoch=changed)


def test_dependency_multiplicity_and_shared_namespace_preserved():
    value = packet()
    edge = dict(catalog="pg_shdepend", dependent_class="pg_catalog.pg_namespace", dependent_class_oid=2615,
        objid=10, objsubid=0, referenced_class="pg_catalog.pg_authid", refobjid=20, refobjsubid=0, deptype="o")
    value["dependencies"] = [edge, deepcopy(edge)]
    actual = normalized(convert(value))
    edges = [row for row in actual.values() if "edge" in row]
    assert len(edges) == 1 and edges[0]["multiplicity"] == 2
    assert edges[0]["edge"]["deptype"] == "shared:o"


def test_unknown_dependency_namespace_STOP():
    value = packet()
    value["dependencies"] = [dict(catalog="UNKNOWN")]
    with pytest.raises(ValueError, match="unknown_native_dependency_catalog_STOP"):
        convert(value)


def test_duplicate_selected_address_is_not_silently_collapsed():
    value = packet()
    value["selected"].append(deepcopy(value["selected"][0]))
    with pytest.raises(ValueError, match="duplicate_selected_native_address_STOP"):
        convert(value)


def test_physical_storage_statistics_explicitly_reported_not_schema_drift():
    left, right = with_table(packet()), with_table(packet())
    right["sections"]["pg_class"][0]["raw"].update(relfilenode=777, relpages=44, reltuples=1000.0)
    result = convert(right)
    assert normalized(convert(left)) == normalized(result)
    assert right["sections"]["pg_class"][0]["raw"]["relfilenode"] == 777
    assert any(row.get("operation") == "PHYSICAL_NOT_SQL_SCHEMA" and row.get("raw_value") == 777
        for row in result["transformations"])


def test_relation_enforcement_and_new_deparser_fields_cannot_disappear():
    value = with_table(packet())
    del value["sections"]["pg_class"][0]["partition_bound"]
    with pytest.raises(ValueError, match="exact_native_deparser_projection_required"):
        convert(value)


def test_real_relation_schema_field_change_still_differs():
    left, right = with_table(packet()), with_table(packet())
    right["sections"]["pg_class"][0]["raw"]["relrowsecurity"] = True
    assert normalized(convert(left)) != normalized(convert(right))


def test_cast_native_empty_names_requires_exact_native_argument_pair():
    assert semantic.native_identity(dict(type="cast", object_names=[], object_args=["integer", "bigint"])) == dict(
        type="cast", object_names=[], object_args=["integer", "bigint"])
    with pytest.raises(ValueError):
        semantic.native_identity(dict(type="cast", object_names=[], object_args=[]))


def test_legacy_profile_cannot_adopt_raw_kind_without_new_explicit_version():
    profile = adapter.source_profile(PINS, "d" * 64)
    profile["version"] = "L12_WHOLE_SCHEMA_NATIVE_FIELD_PROFILE_V1"
    with pytest.raises(ValueError, match="reviewed_complete_native_graph_producer_profile_required"):
        semantic.profile_checked(profile)


def test_native_NULL_vector_not_changed_to_empty_array():
    left, right = packet(), packet()
    for value in (left, right):
        value["sections"]["pg_extension"] = [dict(**{"class": "pg_catalog.pg_extension"}, oid=40, subid=0,
            raw=dict(oid=40, extname="controlled_extension", extowner=20, extnamespace=10,
                extrelocatable=False, extversion="1", extconfig=None, extcondition=None),
            typed_fields=dict(extowner=20, extnamespace=10), typed_vectors=dict(extconfig=None))]
        value["selected"].append({"class": "pg_catalog.pg_extension", "oid": 40, "subid": 0})
        value["native_identities"].append(dict(**{"class": "pg_catalog.pg_extension"}, oid=40, subid=0,
            type="extension", object_names=["controlled_extension"], object_args=[]))
    right["sections"]["pg_extension"][0]["raw"]["extconfig"] = []
    right["sections"]["pg_extension"][0]["typed_vectors"]["extconfig"] = []
    assert normalized(convert(left)) != normalized(convert(right))


def test_array_NULL_contradiction_STOP():
    value = packet()
    value["sections"]["pg_namespace"][0]["raw"]["nspowner"] = None
    with pytest.raises(ValueError, match="native_raw_and_typed_OID_contradiction_STOP"):
        convert(value)


def graph_instance(value, profile):
    return semantic.Graph(semantic.canonical(value), semantic.digest(value), value["role"], profile, {})


def storage_graph(parent_oid=100):
    """Controlled Graph layer; not a native SQL/producer witness."""
    converted = convert(with_table(packet()))
    graph = deepcopy(converted["graph"])

    def move(value):
        if isinstance(value, dict):
            if set(value) == {"class", "oid", "subid"} and value["class"] == "pg_catalog.pg_class" and value["oid"] == 100:
                value["oid"] = parent_oid
            for item in value.values():
                move(item)
        elif isinstance(value, list):
            for item in value:
                move(item)

    move(graph)
    parent = next(row for row in graph["objects"] if row["payload"].get("catalog") == "pg_class")
    toast, index = parent_oid + 1, parent_oid + 2
    parent["payload"]["state"]["reltoastrelid"] = adapter.tree_ref("pg_class", toast)
    graph["resolutions"].append(dict(address=adapter.ref("pg_namespace", 11),
        identity=dict(type="schema", object_names=["pg_toast"], object_args=[]), evidence_pointer="CONTROLLED_NAMESPACE"))
    for oid, name, kind in ((toast, "pg_toast_" + str(parent_oid), "t"),
            (index, "pg_toast_" + str(parent_oid) + "_index", "i")):
        graph["resolutions"].append(dict(address=adapter.ref("pg_class", oid),
            identity=dict(type="toast table" if kind == "t" else "index", object_names=["pg_toast", name],
                object_args=[]), evidence_pointer="CONTROLLED_STORAGE"))
        graph["objects"].append(dict(kind="native_object", address=adapter.ref("pg_class", oid),
            payload=dict(catalog="pg_class", state=dict(relkind=kind, relname=name,
                relnamespace=adapter.tree_ref("pg_namespace", 11), reltoastrelid=adapter.tree_ref("pg_class", 0)),
                definitions={}, attachments={} if kind == "t" else dict(pg_index=[dict(
                    state=dict(indrelid=adapter.tree_ref("pg_class", toast), indisunique=True,
                        indisvalid=True, indisready=True, indislive=True),
                    definitions=dict(definition="CREATE UNIQUE INDEX " + name + " ON pg_toast.pg_toast_" + str(parent_oid)
                        + " USING btree (chunk_id, chunk_seq)"))])), evidence_pointer="CONTROLLED_STORAGE"))
    return graph, converted["profile"]


def test_generated_storage_aliases_require_actual_parent_and_exact_name():
    left, profile = storage_graph(100)
    right, _ = storage_graph(500)
    actual_left, actual_right = graph_instance(left, profile), graph_instance(right, profile)
    assert actual_left.normalized()[0] == actual_right.normalized()[0]
    assert any(row["operation"] == "PROVEN_GENERATED_NATIVE_IDENTITY" for row in actual_left.transforms)
    assert "pg_toast_100" in semantic.canonical(left).decode()


@pytest.mark.parametrize("fault", ["parent", "name", "valid", "ready", "live", "definition"])
def test_storage_alias_cannot_hide_missing_parent_invalid_index_or_foreign_SQL(fault):
    graph, profile = storage_graph()
    relations = [row for row in graph["objects"] if row["payload"].get("catalog") == "pg_class"]
    parent = next(row for row in relations if row["payload"]["state"]["relkind"] == "r")
    toast = next(row for row in relations if row["payload"]["state"]["relkind"] == "t")
    index = next(row for row in relations if row["payload"]["state"]["relkind"] == "i")
    if fault == "parent":
        parent["payload"]["state"]["reltoastrelid"] = adapter.tree_ref("pg_class", 0)
    elif fault == "name":
        toast["payload"]["state"]["relname"] = "UNPROVED"
    elif fault == "definition":
        index["payload"]["attachments"]["pg_index"][0]["definitions"]["definition"] = "CREATE UNIQUE INDEX foreign ON public.t (id)"
    else:
        index["payload"]["attachments"]["pg_index"][0]["state"]["indis" + fault] = False
    with pytest.raises(ValueError):
        graph_instance(graph, profile).normalized()


def ri_graph(oid=400):
    converted = convert(with_table(packet()))
    graph = deepcopy(converted["graph"])
    name = "RI_ConstraintTrigger_c_" + str(oid)
    for catalog, number, kind, names in (("pg_constraint", 200, "table constraint", ["public", "controlled_table", "fk"]),
            ("pg_proc", 300, "function", ["pg_catalog", "RI_FKey_check_ins"]),
            ("pg_trigger", oid, "trigger", ["public", "controlled_table", name])):
        graph["resolutions"].append(dict(address=adapter.ref(catalog, number),
            identity=dict(type=kind, object_names=names, object_args=[]), evidence_pointer="CONTROLLED_RI"))
    graph["objects"].append(dict(kind="trigger", address=adapter.ref("pg_trigger", oid), evidence_pointer="CONTROLLED_RI",
        payload=dict(name=name, table=adapter.ref("pg_class", 100), constraint=adapter.ref("pg_constraint", 200),
            constraint_kind="f", function=adapter.ref("pg_proc", 300), event_type=5, arguments_base64="",
            deferrable=False, initially_deferred=False, enabled="O", condition=None, internal=True,
            definition="CREATE CONSTRAINT TRIGGER " + name + " AFTER INSERT ON public.controlled_table EXECUTE FUNCTION RI_FKey_check_ins()",
            columns=[], parent=adapter.ref("pg_trigger", 0), index=adapter.ref("pg_class", 0), native_state={}, attachments={})))
    return graph, converted["profile"]


def test_internal_RI_OID_alias_preserves_enabled_state_and_function():
    left, profile = ri_graph(400)
    right, _ = ri_graph(900)
    assert graph_instance(left, profile).normalized()[0] == graph_instance(right, profile).normalized()[0]
    right["objects"][-1]["payload"]["enabled"] = "D"
    assert graph_instance(left, profile).normalized()[0] != graph_instance(right, profile).normalized()[0]


@pytest.mark.parametrize("fault", ["name", "constraint_kind", "definition"])
def test_RI_alias_requires_exact_native_proof(fault):
    graph, profile = ri_graph()
    graph["objects"][-1]["payload"][fault] = "UNPROVEN"
    with pytest.raises(ValueError):
        graph_instance(graph, profile).normalized()
