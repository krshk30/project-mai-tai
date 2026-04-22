# Service Wrappers

This directory contains thin executable wrappers, not the real service logic.

Each service subdirectory exists so operators and developers can run a familiar `python services/<name>/main.py` command, while the actual implementation stays in the installable package under `src/project_mai_tai/`.

Mapping:

- `control-plane/`
  - wrapper for `project_mai_tai.services.control_plane`
- `market-data-gateway/`
  - wrapper for `project_mai_tai.services.market_data_gateway`
- `strategy-engine/`
  - wrapper for `project_mai_tai.services.strategy_engine`
- `tradingview-alerts/`
  - wrapper for `project_mai_tai.services.tradingview_alerts`
- `oms-risk/`
  - wrapper for `project_mai_tai.services.oms_risk`
- `reconciler/`
  - wrapper for `project_mai_tai.services.reconciler`

Use this directory when you want:

- a simple script-style local launch command
- an easy place for service-specific notes

Use `src/project_mai_tai/` when you want:

- the actual runtime code
- service ownership boundaries
- broker, strategy, DB, and event contracts

Start with:

- [Source Layout](../src/README.md)
- [Package Layout](../src/project_mai_tai/README.md)
