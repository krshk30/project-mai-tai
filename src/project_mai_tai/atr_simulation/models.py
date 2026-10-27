"""SIM-only current metadata; initial DDL is frozen in its versioned snapshot."""

from .store_schema_20261023_aggregate import SIMULATION_TABLES, simulation_metadata


metadata = simulation_metadata()
inputs = metadata.tables["atr_simulation_inputs"]
consumers = metadata.tables["atr_simulation_consumers"]
runs = metadata.tables["atr_simulation_runs"]
rows = metadata.tables["atr_simulation_rows"]
