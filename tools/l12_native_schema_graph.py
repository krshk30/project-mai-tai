"""Whole-schema native graph SOURCE BODY for Main's existing catalog reader.

No connection/transaction/transport/file/process is created here. Main injects
one existing reviewed RR READ ONLY connection. Output is raw native graph
evidence, NOT the old semantic-graph format, rollback proof or schema approval.
"""

import json
import re

VERSION = "L12_NATIVE_WHOLE_SCHEMA_SOURCE_GRAPH_V1"
MAX_OBJECTS, MAX_EDGES, MAX_BYTES = 32768, 131072, 32 * 1024**2
USER_NS = "nspname NOT IN ('pg_catalog','information_schema') AND nspname !~ '^pg_(toast|temp)'"

# These are catalog identifiers in source, never caller-supplied SQL names.
NAMESPACED = {
    "pg_class": "relnamespace", "pg_proc": "pronamespace", "pg_type": "typnamespace",
    "pg_collation": "collnamespace", "pg_operator": "oprnamespace", "pg_opclass": "opcnamespace",
    "pg_opfamily": "opfnamespace", "pg_conversion": "connamespace", "pg_ts_config": "cfgnamespace",
    "pg_ts_dict": "dictnamespace", "pg_ts_parser": "prsnamespace", "pg_ts_template": "tmplnamespace",
}
ROOTS = " UNION ALL ".join(
    "SELECT 'pg_catalog." + name + "'::regclass::oid AS classid,oid AS objid,0 AS objsubid FROM pg_catalog."
    + name + " WHERE " + namespace + " IN (SELECT oid FROM pg_catalog.pg_namespace WHERE " + USER_NS + ")"
    for name, namespace in NAMESPACED.items())
ROOTS += " UNION ALL SELECT 'pg_catalog.pg_namespace'::regclass::oid,oid,0 FROM pg_catalog.pg_namespace WHERE " + USER_NS
for _catalog in ("pg_extension", "pg_default_acl", "pg_cast", "pg_transform", "pg_language"):
    ROOTS += " UNION ALL SELECT 'pg_catalog." + _catalog + "'::regclass::oid,oid,0 FROM pg_catalog." + _catalog

# Secret-bearing/unmodeled classes are enumerated WITHOUT options/conninfo.
# Nonempty means STOP, not omission, redaction-as-equivalence or permission.
UNSUPPORTED_SQL = """SELECT 'subscription' AS kind,oid::bigint,subname AS name FROM pg_catalog.pg_subscription
UNION ALL SELECT 'foreign_server',oid::bigint,srvname FROM pg_catalog.pg_foreign_server
UNION ALL SELECT 'foreign_data_wrapper',oid::bigint,fdwname FROM pg_catalog.pg_foreign_data_wrapper
UNION ALL SELECT 'user_mapping',oid::bigint,umuser::text FROM pg_catalog.pg_user_mapping
UNION ALL SELECT 'foreign_table',ftrelid::bigint,ftrelid::regclass::text FROM pg_catalog.pg_foreign_table
UNION ALL SELECT 'event_trigger',oid::bigint,evtname FROM pg_catalog.pg_event_trigger
UNION ALL SELECT 'publication',oid::bigint,pubname FROM pg_catalog.pg_publication
UNION ALL SELECT 'large_object_metadata',oid::bigint,oid::text FROM pg_catalog.pg_largeobject_metadata
UNION ALL SELECT 'extended_statistics',oid::bigint,stxname FROM pg_catalog.pg_statistic_ext
ORDER BY kind,oid LIMIT :limit"""

SELECTION_SQL = """WITH RECURSIVE selected(classid,objid,objsubid) AS ((
""" + ROOTS + """
) UNION
SELECT d.classid,d.objid,d.objsubid FROM pg_catalog.pg_depend d JOIN selected s
ON d.refclassid=s.classid AND d.refobjid=s.objid AND (s.objsubid=0 OR d.refobjsubid=s.objsubid)
WHERE d.deptype IN ('i','a','e'))
SELECT n.nspname||'.'||c.relname AS class,s.objid::bigint AS oid,s.objsubid AS subid
FROM selected s JOIN pg_catalog.pg_class c ON c.oid=s.classid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
ORDER BY class,s.objid,s.objsubid LIMIT :limit"""

SELECTED = """WITH selected AS (SELECT class,oid,subid FROM
jsonb_to_recordset(CAST(:addresses AS jsonb)) AS s(class text,oid bigint,subid integer)) """

# Auxiliary native catalog records belong to their real parent object address.
# This avoids inventing an enum-label/pg_index/sequence catalog object identity.
AUXILIARY = {
    "pg_attribute": ("pg_class", "attrelid", "0"), "pg_index": ("pg_class", "indexrelid", "0"),
    "pg_sequence": ("pg_class", "seqrelid", "0"), "pg_enum": ("pg_type", "enumtypid", "0"),
    "pg_range": ("pg_type", "rngtypid", "0"), "pg_aggregate": ("pg_proc", "aggfnoid", "0"),
    "pg_amop": ("pg_opfamily", "amopfamily", "0"), "pg_amproc": ("pg_opfamily", "amprocfamily", "0"),
    "pg_ts_config_map": ("pg_ts_config", "mapcfg", "0"),
    "pg_partitioned_table": ("pg_class", "partrelid", "0"),
    "pg_inherits": ("pg_class", "inhrelid", "0"),
}
OBJECT_CATALOGS = tuple(NAMESPACED) + (
    "pg_namespace", "pg_extension", "pg_default_acl", "pg_attrdef", "pg_constraint", "pg_trigger",
    "pg_policy", "pg_rewrite", "pg_cast", "pg_transform", "pg_language",
)
REFERENCE_CATALOGS = ("pg_collation", "pg_opclass", "pg_opfamily", "pg_am")
ENRICH = {
    "pg_class": "CASE WHEN c.relkind IN ('v','m') THEN pg_catalog.pg_get_viewdef(c.oid,false) ELSE NULL END AS definition,"
        "pg_catalog.pg_get_expr(c.relpartbound,c.oid,false) AS partition_bound",
    "pg_attrdef": "pg_catalog.pg_get_expr(c.adbin,c.adrelid,false) AS definition",
    "pg_constraint": "pg_catalog.pg_get_constraintdef(c.oid,false) AS definition",
    "pg_index": "pg_catalog.pg_get_indexdef(c.indexrelid,0,false) AS definition,"
        "pg_catalog.pg_get_expr(c.indpred,c.indrelid,false) AS predicate,"
        "pg_catalog.pg_get_expr(c.indexprs,c.indrelid,false) AS expressions",
    "pg_trigger": "pg_catalog.pg_get_triggerdef(c.oid,false) AS definition,"
        "pg_catalog.pg_get_expr(c.tgqual,c.tgrelid,false) AS condition",
    "pg_rewrite": "pg_catalog.pg_get_ruledef(c.oid,false) AS definition",
    "pg_policy": "pg_catalog.pg_get_expr(c.polqual,c.polrelid,false) AS using_expression,"
        "pg_catalog.pg_get_expr(c.polwithcheck,c.polrelid,false) AS check_expression",
    "pg_proc": "CASE WHEN c.prokind IN ('f','p') THEN pg_catalog.pg_get_functiondef(c.oid) ELSE NULL END AS definition,"
        "pg_catalog.pg_get_function_identity_arguments(c.oid) AS identity_arguments,"
        "pg_catalog.pg_get_expr(c.proargdefaults,0,false) AS argument_defaults",
    "pg_type": "pg_catalog.format_type(c.oid,NULL) AS formatted_type,"
        "pg_catalog.pg_get_expr(c.typdefaultbin,0,false) AS default_expression",
    "pg_partitioned_table": "pg_catalog.pg_get_partkeydef(c.partrelid) AS definition,"
        "pg_catalog.pg_get_expr(c.partexprs,c.partrelid,false) AS expressions",
}

OID_FIELDS = {
    "pg_class": dict(relnamespace="pg_namespace",reltype="pg_type",reloftype="pg_type",relowner="pg_authid",
        relam="pg_am",reltablespace="pg_tablespace",reltoastrelid="pg_class",relrewrite="pg_class"),
    "pg_attribute": dict(attrelid="pg_class",atttypid="pg_type",attcollation="pg_collation"),
    "pg_attrdef": dict(adrelid="pg_class"),
    "pg_constraint": dict(connamespace="pg_namespace",contypid="pg_type",conrelid="pg_class",confrelid="pg_class",
        conindid="pg_class",conparentid="pg_constraint"),
    "pg_index": dict(indexrelid="pg_class",indrelid="pg_class"),
    "pg_partitioned_table": dict(partrelid="pg_class",partdefid="pg_class"),
    "pg_inherits": dict(inhrelid="pg_class",inhparent="pg_class"),
    "pg_trigger": dict(tgrelid="pg_class",tgfoid="pg_proc",tgconstraint="pg_constraint",tgconstrrelid="pg_class",
        tgconstrindid="pg_class",tgparentid="pg_trigger"),
    "pg_sequence": dict(seqrelid="pg_class",seqtypid="pg_type"),
    "pg_type": dict(typnamespace="pg_namespace",typowner="pg_authid",typrelid="pg_class",typelem="pg_type",
        typarray="pg_type",typbasetype="pg_type",typcollation="pg_collation",typinput="pg_proc",typoutput="pg_proc",
        typreceive="pg_proc",typsend="pg_proc",typmodin="pg_proc",typmodout="pg_proc",typanalyze="pg_proc",typsubscript="pg_proc"),
    "pg_proc": dict(pronamespace="pg_namespace",proowner="pg_authid",prolang="pg_language",prorettype="pg_type",
        provariadic="pg_type",prosupport="pg_proc"),
    "pg_namespace": dict(nspowner="pg_authid"), "pg_extension": dict(extowner="pg_authid",extnamespace="pg_namespace"),
    "pg_collation": dict(collnamespace="pg_namespace",collowner="pg_authid"),
    "pg_operator": dict(oprnamespace="pg_namespace",oprowner="pg_authid",oprleft="pg_type",oprright="pg_type",
        oprresult="pg_type",oprcode="pg_proc",oprcom="pg_operator",oprnegate="pg_operator",oprrest="pg_proc",oprjoin="pg_proc"),
    "pg_opclass": dict(opcnamespace="pg_namespace",opcowner="pg_authid",opcmethod="pg_am",opcintype="pg_type",opckeytype="pg_type",opcfamily="pg_opfamily"),
    "pg_opfamily": dict(opfnamespace="pg_namespace",opfowner="pg_authid",opfmethod="pg_am"),
    "pg_am": dict(amhandler="pg_proc"),
    "pg_amop": dict(amopfamily="pg_opfamily",amoplefttype="pg_type",amoprighttype="pg_type",amopopr="pg_operator",amopmethod="pg_am",amopsortfamily="pg_opfamily"),
    "pg_amproc": dict(amprocfamily="pg_opfamily",amproclefttype="pg_type",amprocrighttype="pg_type",amproc="pg_proc"),
    "pg_policy": dict(polrelid="pg_class"), "pg_rewrite": dict(ev_class="pg_class"),
    "pg_enum": dict(enumtypid="pg_type"),
    "pg_range": dict(rngtypid="pg_type",rngsubtype="pg_type",rngmultitypid="pg_type",rngcollation="pg_collation",
        rngsubopc="pg_opclass",rngcanonical="pg_proc",rngsubdiff="pg_proc"),
    "pg_cast": dict(castsource="pg_type",casttarget="pg_type",castfunc="pg_proc"),
    "pg_transform": dict(trftype="pg_type",trflang="pg_language",trffromsql="pg_proc",trftosql="pg_proc"),
    "pg_language": dict(lanowner="pg_authid",lanplcallfoid="pg_proc",laninline="pg_proc",lanvalidator="pg_proc"),
    "pg_conversion": dict(connamespace="pg_namespace",conowner="pg_authid",conproc="pg_proc"),
    "pg_ts_config": dict(cfgnamespace="pg_namespace",cfgowner="pg_authid",cfgparser="pg_ts_parser"),
    "pg_ts_dict": dict(dictnamespace="pg_namespace",dictowner="pg_authid",dicttemplate="pg_ts_template"),
    "pg_ts_parser": dict(prsnamespace="pg_namespace",prsstart="pg_proc",prstoken="pg_proc",prsend="pg_proc",prsheadline="pg_proc",prslextype="pg_proc"),
    "pg_ts_template": dict(tmplnamespace="pg_namespace",tmplinit="pg_proc",tmpllexize="pg_proc"),
    "pg_ts_config_map": dict(mapcfg="pg_ts_config",mapdict="pg_ts_dict"),
    "pg_default_acl": dict(defaclrole="pg_authid",defaclnamespace="pg_namespace"),
    "pg_aggregate": dict(aggfnoid="pg_proc",aggtransfn="pg_proc",aggfinalfn="pg_proc",aggcombinefn="pg_proc",
        aggserialfn="pg_proc",aggdeserialfn="pg_proc",aggmtransfn="pg_proc",aggminvtransfn="pg_proc",aggmfinalfn="pg_proc",
        aggsortop="pg_operator",aggtranstype="pg_type",aggmtranstype="pg_type"),
}
OID_VECTORS = {
    "pg_index": dict(indclass="pg_opclass",indcollation="pg_collation"),
    "pg_constraint": dict(conexclop="pg_operator",conpfeqop="pg_operator",conppeqop="pg_operator",conffeqop="pg_operator"),
    "pg_proc": dict(proargtypes="pg_type",proallargtypes="pg_type",protrftypes="pg_type"),
    "pg_policy": dict(polroles="pg_authid"), "pg_extension": dict(extconfig="pg_class"),
    "pg_partitioned_table": dict(partclass="pg_opclass",partcollation="pg_collation"),
}
RAW_NODE_TREES = {
    "pg_class": ("relpartbound",),
    "pg_attrdef": ("adbin",), "pg_constraint": ("conbin",), "pg_index": ("indexprs", "indpred"),
    "pg_trigger": ("tgqual",), "pg_policy": ("polqual", "polwithcheck"), "pg_rewrite": ("ev_action", "ev_qual"),
    "pg_type": ("typdefaultbin",), "pg_proc": ("proargdefaults", "prosqlbody"),
    "pg_partitioned_table": ("partexprs",),
}

CATALOGS = tuple(sorted(set(OBJECT_CATALOGS) | set(AUXILIARY) | set(REFERENCE_CATALOGS)
    | {"pg_description", "pg_seclabel", "pg_init_privs"}))
LAYOUT_SQL = """SELECT c.relname AS catalog,a.attname AS field,a.attnum,
t.typname AS type_name,pg_catalog.format_type(a.atttypid,a.atttypmod) AS formatted_type
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
JOIN pg_catalog.pg_type t ON t.oid=a.atttypid
WHERE n.nspname='pg_catalog' AND c.relname IN (""" + ",".join("'" + name + "'" for name in CATALOGS) + """
) ORDER BY c.relname,a.attnum LIMIT :limit"""


def classify_layout(rows):
    """Unknown native OID fields STOP rather than silently lose references."""
    present, seen = set(), set()
    typed = {"oid", "oidvector", "_oid", "regproc", "regprocedure", "regoper", "regoperator",
        "regclass", "regtype", "regconfig", "regdictionary", "regnamespace", "regrole", "regcollation"}
    special = {("pg_class", "relfilenode"), ("pg_description", "classoid"), ("pg_description", "objoid"),
        ("pg_seclabel", "classoid"), ("pg_seclabel", "objoid"),
        ("pg_init_privs", "classoid"), ("pg_init_privs", "objoid")}
    for row in rows:
        name, field = row["catalog"], row["field"]
        if name not in CATALOGS or (name, field) in seen:
            raise ValueError("native_catalog_layout_unknown_or_duplicate_STOP")
        seen.add((name, field))
        present.add(name)
        if row["type_name"] in typed and not (field == "oid"
                or field in OID_FIELDS.get(name, {}) or field in OID_VECTORS.get(name, {})
                or (name, field) in special):
            raise ValueError("unclassified_native_oid_field_STOP:" + name + "." + field)
    if present != set(CATALOGS):
        raise ValueError("complete_fixed_native_catalog_layout_required")
    return rows


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def address(catalog, oid, subid=0):
    if (not isinstance(catalog, str) or not re.fullmatch(r"pg_catalog\.pg_[a-z_]+", catalog)
            or type(oid) is not int or oid < 0 or type(subid) is not int or subid < 0
            or oid == 0 and subid != 0):
        raise ValueError("native_typed_address_required")
    return dict(class_=catalog, oid=oid, subid=subid)


def public_address(catalog, oid, subid=0):
    value = address(catalog, oid, subid)
    return {"class": value["class_"], "oid": value["oid"], "subid": value["subid"]}


def vector(value):
    if value is None:
        return []
    if isinstance(value, str):
        if not re.fullmatch(r"\{(?:\d+(?:,\d+)*)?\}|\d+(?: \d+)*|", value):
            raise ValueError("unknown_native_oid_vector_format_STOP")
        value = [int(item) for item in re.findall(r"\d+", value)]
    if not isinstance(value, list) or any(type(item) is not int or item < 0 for item in value):
        raise ValueError("typed_native_oid_vector_required")
    return value


def row_references(catalog, raw, typed_fields, typed_vectors):
    # regproc JSON text can be a function name. Resolve via explicit native
    # SQL casts instead of trying to infer an OID from that text.
    if (not isinstance(typed_fields, dict) or set(typed_fields) != set(OID_FIELDS.get(catalog, {}))
            or not isinstance(typed_vectors, dict) or set(typed_vectors) != set(OID_VECTORS.get(catalog, {}))):
        raise ValueError("complete_native_typed_reference_sidechannels_required")
    result = []
    for field, target in OID_FIELDS.get(catalog, {}).items():
        if field not in raw:
            raise ValueError("PG16_native_reference_field_missing:" + catalog + "." + field)
        if typed_fields[field] is not None:
            result.append(public_address("pg_catalog." + target, typed_fields[field]))
    for field, target in OID_VECTORS.get(catalog, {}).items():
        if field not in raw:
            raise ValueError("PG16_native_reference_vector_missing:" + catalog + "." + field)
        result.extend(public_address("pg_catalog." + target, value) for value in vector(typed_vectors[field]))
    for table_field, columns_field in {"pg_constraint": (("conrelid", "conkey"), ("confrelid", "confkey"), ("conrelid", "confdelsetcols")),
            "pg_index": (("indrelid", "indkey"),), "pg_trigger": (("tgrelid", "tgattr"),),
            "pg_partitioned_table": (("partrelid", "partattrs"),)}.get(catalog, ()):
        for attnum in vector(raw[columns_field]):
            if attnum:
                result.append(public_address("pg_catalog.pg_class", typed_fields[table_field], attnum))
    if catalog == "pg_attribute" and not raw["attisdropped"] and raw["attnum"] > 0:
        result.append(public_address("pg_catalog.pg_class", typed_fields["attrelid"], raw["attnum"]))
    return result


def catalog_query(catalog):
    if catalog not in OBJECT_CATALOGS and catalog not in AUXILIARY and catalog not in REFERENCE_CATALOGS:
        raise ValueError("fixed_native_catalog_identifier_only")
    parent, oid, subid = AUXILIARY.get(catalog, (catalog, "oid", "0"))
    extra = "," + ENRICH[catalog] if catalog in ENRICH else ""
    fields = ",".join("'" + field + "',c." + field + "::oid::bigint" for field in OID_FIELDS.get(catalog, {}))
    vectors = ",".join("'" + field + "',to_jsonb(c." + field + "::oid[]::bigint[])" for field in OID_VECTORS.get(catalog, {}))
    return SELECTED + "SELECT 'pg_catalog." + parent + "' AS class,c." + oid + "::bigint AS oid," + subid + " AS subid," \
        "to_jsonb(c) AS raw,jsonb_build_object(" + fields + ") AS typed_fields," \
        "jsonb_build_object(" + vectors + ") AS typed_vectors" + extra + " FROM pg_catalog." + catalog + " c WHERE EXISTS (SELECT 1 FROM selected s " \
        "WHERE s.class='pg_catalog." + parent + "' AND s.oid=c." + oid + ") ORDER BY c." + oid + " LIMIT :limit"


DEPENDENCIES_SQL = SELECTED + """SELECT 'pg_depend' AS catalog,
nc.nspname||'.'||cc.relname AS dependent_class,d.classid::bigint AS dependent_class_oid,d.objid::bigint,d.objsubid,
nr.nspname||'.'||cr.relname AS referenced_class,d.refobjid::bigint,d.refobjsubid,d.deptype::text
FROM pg_catalog.pg_depend d LEFT JOIN pg_catalog.pg_class cc ON cc.oid=d.classid
LEFT JOIN pg_catalog.pg_namespace nc ON nc.oid=cc.relnamespace
JOIN pg_catalog.pg_class cr ON cr.oid=d.refclassid JOIN pg_catalog.pg_namespace nr ON nr.oid=cr.relnamespace
WHERE EXISTS (SELECT 1 FROM selected s WHERE
(s.class=nc.nspname||'.'||cc.relname AND s.oid=d.objid) OR
(s.class=nr.nspname||'.'||cr.relname AND s.oid=d.refobjid))
ORDER BY d.classid,d.objid,d.objsubid,d.refclassid,d.refobjid,d.refobjsubid,d.deptype LIMIT :limit"""
SHARED_SQL = SELECTED + """SELECT 'pg_shdepend' AS catalog,
nc.nspname||'.'||cc.relname AS dependent_class,d.classid::bigint AS dependent_class_oid,d.objid::bigint,d.objsubid,
nr.nspname||'.'||cr.relname AS referenced_class,d.refobjid::bigint,0 AS refobjsubid,d.deptype::text
FROM pg_catalog.pg_shdepend d JOIN pg_catalog.pg_class cc ON cc.oid=d.classid
JOIN pg_catalog.pg_namespace nc ON nc.oid=cc.relnamespace
JOIN pg_catalog.pg_class cr ON cr.oid=d.refclassid JOIN pg_catalog.pg_namespace nr ON nr.oid=cr.relnamespace
WHERE d.dbid=(SELECT oid FROM pg_catalog.pg_database WHERE datname=current_database())
AND EXISTS (SELECT 1 FROM selected s WHERE s.class=nc.nspname||'.'||cc.relname AND s.oid=d.objid)
ORDER BY d.classid,d.objid,d.objsubid,d.refclassid,d.refobjid,d.deptype LIMIT :limit"""
RESOLVE_SQL = SELECTED + """SELECT s.class,s.oid,s.subid,i.type,i.object_names,i.object_args
FROM selected s CROSS JOIN LATERAL pg_catalog.pg_identify_object_as_address(
CAST(s.class AS regclass),CAST(s.oid AS oid),s.subid) i ORDER BY s.class,s.oid,s.subid LIMIT :limit"""


def attached_query(catalog):
    if catalog not in {"pg_description", "pg_seclabel", "pg_init_privs"}:
        raise ValueError("fixed_native_attached_catalog_only")
    return SELECTED + "SELECT n.nspname||'.'||cl.relname AS class,c.objoid::bigint AS oid,0 AS subid,to_jsonb(c) AS raw," \
        "'{}'::jsonb AS typed_fields,'{}'::jsonb AS typed_vectors " \
        "FROM pg_catalog." + catalog + " c JOIN pg_catalog.pg_class cl ON cl.oid=c.classoid " \
        "JOIN pg_catalog.pg_namespace n ON n.oid=cl.relnamespace WHERE EXISTS (SELECT 1 FROM selected s " \
        "WHERE s.class=n.nspname||'.'||cl.relname AND s.oid=c.objoid) ORDER BY c.classoid,c.objoid,c.objsubid LIMIT :limit"


def collect(connection, role, source_sha256):
    """Main-supplied existing connection only; rollback remains caller-owned."""
    from sqlalchemy import text
    if role not in {"PRODUCTION_BASELINE", "PRIVATE_BASELINE", "PRIVATE_UPGRADED", "INDEPENDENT_REFERENCE"}:
        raise ValueError("explicit_native_graph_observation_role_required")
    if not isinstance(source_sha256, str) or not re.fullmatch("[0-9a-f]{64}", source_sha256) or source_sha256 == "0" * 64:
        raise ValueError("actual_reviewed_native_producer_source_hash_required")
    for setting in ("statement_timeout='750ms'", "lock_timeout='250ms'"):
        connection.exec_driver_sql("SET LOCAL " + setting)
    context = dict(connection.exec_driver_sql("SELECT current_database() AS database,current_user AS role,"
        "current_setting('transaction_read_only') AS read_only,current_setting('transaction_isolation') AS isolation,"
        "current_setting('server_version_num') AS version,pg_current_snapshot()::text AS snapshot,"
        "pg_postmaster_start_time()::text AS postmaster_started,clock_timestamp()::text AS observed_at").mappings().one())
    if context["read_only"] != "on" or context["isolation"] != "repeatable read" or context["version"] != "160015":
        raise ValueError("existing_reviewed_PG16_15_RR_READONLY_required")
    def read(sql, params, cap):
        result = [dict(row) for row in connection.execute(text(sql), dict(params, limit=cap + 1)).mappings()]
        if len(result) > cap:
            raise ValueError("native_graph_count_cap_STOP_no_truncation")
        return result
    unsupported = read(UNSUPPORTED_SQL, {}, MAX_OBJECTS)
    if unsupported:
        raise ValueError("unmodeled_or_secret_bearing_native_object_class_STOP:" + canonical(unsupported).decode())
    layout = classify_layout(read(LAYOUT_SQL, {}, 4096))
    selected = read(SELECTION_SQL, {}, MAX_OBJECTS)
    addresses = {canonical(public_address(row["class"], row["oid"], row["subid"])) for row in selected}
    params = {"addresses": canonical(selected).decode()}
    sections, byte_count = {}, 0
    def keep(catalog, records):
        nonlocal byte_count
        for row in records:
            raw = canonical(row)
            if len(raw) > 2 * 1024**2:
                raise ValueError("native_single_record2MiB_STOP")
            byte_count += len(raw)
            if byte_count > MAX_BYTES:
                raise ValueError("native_graph32MiB_STOP")
            addresses.add(canonical(public_address(row["class"], row["oid"], row["subid"])))
            for ref in row_references(catalog, row["raw"], row["typed_fields"], row["typed_vectors"]):
                if ref["oid"]:
                    addresses.add(canonical(ref))
        sections[catalog] = records
    for catalog in OBJECT_CATALOGS + tuple(AUXILIARY):
        keep(catalog, read(catalog_query(catalog), params, MAX_OBJECTS))
    # Referenced builtin collation/opclass/family facts are meaningful; a name
    # alone cannot conceal different provider/version/operator-family members.
    for catalog in REFERENCE_CATALOGS:
        references = [json.loads(raw) for raw in sorted(addresses)]
        reference_params = dict(addresses=canonical(references).decode())
        keep(catalog, read(catalog_query(catalog), reference_params, MAX_OBJECTS))
        if catalog == "pg_opfamily":
            for child in ("pg_amop", "pg_amproc"):
                keep(child, read(catalog_query(child), reference_params, MAX_OBJECTS))
    for catalog in ("pg_description", "pg_seclabel", "pg_init_privs"):
        keep(catalog, read(attached_query(catalog), params, MAX_OBJECTS))
    edges = read(DEPENDENCIES_SQL, params, MAX_EDGES) + read(SHARED_SQL, params, MAX_EDGES)
    if len(edges) > MAX_EDGES:
        raise ValueError("native_dependency_whole_cap_STOP")
    for edge in edges:
        for prefix in ("dependent", "referenced"):
            oid, subid = (edge["objid"], edge["objsubid"]) if prefix == "dependent" else (edge["refobjid"], edge["refobjsubid"])
            if prefix == "dependent" and edge["dependent_class"] is None:
                if (edge["catalog"] != "pg_depend" or edge["deptype"] != "p"
                        or (edge["dependent_class_oid"], oid, subid) != (0, 0, 0)):
                    raise ValueError("unresolved_native_dependency_class_STOP")
                continue  # Preserve the native pinned sentinel in edges.
            addresses.add(canonical(public_address(edge[prefix + "_class"], oid, subid)))
    if len(addresses) > 100000:
        raise ValueError("native_resolution_count_STOP")
    native = read(RESOLVE_SQL, dict(addresses="[" + ",".join(raw.decode() for raw in sorted(addresses)) + "]"), 100000)
    expected = {(json.loads(raw)["class"], json.loads(raw)["oid"], json.loads(raw)["subid"]) for raw in addresses}
    actual = {(row["class"], row["oid"], row["subid"]) for row in native}
    if actual != expected or len(native) != len(expected):
        raise ValueError("native_reference_resolution_incomplete_STOP")
    result = dict(version=VERSION, role=role, source_sha256=source_sha256, context=context, selected=selected,
        sections=sections, dependencies=edges, native_identities=native, catalog_layout=layout,
        raw_node_tree_fields=RAW_NODE_TREES, status="NATIVE_GRAPH_SOURCE_PENDING_SEMANTIC_ADAPTER_AND_ROLLBACK",
        canonical_approval=False, permission_granted=False, rollback_verified=False)
    raw = canonical(result)
    if len(raw) > MAX_BYTES:
        raise ValueError("complete_native_graph32MiB_STOP_no_partial")
    return result
