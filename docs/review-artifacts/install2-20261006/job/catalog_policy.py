"""Catalog populations are identities, not a guessed denominator."""
import json
from release_policy import BINDING, NEW_ENV, RETRY_ENABLED, canonical, need

RETRY_NAME = "strategy_schwab_1m_v2_retry_one_max_retries"


def identities(rows):
    result = []
    need(isinstance(rows, list) and bool(rows), "empty catalog")
    for row in rows:
        owners = [row["owning_service"], *row.get("also_check_services", [])]
        need(isinstance(row["name"], str) and owners and all(isinstance(x, str) for x in owners),
             "catalog identity malformed")
        result.extend((row["name"], owner) for owner in owners)
    need(len(result) == len(set(result)), "catalog identities duplicated")
    return result


def numeric_candidate(raw):
    data = json.loads(raw)
    need(data["schema_version"] == 1, "numeric schema unsupported")
    rows = data["settings"]
    identities(rows)
    found = [row for row in rows if row["name"] == RETRY_NAME]
    need(len(found) <= 1, "retry numeric row duplicated")
    if found:
        entry = found[0]
        need(entry["owning_service"] == "schwab-1m-v2"
             and entry.get("also_check_services") == ["oms"], "retry numeric owner drift")
        entry.update(expected=0, require_process_env=True)
    else:
        rows.append(dict(name=RETRY_NAME, expected=0, owning_service="schwab-1m-v2",
                         also_check_services=["oms"], require_process_env=True,
                         reason="Retain operator retry enabled=true / budget=0; explicit process proof.",
                         ruling="Operator Install2 2026-10-06; no retry policy change."))
    return canonical(data)


def validate_catalogs(flags_raw, numeric_raw, binding=None):
    binding = BINDING if binding is None else binding
    flags, numeric = json.loads(flags_raw)["flags"], json.loads(numeric_raw)["settings"]
    boolean_ids, numeric_ids = identities(flags), identities(numeric)
    need(not set(boolean_ids) & set(numeric_ids), "boolean/numeric identities overlap")
    required = {key.removeprefix("MAI_TAI_").lower() for key in NEW_ENV}
    required.update({RETRY_ENABLED.removeprefix("MAI_TAI_").lower(), binding["t43_catalog_name"]})
    for name in required:
        rows = [row for row in flags if row["name"] == name]
        need(len(rows) == 1 and rows[0]["expected"] is True, "required catalog not ON: " + name)
    # Keep the actual reviewed catalog owner population, not invented extra checks.
    # The attended proc proof separately reads all four explicit new keys on v2/OMS.
    retry = [row for row in numeric if row["name"] == RETRY_NAME]
    need(len(retry) == 1 and type(retry[0]["expected"]) is int and retry[0]["expected"] == 0
         and retry[0].get("require_process_env") is True
         and {owner for flag, owner in numeric_ids if flag == RETRY_NAME} == {"oms", "schwab-1m-v2"},
         "explicit zero numeric catalog missing/drift")
    return dict(boolean=len(boolean_ids), numeric=len(numeric_ids),
                total=len(boolean_ids) + len(numeric_ids))
