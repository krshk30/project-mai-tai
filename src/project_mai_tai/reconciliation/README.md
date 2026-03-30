# Reconciliation Package

This package checks whether Mai Tai's persisted execution model still matches account truth.

Files:

- `service.py`
  - periodic reconciliation runner and finding/incident creation

Current finding categories include:

- position quantity mismatch
- average-price mismatch
- stuck orders
- stuck intents

Responsibility boundary:

- reconciliation identifies problems and records them durably
- it does not bypass OMS to mutate positions or orders directly

If you are debugging operator-visible drift between broker/account state and strategy attribution, start here after checking `oms/`.
