# OMS and Risk

Authoritative execution service for Mai Tai.

Responsibilities:

- consume strategy intents
- apply risk checks
- submit or cancel broker orders
- persist orders, events, and fills
- derive virtual and account positions
- sync broker positions back into Postgres

Implementation:

- wrapper: `services/oms-risk/main.py`
- package code: `src/project_mai_tai/oms/` and `src/project_mai_tai/broker_adapters/`

This is the only service that should talk to broker trading APIs.
