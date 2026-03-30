# Docs Index

This directory contains the durable project-level documentation.

Use these files by question:

- `architecture.md`
  - what each service owns and how persistence/events are split
- `active-market-verification-todo.md`
  - what still needs to be proven during a real session
- `live-market-restart-runbook.md`
  - how to restart safely during live operations
- `github-actions-deploy.md`
  - how validation plus VPS deploy is wired through GitHub Actions
- `deployment-operating-model.md`
  - current deploy design, agent vs user actions, and the risk-based operating model
- `vps-deployment.md`
  - how the runtime is deployed on the VPS
- `schwab-onboarding.md`
  - how Schwab auth and token-store setup works
- `strategy-preservation.md`
  - what legacy behavior is intentionally being preserved
- `implementation-roadmap.md`
  - planned work and sequencing
- `session-handoff-2026-03-29.md`
  - high-signal operator and build-session handoff context
- `morning-runtime-flow.mmd`
  - editable runtime flow diagram source
- `morning-runtime-flow.pdf`
  - exported diagram for quick viewing

If you change runtime ownership, deployment flow, or live-operation expectations, update the relevant doc here and the nearest folder-level README in the code tree.
