# Reconciler

Compares persisted execution truth to account truth and records drift.

Responsibilities:

- quantity-drift detection
- average-price drift detection
- stuck-order detection
- stuck-intent detection
- reconciliation finding and incident creation

Implementation:

- wrapper: `services/reconciler/main.py`
- package code: `src/project_mai_tai/reconciliation/service.py`

This service identifies problems; it should not bypass OMS to repair state directly.
