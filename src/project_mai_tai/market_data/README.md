# Market Data Package

This package owns market-data ingestion and publication for the new runtime.

Files:

- `gateway.py`
  - orchestrates snapshot polling, streaming, warmup, and subscription updates
- `massive_provider.py`
  - Massive/Polygon-specific REST and WebSocket integration
- `models.py`
  - normalized record types for snapshots, trades, quotes, and historical bars
- `protocols.py`
  - provider interfaces used by the gateway
- `publisher.py`
  - Redis stream publishing for snapshot batches, trades, quotes, warmup bars, and heartbeats
- `reference_cache.py`
  - cached reference universe used for snapshot scanning and enrichment

Responsibility boundary:

- this package fetches and normalizes market data
- it does not decide whether a ticker should be traded
- it does not call brokers

The orchestration layer that runs this package in production is:

- `../services/market_data_gateway.py`
