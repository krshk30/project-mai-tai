"""Offline, lossless-evidence whole-object comparison; no DB/SQL/APP imports.

Needs independently audited COMPLETE native object graphs, not pg_dump text
alone or old21 projections. Missing resolver/profile/closure means REFUSE.
No approval, catalog read, target mutation or automatic delta allowlist is made.
"""

import argparse
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
import os
from pathlib import Path
import re

VERSION = "L12_WHOLE_SCHEMA_SEMANTIC_GRAPH_V1"
PROFILE_VERSION = "L12_WHOLE_SCHEMA_NATIVE_FIELD_PROFILE_V1"
RAW_PROFILE_VERSION = "L12_SOURCE_CLOSED_NATIVE_RAW_ADAPTER_PROFILE_V2"
MAX_BYTES, MAX_OBJECTS, MAX_RESOLUTIONS, MAX_EDGES = 64 * 1024**2, 32768, 100000, 131072
REVISIONS = {"20261023_atr_simulation", "20261023_atr_aggregate", "20261022_l12_retirement"}
FIELD_TYPES = {"scalar", "ref", "ref_vector", "tree", "owner", "trigger_name", "trigger_definition"}
SUPPORTED = {"relation", "column", "constraint", "index", "trigger", "sequence", "policy", "rule",
    "function", "type", "collation", "opclass", "opfamily", "namespace", "extension", "operator", "cast"}
REQUIRED = {
    "relation": {"schema", "name", "kind", "owner", "acl", "column_order", "persistence", "partition", "rls", "replica_identity", "options", "tablespace", "access_method"},
    "column": {"name", "type", "collation", "default", "not_null", "identity", "generated", "physical_attnum", "storage", "compression", "acl"},
    "constraint": {"name", "table", "columns", "referenced_table", "referenced_columns", "definition", "kind", "validated", "deferrable", "initially_deferred", "inheritance", "parent", "index", "match_type", "on_update", "on_delete"},
    "index": {"name", "table", "definition", "keys", "opclasses", "collations", "predicate", "expressions", "unique", "primary", "exclusion", "nulls_not_distinct", "valid", "ready", "live", "immediate", "options", "acl", "tablespace"},
    "trigger": {"name", "table", "constraint", "constraint_kind", "function", "event_type", "arguments_base64", "deferrable", "initially_deferred", "enabled", "condition", "internal", "definition", "columns", "parent", "index"},
    "sequence": {"name", "type", "owner", "acl", "start", "increment", "minimum", "maximum", "cache", "cycle", "ownership", "ownership_kind", "default_references"},
    "policy": {"name", "table", "roles", "permissive", "command", "using", "check"},
    "rule": {"name", "table", "event", "enabled", "instead", "definition"},
    "function": {"definition", "owner", "acl", "kind", "security_definer", "leakproof", "volatility", "parallel", "strict", "returns_set", "config", "identity_arguments"},
    "type": {"definition", "owner", "acl", "base", "element", "array", "collation", "not_null", "default", "enum_labels", "composite_columns"},
    "collation": {"namespace", "name", "provider", "deterministic", "encoding", "locale", "ctype", "icu_locale", "icu_rules", "version", "owner"},
    "opclass": {"namespace", "name", "method", "input_type", "key_type", "family", "default", "owner"},
    "opfamily": {"namespace", "name", "method", "owner", "members"},
    "namespace": {"name", "owner", "acl"},
    "extension": {"name", "namespace", "owner", "version", "relocatable", "config", "condition"},
    "operator": {"namespace", "name", "owner", "kind", "left_type", "right_type", "result_type", "function", "commutator", "negator", "restriction", "join", "merge", "hash"},
    "cast": {"source_type", "target_type", "function", "context", "method"},
}
REFERENCE_FIELDS = {
    "relation": {"tablespace", "access_method"}, "column": {"type", "collation"},
    "constraint": {"table", "referenced_table", "parent", "index"}, "index": {"table", "tablespace"},
    "trigger": {"table", "constraint", "function", "parent", "index"},
    "sequence": {"type", "ownership"}, "policy": {"table"}, "rule": {"table"},
    "type": {"base", "element", "array", "collation"},
    "opclass": {"method", "input_type", "key_type", "family"}, "opfamily": {"method"},
    "extension": {"namespace"}, "operator": {"left_type", "right_type", "result_type", "function", "commutator", "negator", "restriction", "join"},
    "cast": {"source_type", "target_type", "function"},
}
VECTOR_FIELDS = {"constraint": {"columns", "referenced_columns"}, "index": {"keys", "opclasses", "collations"},
    "trigger": {"columns"}, "sequence": {"default_references"}, "policy": {"roles"}, "extension": {"config"}}
TREE_FIELDS = {"relation": {"partition"}, "constraint": {"inheritance"}, "type": {"composite_columns"}, "opfamily": {"members"}}


def required_category(kind, field):
    if kind == "native_object":
        return "scalar" if field == "catalog" else "tree"
    if field in {"native_state", "attachments"} and kind == "trigger":
        return "tree"
    if field == "owner":
        return "owner"
    if field in REFERENCE_FIELDS.get(kind, set()):
        return "ref"
    if field in VECTOR_FIELDS.get(kind, set()):
        return "ref_vector"
    if field in TREE_FIELDS.get(kind, set()):
        return "tree"
    if kind == "trigger" and field in {"name", "definition"}:
        return "trigger_name" if field == "name" else "trigger_definition"
    return "scalar"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return sha256(canonical(value)).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_semantic_input_key_STOP")
        value[key] = item
    return value


def decode(raw):
    def reject(value):
        raise ValueError("nonfinite_semantic_input_STOP")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=reject)


def is_sha(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None and value != "0" * 64


def address(value):
    if (not isinstance(value, dict) or set(value) != {"class", "oid", "subid"}
            or not isinstance(value["class"], str) or not re.fullmatch(r"pg_catalog\.pg_[a-z_]+", value["class"])
            or type(value["oid"]) is not int or value["oid"] < 0
            or type(value["subid"]) is not int or value["subid"] < 0 or value["oid"] == 0 and value["subid"] != 0):
        raise ValueError("typed_catalog_class_OID_subobject_address_required")
    return value["class"], value["oid"], value["subid"]


def native_identity(value):
    if (not isinstance(value, dict) or set(value) != {"type", "object_names", "object_args"}
            or not isinstance(value["type"], str) or not value["type"]
            or any(not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key])
                for key in ("object_names", "object_args"))
            or not value["object_names"] and not (value["type"] == "cast" and len(value["object_args"]) == 2
                and all(value["object_args"]))):
        raise ValueError("actual_pg_identify_object_as_address_evidence_required")
    return deepcopy(value)


def profile_checked(profile):
    raw_profile = isinstance(profile, dict) and profile.get("version") == RAW_PROFILE_VERSION
    allowed = {"native_object", "trigger"} if raw_profile else SUPPORTED
    if (not isinstance(profile, dict) or set(profile) != {"version", "producer_source_sha256", "review_sha256", "schemas"}
            or profile["version"] not in {PROFILE_VERSION, RAW_PROFILE_VERSION} or not is_sha(profile["producer_source_sha256"])
            or not is_sha(profile["review_sha256"]) or not isinstance(profile["schemas"], dict)
            or not profile["schemas"] or not set(profile["schemas"]) <= allowed
            or raw_profile and set(profile["schemas"]) != allowed):
        raise ValueError("reviewed_complete_native_graph_producer_profile_required")
    for kind, fields in profile["schemas"].items():
        required = ({"catalog", "state", "attachments", "definitions"} if kind == "native_object" else REQUIRED[kind])
        if raw_profile and kind == "trigger":
            required = required | {"native_state", "attachments"}
        if (not isinstance(fields, dict) or not required <= set(fields)
                or raw_profile and set(fields) != required
                or any(not isinstance(key, str) or category not in FIELD_TYPES for key, category in fields.items())
                or any(fields[field] != required_category(kind, field) for field in required)
                or any(category == "owner" and field != "owner" for field, category in fields.items())):
            raise ValueError("mandatory_semantic_fields_or_reference_classification_missing_STOP")
    return profile


class Graph:
    def __init__(self, raw, expected_sha256, role, profile, owner_mapping):
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_BYTES or sha256(raw).hexdigest() != expected_sha256:
            raise ValueError("bounded_immutable_whole_graph_required")
        self.raw_sha256, self.profile, self.mapping = expected_sha256, profile_checked(profile), owner_mapping
        self.value = value = decode(raw)
        fields = {"version", "role", "producer_source_sha256", "profile_sha256", "closure_audit_sha256", "identity",
            "scope", "objects", "resolutions", "dependencies", "unsupported_objects"}
        if (not isinstance(value, dict) or set(value) != fields or value["version"] != VERSION or value["role"] != role
                or value["producer_source_sha256"] != profile["producer_source_sha256"]
                or value["profile_sha256"] != digest(profile) or not is_sha(value["closure_audit_sha256"])
                or value["scope"] != "WHOLE_DATABASE_SCHEMA_PLUS_REFERENCED_IDENTITIES_NO_OMISSIONS"
                or not isinstance(value["identity"], dict) or value["identity"].get("read_only") is not True
                or not isinstance(value["identity"].get("snapshot"), str) or not value["identity"]["snapshot"]
                or value["unsupported_objects"] != []):
            raise ValueError("actual_complete_audited_whole_scope_required_unknown_objects_STOP")
        context = value["identity"]
        required_context = {"database", "role", "system_identifier", "postmaster_started", "server_version_num", "snapshot", "observed_at", "read_only"}
        if (not required_context <= set(context) or any(not isinstance(context[key], str) or not context[key]
                for key in required_context - {"read_only"}) or not context["system_identifier"].isdecimal()
                or int(context["system_identifier"]) <= 0 or not context["server_version_num"].isdecimal()
                or not 160000 <= int(context["server_version_num"]) < 170000):
            raise ValueError("actual_PG16_database_role_epoch_snapshot_context_required")
        for key, maximum in (("objects", MAX_OBJECTS), ("resolutions", MAX_RESOLUTIONS), ("dependencies", MAX_EDGES)):
            if not isinstance(value[key], list) or len(value[key]) > maximum:
                raise ValueError("whole_graph_count_bound_STOP_not_truncated")
        self.registry, self.nodes, self.transforms, self.derived = {}, {}, [], {}
        for row in value["resolutions"]:
            if (not isinstance(row, dict) or set(row) != {"address", "identity", "evidence_pointer"}
                    or not isinstance(row["evidence_pointer"], str) or not row["evidence_pointer"]):
                raise ValueError("every_resolved_address_needs_actual_native_evidence_pointer")
            key, identity = address(row["address"]), native_identity(row["identity"])
            if key[1] == 0 or key in self.registry:
                raise ValueError("zero_duplicate_or_conflicting_resolution_STOP")
            self.registry[key] = identity
        if len({canonical(identity) for identity in self.registry.values()}) != len(self.registry):
            raise ValueError("native_stable_identity_resolution_collision_STOP")
        for row in value["objects"]:
            if (not isinstance(row, dict) or set(row) != {"kind", "address", "payload", "evidence_pointer"}
                    or row["kind"] not in profile["schemas"] or not isinstance(row["evidence_pointer"], str) or not row["evidence_pointer"]
                    or not isinstance(row["payload"], dict) or set(row["payload"]) != set(profile["schemas"][row["kind"]])):
                raise ValueError("unknown_kind_field_or_omitted_object_payload_STOP")
            key = address(row["address"])
            if key[1] == 0 or key in self.nodes:
                raise ValueError("zero_duplicate_object_address_STOP")
            self.resolve(row["address"])
            self.nodes[key] = row
        if self.profile["version"] == RAW_PROFILE_VERSION:
            self.native_storage_identities()
        # Only fully proven internal RI identifiers get a stable generated name.
        for row in value["objects"]:
            if row["kind"] == "trigger" and row["payload"]["internal"] is True:
                payload = row["payload"]
                match = re.fullmatch(r"RI_ConstraintTrigger_([ac])_(\d+)", payload["name"])
                if match is None or int(match[2]) != row["address"]["oid"] or payload["constraint_kind"] != "f":
                    raise ValueError("unrecognized_internal_trigger_identity_STOP")
                own = self.resolve(row["address"])
                if own["object_names"][-1] != payload["name"]:
                    raise ValueError("raw_native_RI_trigger_name_contradiction_STOP")
                stable = dict(type="L12_VALIDATED_INTERNAL_RI_TRIGGER", object_names=[canonical(dict(
                    table=self.resolve(payload["table"]), constraint=self.resolve(payload["constraint"]),
                    function=self.resolve(payload["function"]), action=match[1], event_type=payload["event_type"],
                    arguments_base64=payload["arguments_base64"], condition=payload["condition"],
                    deferrable=payload["deferrable"], initially_deferred=payload["initially_deferred"])).decode()], object_args=[])
                self.registry[address(row["address"])] = stable
                self.transforms.append(dict(operation="VALIDATED_INTERNAL_RI_IDENTITY", address=row["address"], raw=own, stable=stable))

    def native_storage_identities(self):
        """Prove generated TOAST identities from actual parent references only."""
        relations = [row for row in self.value["objects"] if row["kind"] == "native_object"
            and row["payload"]["catalog"] == "pg_class"]

        def typed_ref(state, name):
            raw = state.get(name)
            if not isinstance(raw, dict) or set(raw) != {"$ref"}:
                raise ValueError("native_parent_reference_required_for_derived_identity")
            return address(raw["$ref"])

        def alias(row, kind, parent, raw_name):
            token = address(row["address"])
            original = self.resolve(row["address"])
            stable = dict(type="L12_PROVEN_NATIVE_" + kind.upper(),
                object_names=[canonical(parent).decode()], object_args=[])
            self.registry[token] = stable
            self.derived[token] = dict(kind=kind, raw_name=raw_name,
                token="L12_" + kind.upper() + "_" + digest(stable), parent=parent)
            self.transforms.append(dict(operation="PROVEN_GENERATED_NATIVE_IDENTITY", address=row["address"],
                raw=original, stable=stable))

        for toast in relations:
            state = toast["payload"]["state"]
            if state.get("relkind") != "t":
                continue
            own = address(toast["address"])
            parents = [row for row in relations if typed_ref(row["payload"]["state"], "reltoastrelid") == own]
            if (len(parents) != 1 or parents[0]["payload"]["state"].get("relkind") not in {"r", "m"}
                    or state.get("relname") != "pg_toast_" + str(parents[0]["address"]["oid"])
                    or self.resolve(state["relnamespace"]["$ref"])["object_names"] != ["pg_toast"]):
                raise ValueError("unproved_native_TOAST_parent_or_generated_name_STOP")
            alias(toast, "toast", self.resolve(parents[0]["address"]), state["relname"])

        for index in relations:
            state = index["payload"]["state"]
            if state.get("relkind") != "i":
                continue
            rows = index["payload"]["attachments"].get("pg_index", [])
            if len(rows) != 1:
                raise ValueError("one_actual_native_index_record_required")
            table = typed_ref(rows[0]["state"], "indrelid")
            if table not in self.derived:
                continue
            toast = self.derived[table]
            if (toast["kind"] != "toast" or state.get("relname") != toast["raw_name"] + "_index"
                    or any(rows[0]["state"].get(field) is not True
                        for field in ("indisunique", "indisvalid", "indisready", "indislive"))):
                raise ValueError("unproved_native_TOAST_index_STOP")
            alias(index, "toast_index", self.registry[table], state["relname"])
            self.derived[address(index["address"])]["table_address"] = table

        for row in self.value["objects"]:
            if row["kind"] != "native_object" or row["payload"]["catalog"] != "pg_attribute":
                continue
            state, own = row["payload"]["state"], address(row["address"])
            parent = typed_ref(state, "attrelid")
            if parent not in self.derived:
                continue
            raw = self.resolve(row["address"])
            if (not raw["object_names"] or raw["object_names"][-1] != state.get("attname")
                    or own[:2] != parent[:2] or own[2] != state.get("attnum")):
                raise ValueError("native_TOAST_column_identity_contradiction_STOP")
            alias(row, "toast_column", dict(parent=self.registry[parent], name=state["attname"]), state["attname"])

    def native_tree(self, row, field, original):
        token = address(row["address"])
        rule = self.derived.get(token)
        value = deepcopy(original)
        if rule is None:
            return value
        if field == "state" and rule["kind"] in {"toast", "toast_index"}:
            if value.get("relname") != rule["raw_name"]:
                raise ValueError("derived_relation_name_contradiction_STOP")
            value["relname"] = rule["token"]
        if field == "attachments" and rule["kind"] == "toast_index":
            entries = value.get("pg_index", [])
            if len(entries) != 1:
                raise ValueError("one_native_TOAST_index_definition_required")
            definition = entries[0]["definitions"].get("definition")
            parent = self.derived[rule["table_address"]]
            def quoted(name):
                return '(?:"' + re.escape(name) + '"|' + re.escape(name) + ')'
            pattern = r"^(CREATE UNIQUE INDEX )" + quoted(rule["raw_name"]) + r"( ON )" + quoted("pg_toast") + r"\." + quoted(parent["raw_name"]) + r"(?= )"
            matched = re.match(pattern, definition) if isinstance(definition, str) else None
            if matched is None:
                raise ValueError("native_TOAST_index_definition_identity_mismatch_STOP")
            entries[0]["definitions"]["definition"] = matched[1] + rule["token"] + matched[2] + "pg_toast." + parent["token"] + definition[matched.end():]
        if value != original:
            self.transforms.append(dict(operation="PROVEN_NATIVE_DERIVED_FIELD", address=row["address"],
                field=field, raw=original, stable=value))
        return value

    def resolve(self, raw):
        key = address(raw)
        if key[1] == 0:
            return dict(type="ZERO_SENTINEL", object_names=[key[0]], object_args=[])
        if key not in self.registry:
            raise ValueError("unresolved_typed_OID_or_attnum_STOP_no_guess_or_raw_number_equivalence")
        return deepcopy(self.registry[key])

    def normalized(self):
        result, pointers = {}, {}
        for raw in self.value["objects"]:
            payload, kind = raw["payload"], raw["kind"]
            identity = self.resolve(raw["address"])
            key = canonical(dict(kind=kind, identity=identity)).decode()
            if key in result:
                raise ValueError("stable_object_identity_collision_STOP")
            normalized = {}
            for field, category in self.profile["schemas"][kind].items():
                original = payload[field]
                if category == "scalar":
                    value = deepcopy(original)
                elif category == "ref":
                    value = self.resolve(original)
                elif category == "ref_vector":
                    if not isinstance(original, list):
                        raise ValueError("ordered_reference_vector_required")
                    value = [self.resolve(item) for item in original]
                elif category == "tree":
                    value = self.reference_tree(self.native_tree(raw, field, original)
                        if self.profile["version"] == RAW_PROFILE_VERSION and kind == "native_object" else original)
                    if self.profile["version"] == RAW_PROFILE_VERSION and field == "attachments":
                        if not isinstance(value, dict) or any(not isinstance(rows, list) for rows in value.values()):
                            raise ValueError("native_auxiliary_catalog_multisets_required")
                        # Only catalog row collections are multisets. Ordered
                        # vectors/SQL bodies within each row remain untouched.
                        value = {catalog: sorted(rows, key=canonical) for catalog, rows in value.items()}
                elif category == "owner":
                    if (not isinstance(original, dict) or set(original) != {"name", "raw_oid"}
                            or not isinstance(original["name"], str) or not original["name"]
                            or type(original["raw_oid"]) is not int or original["raw_oid"] <= 0):
                        raise ValueError("actual_owner_name_and_raw_OID_required")
                    value = {"owner_role": self.mapping.get(original["name"], original["name"])}
                else:
                    if kind != "trigger" or field not in {"name", "definition"}:
                        raise ValueError("trigger_identifier_normalization_not_general_SQL_rewriter")
                    value = original
                    if payload["internal"] is True:
                        token = "L12_RI_" + digest(identity)
                        if category == "trigger_name":
                            value = token
                        else:
                            pattern = r'^(CREATE (?:CONSTRAINT )?TRIGGER )(?:(?:"' + re.escape(payload["name"]) + r'")|(?:' + re.escape(payload["name"]) + r'))(?= )'
                            matched = re.match(pattern, original)
                            if matched is None:
                                raise ValueError("RI_definition_identifier_mismatch_STOP")
                            value = matched[1] + token + original[matched.end():]
                normalized[field] = value
                if original != value:
                    self.transforms.append(dict(operation=category, object=key, field=field, raw=original, stable=value))
            result[key] = dict(kind=kind, identity=identity, payload=normalized)
            pointers[key] = dict(address=raw["address"], evidence_pointer=raw["evidence_pointer"], raw_payload_sha256=digest(payload))
        # Dependencies are a multiset, preserving edge multiplicity and deptype.
        edges = Counter()
        for edge in self.value["dependencies"]:
            allowed_deptypes = {"n", "a", "i", "e", "x", "P", "S"}
            if self.profile["version"] == RAW_PROFILE_VERSION:
                allowed_deptypes |= {"p", "shared:o", "shared:a", "shared:r", "shared:t", "shared:p"}
            if (not isinstance(edge, dict) or set(edge) != {"dependent", "referenced", "deptype", "evidence_pointer"}
                    or edge["deptype"] not in allowed_deptypes
                    or not isinstance(edge["evidence_pointer"], str) or not edge["evidence_pointer"]):
                raise ValueError("complete_named_dependency_edge_required")
            key = canonical(dict(kind="DEPENDENCY", dependent=self.resolve(edge["dependent"]),
                referenced=self.resolve(edge["referenced"]), deptype=edge["deptype"])).decode()
            edges[key] += 1
        for key, count in edges.items():
            if key in result:
                raise ValueError("edge_object_key_collision_STOP")
            result[key] = dict(edge=decode(key), multiplicity=count)
            pointers[key] = {"evidence_pointer": "dependencies", "multiplicity": count}
        return result, pointers

    def reference_tree(self, value):
        if isinstance(value, dict):
            if "$ref" in value:
                if set(value) != {"$ref"}:
                    raise ValueError("explicit_reference_leaf_cannot_hide_other_fields")
                return {"$resolved": self.resolve(value["$ref"])}
            if "$resolved" in value:
                raise ValueError("caller_cannot_supply_pre_resolved_native_identity")
            return {key: self.reference_tree(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.reference_tree(item) for item in value]
        return value


def difference(before, after, pointers_before, pointers_after):
    rows = []
    for key in sorted(set(before) | set(after)):
        left, right = before.get(key), after.get(key)
        if left != right:
            rows.append(dict(object_key=key, action="ADD" if left is None else "REMOVE" if right is None else "REPLACE",
                before_sha256=None if left is None else digest(left), after_sha256=None if right is None else digest(right),
                before=left, after=right, before_pointer=pointers_before.get(key), after_pointer=pointers_after.get(key)))
    return rows


def compare(graphs, profile, owner_review, ledger):
    if len(graphs) != 3 or [row.value["role"] for row in graphs] != ["PRODUCTION_BASELINE", "PRIVATE_BASELINE", "PRIVATE_UPGRADED"]:
        raise ValueError("three_complete_whole_schema_observations_required")
    hashes = {graph.value["role"]: graph.raw_sha256 for graph in graphs}
    if (not isinstance(owner_review, dict) or set(owner_review) != {"authority_sha256", "review_sha256", "mapping"}
            or not is_sha(owner_review["authority_sha256"]) or not is_sha(owner_review["review_sha256"])
            or not isinstance(owner_review["mapping"], dict) or len(owner_review["mapping"]) > 64
            or any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
                for key, value in owner_review["mapping"].items())):
        raise ValueError("actual_C10409_explicit_owner_mapping_review_required")
    if (any(graph.mapping != (owner_review["mapping"] if index == 0 else {}) for index, graph in enumerate(graphs))
            or any(graph.profile != profile for graph in graphs)):
        raise ValueError("exact_shared_profile_and_source_only_owner_mapping_required")
    if (not isinstance(ledger, dict) or set(ledger) != {"authority_sha256", "review_sha256", "source_pins", "entries"}
            or ledger["authority_sha256"] != owner_review["authority_sha256"] or not is_sha(ledger["review_sha256"])
            or not isinstance(ledger["source_pins"], dict) or not ledger["source_pins"]
            or any(not is_sha(value) for value in ledger["source_pins"].values()) or not isinstance(ledger["entries"], list)):
        raise ValueError("independently_reviewed_literal_three_migration_delta_ledger_required")
    normalized = [graph.normalized() for graph in graphs]
    baseline = difference(normalized[0][0], normalized[1][0], normalized[0][1], normalized[1][1])
    changes = difference(normalized[1][0], normalized[2][0], normalized[1][1], normalized[2][1])
    expected = {}
    for item in ledger["entries"]:
        if (not isinstance(item, dict) or set(item) != {"revision", "object_key", "action", "before_sha256", "after_sha256", "source_pointer"}
                or item["revision"] not in REVISIONS or item["action"] not in {"ADD", "REPLACE", "REMOVE"}
                or not isinstance(item["object_key"], str) or item["object_key"] in expected or not item["source_pointer"]
                or item["object_key"] == "*"
                or item["action"] in {"ADD", "REPLACE"} and not is_sha(item["after_sha256"])
                or item["action"] == "REMOVE" and item["after_sha256"] is not None
                or item["action"] == "ADD" and item["before_sha256"] is not None
                or item["action"] in {"REPLACE", "REMOVE"} and not is_sha(item["before_sha256"])):
            raise ValueError("exact_per_object_source_proven_delta_required_no_wildcards")
        expected[item["object_key"]] = item
    unapproved, matched = [], []
    for row in changes:
        approved = expected.get(row["object_key"])
        if approved is None or any(row[field] != approved[field] for field in ("action", "before_sha256", "after_sha256")):
            unapproved.append(row)
        else:
            matched.append(row["object_key"])
    missing = sorted(set(expected) - set(matched))
    return dict(version=VERSION, status="STOP_BASELINE_DRIFT_OR_UNAPPROVED_MIGRATION_DELTA" if baseline or unapproved or missing
        else "EXACT_REVIEWED_THREE_MIGRATION_DELTA_REPORTED_NOT_CANONICAL_APPROVAL", graph_sha256=hashes,
        baseline_differences=baseline, migration_differences=changes, unapproved_changes=unapproved, unfulfilled_ledger_entries=missing,
        transforms={graph.value["role"]: graph.transforms for graph in graphs},
        raw_identity_contexts={graph.value["role"]: graph.value["identity"] for graph in graphs},
        owner_review=owner_review, ledger_review_sha256=ledger["review_sha256"], producer_profile_sha256=digest(profile),
        permission_granted=False, canonical_approval=False)


def read(item):
    if not isinstance(item, dict) or set(item) != {"path", "sha256"} or not is_sha(item["sha256"]):
        raise ValueError("exact_hashed_actual_private_artifact_descriptor_required")
    path = Path(item["path"])
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("bounded_canonical_actual_artifact_required")
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != item["sha256"]:
        raise ValueError("actual_semantic_artifact_hash_changed_STOP")
    return raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--binding-sha256", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    binding = decode(read(dict(path=args.binding, sha256=args.binding_sha256)))
    if set(binding) != {"version", "profile", "owner_review", "ledger", "graphs", "independent_audit"} or binding["version"] != VERSION:
        raise ValueError("exact_offline_whole_graph_comparison_binding_required")
    profile, owner, ledger = (decode(read(binding[key])) for key in ("profile", "owner_review", "ledger"))
    roles = ("PRODUCTION_BASELINE", "PRIVATE_BASELINE", "PRIVATE_UPGRADED")
    if set(binding["graphs"]) != set(roles):
        raise ValueError("all_three_graphs_required")
    audit = decode(read(binding["independent_audit"]))
    if (audit.get("purpose") != "MAIN_INDEPENDENT_COMPLETE_WHOLE_SCHEMA_GRAPH_AUDIT"
            or audit.get("graph_sha256") != {role: binding["graphs"][role]["sha256"] for role in roles}
            or audit.get("whole_scope_and_reference_resolution_complete") is not True
            or audit.get("readonly_rollback_backend_closure_verified") is not True):
        raise ValueError("actual_complete_independent_graph_audit_required_not_self_approval")
    graphs = [Graph(read(binding["graphs"][role]), binding["graphs"][role]["sha256"], role, profile,
        owner.get("mapping", {}) if index == 0 else {}) for index, role in enumerate(roles)]
    result = compare(graphs, profile, owner, ledger)
    result["independent_audit_sha256"] = binding["independent_audit"]["sha256"]
    out = Path(args.out)
    if not out.is_absolute() or out.parent.resolve() != out.parent or out.exists() or out.is_symlink():
        raise ValueError("fresh_absolute_private_comparison_output_required")
    with os.fdopen(os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as stream:
        stream.write(canonical(result) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    if result["status"].startswith("STOP"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
