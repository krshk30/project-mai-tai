# Standalone Gateway Restart Withdrawn

Superseded on 2026-10-03 by the operator's Sunday resize/reboot decision.
The October 3 standalone job was never installed: service and timer both read
LoadState=not-found, ActiveState=inactive, MainPID=0. No merge, env change or
gateway restart occurred under that draft.

Use [RESIZE_PLAN_2026-10-04.md](RESIZE_PLAN_2026-10-04.md) and its exact reviewed
release instead. No authorization from the withdrawn draft carries forward.
