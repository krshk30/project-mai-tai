"""Validated backtest engine (in-repo, CI-gated golden-case suite).

Mirrors live strategy LOGIC exactly (imports the live pure leaves) and models FILLS
honestly (~3s latency, full spread). See docs/backtest-engine-design.md.
"""
