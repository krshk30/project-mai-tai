# Shadow Package

This package contains optional comparison helpers for observing the legacy app beside Mai Tai.

Files:

- `legacy_client.py`
  - HTTP client that fetches scanner and bot snapshots from the legacy runtime and normalizes them for the control plane

Responsibility boundary:

- shadow comparison is for visibility and parity checking
- it must not drive broker execution or mutate Mai Tai runtime state

The main consumer is:

- `../services/control_plane.py`
