# Market-Data Gateway

Single owner of market-data ingestion for the new platform.

Responsibilities:

- Massive/Polygon snapshot polling
- trade and quote subscriptions
- reference-data cache usage
- historical warmup publishing
- dynamic symbol subscription updates
- Redis publication of normalized market-data events

Implementation:

- wrapper: `services/market-data-gateway/main.py`
- package code: `src/project_mai_tai/market_data/` and `src/project_mai_tai/services/market_data_gateway.py`

This service should fetch and normalize data, not make trading decisions.
