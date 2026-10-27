"""Offline raw-native -> GraphV1 adapter. No files, SQL, APP or process IO.

The explicit V2 field profile preserves complete source catalog state while
classifying physical storage identifiers separately. Raw evidence is immutable
and remains required. No source/canonical permission or target approval here.
"""

from copy import deepcopy
from hashlib import sha256
import base64
import re

from tools import l12_native_schema_graph as native
from tools import l12_whole_schema_semantics as semantic

VERSION = "L12_NATIVE_RAW_TO_GRAPH_V1_ADAPTER_V1"
SOURCE_PATHS = ("tools/l12_native_schema_graph.py", "tools/l12_native_graph_adapter.py",
    "tools/l12_whole_schema_semantics.py")
# These are storage/statistics, not pg_dump schema definitions. Values remain
# in the raw packet and every exclusion is named in the normalization report.
PHYSICAL = {
    "pg_class": {"relfilenode", "relpages", "reltuples", "relallvisible", "relfrozenxid", "relminmxid"},
    "pg_attribute": {"atthasmissing", "attmissingval"},
}
DEPARSED = {
    "pg_class": {"relpartbound": "partition_bound"}, "pg_attrdef": {"adbin": "definition"},
    "pg_constraint": {"conbin": "definition"}, "pg_index": {"indexprs": "expressions", "indpred": "predicate"},
    "pg_trigger": {"tgqual": "condition"}, "pg_policy": {"polqual": "using_expression", "polwithcheck": "check_expression"},
    "pg_rewrite": {"ev_action": "definition", "ev_qual": "definition"},
    "pg_type": {"typdefaultbin": "default_expression"},
    "pg_proc": {"proargdefaults": "argument_defaults", "prosqlbody": "definition"},
    "pg_partitioned_table": {"partexprs": "expressions"},
}
ENRICH_FIELDS = {
    "pg_class": {"definition", "partition_bound"}, "pg_attrdef": {"definition"}, "pg_constraint": {"definition"},
    "pg_index": {"definition", "predicate", "expressions"}, "pg_trigger": {"definition", "condition"},
    "pg_rewrite": {"definition"}, "pg_policy": {"using_expression", "check_expression"},
    "pg_proc": {"definition", "identity_arguments", "argument_defaults"},
    "pg_type": {"formatted_type", "default_expression"}, "pg_partitioned_table": {"definition", "expressions"},
}
RELATIVE_COLUMNS = {
    "pg_constraint": (("conrelid", "conkey"), ("confrelid", "confkey"), ("conrelid", "confdelsetcols")),
    "pg_index": (("indrelid", "indkey"),), "pg_trigger": (("tgrelid", "tgattr"),),
    "pg_partitioned_table": (("partrelid", "partattrs"),),
}
# Mandatory PG16 schema semantics, independent of a packet's self-declared
# layout. Extra actual fields remain included; missing known fields STOP.
CATALOG_MINIMUM = {
    "pg_class": "relname relkind relpersistence relnatts relchecks relhasrules relhastriggers relhassubclass relrowsecurity relforcerowsecurity relispopulated relreplident relispartition reloptions relpartbound relacl",
    "pg_proc": "proname prokind prosecdef proleakproof proisstrict proretset provolatile proparallel pronargs pronargdefaults procost prorows proargmodes proargnames proargdefaults prosrc probin prosqlbody proconfig proacl",
    "pg_type": "typname typlen typbyval typtype typcategory typispreferred typisdefined typdelim typalign typstorage typnotnull typtypmod typndims typdefaultbin typdefault typacl",
    "pg_collation": "collname collprovider collisdeterministic collencoding collcollate collctype colliculocale collicurules collversion",
    "pg_operator": "oprname oprkind oprcanmerge oprcanhash", "pg_opclass": "opcname opcdefault",
    "pg_opfamily": "opfname", "pg_conversion": "conname conforencoding contoencoding condefault",
    "pg_ts_config": "cfgname", "pg_ts_dict": "dictname dictinitoption", "pg_ts_parser": "prsname",
    "pg_ts_template": "tmplname", "pg_namespace": "nspname nspacl",
    "pg_extension": "extname extrelocatable extversion extcondition", "pg_default_acl": "defaclobjtype defaclacl",
    "pg_attrdef": "adnum adbin",
    "pg_constraint": "conname contype condeferrable condeferred convalidated conkey confkey confupdtype confdeltype confmatchtype conislocal coninhcount connoinherit conbin confdelsetcols",
    "pg_trigger": "tgname tgenabled tgisinternal tgtype tgdeferrable tginitdeferred tgnargs tgattr tgargs tgqual tgoldtable tgnewtable",
    "pg_policy": "polname polcmd polpermissive polqual polwithcheck",
    "pg_rewrite": "rulename ev_type ev_enabled is_instead ev_action ev_qual",
    "pg_cast": "castcontext castmethod", "pg_transform": "", "pg_language": "lanname lanispl lanpltrusted lanacl",
    "pg_attribute": "attname attnum attnotnull atthasdef attidentity attgenerated attisdropped attlen atttypmod attndims attbyval attalign attstorage attcompression attstattarget attinhcount attislocal attacl attoptions attfdwoptions attmissingval atthasmissing",
    "pg_index": "indnatts indnkeyatts indisunique indnullsnotdistinct indisprimary indisexclusion indimmediate indisclustered indisvalid indcheckxmin indisready indislive indisreplident indkey indoption indexprs indpred",
    "pg_sequence": "seqstart seqincrement seqmax seqmin seqcache seqcycle", "pg_enum": "enumlabel enumsortorder",
    "pg_range": "", "pg_aggregate": "aggkind aggnumdirectargs aggfinalextra aggmfinalextra aggfinalmodify aggmfinalmodify aggtransspace aggmtransspace agginitval aggminitval",
    "pg_amop": "amopstrategy amoppurpose", "pg_amproc": "amprocnum", "pg_ts_config_map": "maptokentype mapseqno",
    "pg_partitioned_table": "partstrat partnatts partattrs partexprs", "pg_inherits": "inhseqno inhdetachpending",
    "pg_description": "classoid objoid objsubid description", "pg_seclabel": "classoid objoid objsubid provider label",
    "pg_init_privs": "classoid objoid objsubid privtype initprivs", "pg_am": "amname amtype",
}


def key(address):
    return semantic.address(address)


def ref(catalog, oid, subid=0):
    return native.public_address("pg_catalog." + catalog, oid, subid)


def tree_ref(catalog, oid, subid=0):
    return {"$ref": ref(catalog, oid, subid)}


def source_profile(pins, reviewed_profile_sha256):
    if (not isinstance(pins, dict) or set(pins) != set(SOURCE_PATHS)
            or any(not semantic.is_sha(value) for value in pins.values())
            or not semantic.is_sha(reviewed_profile_sha256)):
        raise ValueError("actual_reviewed_adapter_native_semantic_source_pins_required")
    triggers = {field: semantic.required_category("trigger", field) for field in semantic.REQUIRED["trigger"]}
    triggers.update(native_state="tree", attachments="tree")
    return dict(version=semantic.RAW_PROFILE_VERSION,
        producer_source_sha256=pins["tools/l12_native_schema_graph.py"], review_sha256=reviewed_profile_sha256,
        schemas={"native_object": dict(catalog="scalar", state="tree", attachments="tree", definitions="tree"),
            "trigger": triggers})


def adapt(raw, raw_sha256, *, source_pins, profile_review_sha256, closure_audit_sha256, epoch):
    """Main supplies actual immutable packet, independent audit and epoch.

    C11030 uses PRIVATE_BASELINE/PRIVATE_UPGRADED. Neither a restored clone nor
    C1's raw SQL is relabelled PRODUCTION_BASELINE. Graph result is unauthorised
    until the existing independent review/comparison contract is satisfied.
    """
    if (type(raw) is not bytes or not 0 < len(raw) <= semantic.MAX_BYTES
            or sha256(raw).hexdigest() != raw_sha256 or not semantic.is_sha(closure_audit_sha256)):
        raise ValueError("actual_raw_graph_bytes_and_closure_audit_required")
    value = semantic.decode(raw)
    required = {"version", "role", "source_sha256", "context", "selected", "sections", "dependencies",
        "native_identities", "catalog_layout", "raw_node_tree_fields", "status", "canonical_approval",
        "permission_granted", "rollback_verified"}
    profile = source_profile(source_pins, profile_review_sha256)
    if (not isinstance(value, dict) or set(value) != required or value["version"] != native.VERSION
            or value["role"] not in {"PRIVATE_BASELINE", "PRIVATE_UPGRADED", "INDEPENDENT_REFERENCE"}
            or value["source_sha256"] != profile["producer_source_sha256"]
            or value["status"] != "NATIVE_GRAPH_SOURCE_PENDING_SEMANTIC_ADAPTER_AND_ROLLBACK"
            or any(value[field] is not False for field in ("canonical_approval", "permission_granted", "rollback_verified"))
            or value["raw_node_tree_fields"] != {name: list(fields) for name, fields in native.RAW_NODE_TREES.items()}):
        raise ValueError("exact_raw_producer_packet_not_permission_or_production_relabel_required")
    context = value["context"]
    if (not isinstance(epoch, dict) or set(epoch) != {"database", "role", "system_identifier", "postmaster_started", "server_version_num"}
            or any(not isinstance(item, str) or not item for item in epoch.values())
            or not epoch["system_identifier"].isdecimal() or int(epoch["system_identifier"]) <= 0
            or epoch["server_version_num"] != "160015" or context.get("version") != "160015"
            or context.get("read_only") != "on" or context.get("isolation") != "repeatable read"
            or any(epoch[name] != context.get(name) for name in ("database", "role", "postmaster_started"))):
        raise ValueError("actual_same_private_epoch_database_role_readonly_snapshot_required")
    native.classify_layout(value["catalog_layout"])
    layout = {}
    for row in value["catalog_layout"]:
        layout.setdefault(row["catalog"], {})[row["field"]] = row["type_name"]
        if row["type_name"] == "pg_node_tree" and row["field"] not in DEPARSED.get(row["catalog"], {}):
            raise ValueError("unclassified_native_node_tree_STOP")
    if set(CATALOG_MINIMUM) != set(native.CATALOGS):
        raise ValueError("source_catalog_semantic_membership_not_closed")
    for catalog, minimum in CATALOG_MINIMUM.items():
        required_fields = set(minimum.split()) | set(native.OID_FIELDS.get(catalog, {})) | set(native.OID_VECTORS.get(catalog, {}))
        if not required_fields <= set(layout[catalog]):
            raise ValueError("mandatory_native_schema_fields_missing_from_layout_STOP:" + catalog)
    sections = value["sections"]
    expected_sections = set(native.OBJECT_CATALOGS) | set(native.AUXILIARY) | set(native.REFERENCE_CATALOGS) | {
        "pg_description", "pg_seclabel", "pg_init_privs"}
    if not isinstance(sections, dict) or set(sections) != expected_sections:
        raise ValueError("complete_fixed_catalog_sections_required")
    registry, resolutions = {}, []
    for ordinal, row in enumerate(value["native_identities"]):
        address = native.public_address(row["class"], row["oid"], row["subid"])
        identity = semantic.native_identity({name: row[name] for name in ("type", "object_names", "object_args")})
        if key(address) in registry or row["oid"] == 0:
            raise ValueError("duplicate_or_zero_native_resolution_STOP")
        registry[key(address)] = identity
        resolutions.append(dict(address=address, identity=identity,
            evidence_pointer=raw_sha256 + "#/native_identities/" + str(ordinal)))

    transformations, nodes, attached, covered = [], {}, {}, set()

    def normalize(catalog, row, pointer):
        state = deepcopy(row["raw"])
        if set(state) != set(layout[catalog]):
            raise ValueError("every_native_catalog_field_required_no_partial_projection")
        native.row_references(catalog, state, row["typed_fields"], row["typed_vectors"])
        if catalog in {"pg_description", "pg_seclabel", "pg_init_privs"}:
            state["classoid"] = row["class"]
            state["objoid"] = {"$ref": native.public_address(row["class"], row["oid"], state["objsubid"])}
        for field, target in native.OID_FIELDS.get(catalog, {}).items():
            observed = row["typed_fields"][field]
            if type(state[field]) is int and state[field] != observed or (state[field] is None) != (observed is None):
                raise ValueError("native_raw_and_typed_OID_contradiction_STOP")
            state[field] = None if observed is None else tree_ref(target, observed)
        for field, target in native.OID_VECTORS.get(catalog, {}).items():
            if (state[field] is None) != (row["typed_vectors"][field] is None):
                raise ValueError("native_raw_and_typed_vector_NULL_contradiction_STOP")
            state[field] = (None if row["typed_vectors"][field] is None else
                [tree_ref(target, oid) for oid in native.vector(row["typed_vectors"][field])])
        for table_field, column_field in RELATIVE_COLUMNS.get(catalog, ()):
            state[column_field] = (None if row["raw"][column_field] is None else
                [tree_ref("pg_class", row["typed_fields"][table_field], attnum)
                    if attnum else tree_ref("pg_class", 0) for attnum in native.vector(row["raw"][column_field])])
        for field in PHYSICAL.get(catalog, set()):
            if field in state:
                transformations.append(dict(operation="PHYSICAL_NOT_SQL_SCHEMA", pointer=pointer + "/raw/" + field,
                    raw_value=deepcopy(state.pop(field))))
        if "oid" in state:
            transformations.append(dict(operation="CATALOG_ROW_OID_IN_RAW_EVIDENCE", pointer=pointer + "/raw/oid",
                raw_value=state.pop("oid")))
        definitions = {name: deepcopy(item) for name, item in row.items()
            if name not in {"class", "oid", "subid", "raw", "typed_fields", "typed_vectors"}}
        if set(definitions) != ENRICH_FIELDS.get(catalog, set()):
            raise ValueError("exact_native_deparser_projection_required")
        for field, definition in DEPARSED.get(catalog, {}).items():
            if field not in state:
                raise ValueError("expected_native_node_tree_field_missing")
            if state[field] is not None:
                if not isinstance(definitions.get(definition), str) or not definitions[definition]:
                    raise ValueError("nonnull_native_node_tree_requires_actual_deparser_result")
                transformations.append(dict(operation="NATIVE_NODE_TREE_AND_RESOLVED_DEPENDENCIES",
                    pointer=pointer + "/raw/" + field, deparsed_field=definition))
            # Keep null distinct from an actual deparsed expression.
            state[field] = None if state[field] is None else dict(native_deparsed=definitions[definition])
        return state, definitions

    def put(address, catalog, state, definitions, pointer):
        token = key(address)
        if token in nodes or token not in registry:
            raise ValueError("duplicate_or_unresolved_native_object_STOP")
        covered.add(token)
        nodes[token] = dict(kind="native_object", address=address,
            payload=dict(catalog=catalog, state=state, attachments={}, definitions=definitions), evidence_pointer=pointer)

    for catalog in native.OBJECT_CATALOGS:
        for ordinal, row in enumerate(sections[catalog]):
            pointer = raw_sha256 + "#/sections/" + catalog + "/" + str(ordinal)
            address = native.public_address(row["class"], row["oid"], row["subid"])
            if row["class"] != "pg_catalog." + catalog or row["subid"] != 0:
                raise ValueError("native_object_catalog_address_contradiction")
            state, definitions = normalize(catalog, row, pointer)
            put(address, catalog, state, definitions, pointer)
            if catalog == "pg_trigger":
                raw_trigger = row["raw"]
                constraint_oid = row["typed_fields"]["tgconstraint"]
                constraints = [entry for entry in sections["pg_constraint"] if entry["oid"] == constraint_oid]
                constraint_kind = constraints[0]["raw"]["contype"] if len(constraints) == 1 else None
                if raw_trigger["tgisinternal"] and constraint_kind != "f":
                    raise ValueError("unknown_internal_non_FK_trigger_STOP")
                encoded = raw_trigger["tgargs"]
                if not isinstance(encoded, str) or not re.fullmatch(r"\\x(?:[0-9a-fA-F]{2})*", encoded):
                    raise ValueError("native_trigger_bytea_hex_required")
                # Covered by GraphV1's proven internal-RI identifier transform.
                state.pop("tgname")
                nodes[key(address)] = dict(kind="trigger", address=address, evidence_pointer=pointer,
                    payload=dict(name=raw_trigger["tgname"], table=ref("pg_class", row["typed_fields"]["tgrelid"]),
                        constraint=ref("pg_constraint", constraint_oid), constraint_kind=constraint_kind,
                        function=ref("pg_proc", row["typed_fields"]["tgfoid"]), event_type=raw_trigger["tgtype"],
                        arguments_base64=base64.b64encode(bytes.fromhex(encoded[2:])).decode(),
                        deferrable=raw_trigger["tgdeferrable"], initially_deferred=raw_trigger["tginitdeferred"],
                        enabled=raw_trigger["tgenabled"], condition=definitions.get("condition"), internal=raw_trigger["tgisinternal"],
                        definition=definitions.get("definition"), columns=[ref("pg_class", row["typed_fields"]["tgrelid"], number)
                            for number in native.vector(raw_trigger["tgattr"])],
                        parent=ref("pg_trigger", row["typed_fields"]["tgparentid"]),
                        index=ref("pg_class", row["typed_fields"]["tgconstrindid"]), native_state=state, attachments={}))

    # Auxiliary rows remain grouped under their real parent address, not an
    # invented pg_identify identity for an enum label or catalog storage row.
    for catalog in tuple(native.AUXILIARY) + ("pg_description", "pg_seclabel", "pg_init_privs"):
        for ordinal, row in enumerate(sections[catalog]):
            pointer = raw_sha256 + "#/sections/" + catalog + "/" + str(ordinal)
            address = native.public_address(row["class"], row["oid"], row["subid"])
            token = key(address)
            state, definitions = normalize(catalog, row, pointer)
            if catalog == "pg_attribute" and row["raw"]["attnum"] > 0 and not row["raw"]["attisdropped"]:
                column = ref("pg_class", row["typed_fields"]["attrelid"], row["raw"]["attnum"])
                put(column, catalog, state, definitions, pointer)
                continue
            if token not in nodes:
                # Referenced builtin families/collations are identities/facts,
                # not falsely labelled user-owned schema object definitions.
                attached.setdefault(token, {}).setdefault(catalog, []).append(dict(state=state, definitions=definitions))
            else:
                nodes[token]["payload"]["attachments"].setdefault(catalog, []).append(dict(state=state, definitions=definitions))
            if type(row["raw"].get("oid")) is int:
                covered.add(("pg_catalog." + catalog, row["raw"]["oid"], 0))

    # Builtin reference facts participate as explicit native objects too. They
    # are not user-schema objects, but cannot be dropped from equivalence.
    for catalog in native.REFERENCE_CATALOGS:
        for ordinal, row in enumerate(sections[catalog]):
            token = key(native.public_address(row["class"], row["oid"], row["subid"]))
            if token in nodes:
                continue
            state, definitions = normalize(catalog, row, raw_sha256 + "#/sections/" + catalog + "/" + str(ordinal))
            if token not in registry:
                raise ValueError("unresolved_builtin_reference_fact")
            put(native.public_address(row["class"], row["oid"], row["subid"]), catalog, state, definitions,
                raw_sha256 + "#/sections/" + catalog + "/" + str(ordinal))
            nodes[token]["payload"]["attachments"] = attached.pop(token, {})
    if attached:
        raise ValueError("unaccounted_auxiliary_native_object_STOP")
    selected = {key(native.public_address(row["class"], row["oid"], row["subid"])) for row in value["selected"]}
    if len(selected) != len(value["selected"]):
        raise ValueError("duplicate_selected_native_address_STOP")
    if not selected <= covered:
        raise ValueError("selected_native_object_has_no_semantic_payload_STOP")
    dependencies = []
    for ordinal, edge in enumerate(value["dependencies"]):
        if edge["catalog"] not in {"pg_depend", "pg_shdepend"}:
            raise ValueError("unknown_native_dependency_catalog_STOP")
        if edge["dependent_class"] is None:
            if (edge["catalog"] != "pg_depend" or edge["deptype"] != "p"
                    or (edge["dependent_class_oid"], edge["objid"], edge["objsubid"]) != (0, 0, 0)):
                raise ValueError("invalid_native_pinned_dependency_sentinel")
            dependent = ref("pg_depend", 0)
        else:
            dependent = native.public_address(edge["dependent_class"], edge["objid"], edge["objsubid"])
        dependencies.append(dict(dependent=dependent,
            referenced=native.public_address(edge["referenced_class"], edge["refobjid"], edge["refobjsubid"]),
            deptype=("shared:" if edge["catalog"] == "pg_shdepend" else "") + edge["deptype"],
            evidence_pointer=raw_sha256 + "#/dependencies/" + str(ordinal)))
    identity = dict(epoch, snapshot=context["snapshot"], observed_at=context["observed_at"], read_only=True,
        raw_native_graph_sha256=raw_sha256)
    graph = dict(version=semantic.VERSION, role=value["role"], producer_source_sha256=profile["producer_source_sha256"],
        profile_sha256=semantic.digest(profile), closure_audit_sha256=closure_audit_sha256, identity=identity,
        scope="WHOLE_DATABASE_SCHEMA_PLUS_REFERENCED_IDENTITIES_NO_OMISSIONS", objects=list(nodes.values()),
        resolutions=resolutions, dependencies=dependencies, unsupported_objects=[])
    encoded = semantic.canonical(graph)
    checked = semantic.Graph(encoded, semantic.digest(graph), value["role"], profile, {})
    checked.normalized()  # Resolve every nested reference before returning.
    transformations.extend(checked.transforms)
    return dict(graph=graph, profile=profile, transformations=transformations,
        status="ADAPTED_NOT_REPLAY_OR_CANONICAL_APPROVAL", raw_graph_sha256=raw_sha256,
        source_pins=source_pins, permission_granted=False, canonical_approval=False)
