"""Render controlled UI review fixtures, never current production evidence."""

import argparse
from pathlib import Path

from project_mai_tai.services import control_plane as cp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    data = {"services": [], "market_data": {}, "recent_orders": [],
            "account_positions": [], "virtual_positions": []}
    position = {"symbol": "SYNTH", "ticker": "SYNTH", "quantity": "10",
                "remaining_quantity": "8", "entry_price": "3.11",
                "entry_time": "UNKNOWN", "first_execution_time": "UNKNOWN",
                "account_name": "live:orb", "episode_id": "SYNTH-UI-ONLY"}
    live = {"strategy_code": "momentum_warn_live", "source": "strategy_owned_broker_fills",
            "status": "READABLE", "positions": [position], "closed_today": [],
            "daily_pnl": "25.50", "daily_pnl_partial": False}
    owner = {"status": "UNKNOWN"}
    metrics = {"status": "UNKNOWN"}
    bot = cp._momentum_live_dashboard_view(data, live, owner, metrics)
    data["bots"] = [bot]
    warnings = [{"strategy_code": code, "status": "READABLE", "positions": [],
                 "closed_today": [], "recent_decisions": [], "daily_pnl": "987654.32"}
                for code in ("momentum_30s_warn", "momentum_60s_warn")]
    banner = ('<div role="alert" style="padding:12px;background:#7a3200;color:white">'
              'CONTROLLED SYNTH UI FIXTURE ONLY. Not production positions, P&amp;L or health.</div>')
    pages = {"dashboard.html": cp._render_compact_dashboard(data),
             "momentum.html": cp._render_momentum_page(data, live=live, warnings=warnings,
                                                       owner=owner, metrics=metrics)}
    assert "987654" not in pages["dashboard.html"]
    assert 'data-strategy="momentum_warn_live"' in pages["dashboard.html"]
    assert "Warning method 30 s" in pages["momentum.html"]
    assert "Warning method 60 s" in pages["momentum.html"]
    for name, html in pages.items():
        with (args.output_dir / name).open("x") as handle:
            handle.write(html.replace("<body>", "<body>" + banner, 1))
    print(args.output_dir)


if __name__ == "__main__":
    main()
