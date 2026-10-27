"""Frozen additive aggregate-entry schema generation; initial DDL is untouched."""

import sqlalchemy as sa

from project_mai_tai.atr_simulation.store_schema_20261023 import (
    SIMULATION_TABLES, simulation_metadata as initial_metadata,
)


MODE = "AGG_ENTRY"
MODEL = "RECORDED_AGGREGATE_ENTRY"
CHECKS_BEFORE = {
    "ck_atrsim_mode": "mode IN ('LIVE', 'TODAY', 'VOLBODY', 'FIRSTBAR', 'FIRSTBAR_X3')",
    "ck_atrsim_model": "model IN ('ACTUAL', 'SUPPLIED', 'QUEUE_AWARE', 'OPTIMISTIC', "
        "'MODELED_STOP_AT_OBSERVED_PRINT_VOLUME')",
    "ck_atrsim_mode_model": "(mode = 'LIVE' AND model = 'ACTUAL') OR (mode = 'TODAY' AND model = 'SUPPLIED') OR "
        "(mode = 'VOLBODY' AND model IN ('QUEUE_AWARE', 'OPTIMISTIC')) OR "
        "(mode IN ('FIRSTBAR', 'FIRSTBAR_X3') AND model = 'MODELED_STOP_AT_OBSERVED_PRINT_VOLUME')",
}
CHECKS_AFTER = {
    "ck_atrsim_mode": "(" + CHECKS_BEFORE["ck_atrsim_mode"] + ") OR mode = 'AGG_ENTRY'",
    "ck_atrsim_model": "(" + CHECKS_BEFORE["ck_atrsim_model"] + ") OR model = 'RECORDED_AGGREGATE_ENTRY'",
    "ck_atrsim_mode_model": "(" + CHECKS_BEFORE["ck_atrsim_mode_model"]
        + ") OR (mode = 'AGG_ENTRY' AND model = 'RECORDED_AGGREGATE_ENTRY')",
}


def simulation_metadata():
    metadata = initial_metadata()
    rows = metadata.tables["atr_simulation_rows"]
    checks = {item.name: str(item.sqltext) for item in rows.constraints
              if isinstance(item, sa.CheckConstraint)}
    if checks != CHECKS_BEFORE or set(metadata.tables) != set(SIMULATION_TABLES):
        raise RuntimeError("frozen_initial_simulation_schema_changed")
    for item in tuple(rows.constraints):
        if isinstance(item, sa.CheckConstraint) and item.name in CHECKS_BEFORE:
            rows.constraints.remove(item)
    for name, expression in CHECKS_AFTER.items():
        rows.append_constraint(sa.CheckConstraint(expression, name=name))
    return metadata
