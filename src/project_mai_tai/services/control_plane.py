from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from project_mai_tai.events import stream_name
from project_mai_tai.log import configure_logging
from project_mai_tai.settings import Settings, get_settings


SERVICE_NAME = "control-plane"


def build_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    app = FastAPI(title="Project Mai Tai Control Plane", version="0.1.0")

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {
            "status": "bootstrap",
            "service": SERVICE_NAME,
            "environment": active_settings.environment,
            "control_plane_url": active_settings.control_plane_base_url,
            "database_configured": bool(active_settings.database_url),
            "redis_configured": bool(active_settings.redis_url),
        }

    @app.get("/meta")
    async def meta() -> dict[str, object]:
        return {
            "app_name": active_settings.app_name,
            "domain": "project-mai-tai.live",
            "legacy_api_base_url": active_settings.legacy_api_base_url,
            "streams": {
                "market_data": stream_name(active_settings.redis_stream_prefix, "market-data"),
                "snapshot_batches": stream_name(active_settings.redis_stream_prefix, "snapshot-batches"),
                "market_data_subscriptions": stream_name(
                    active_settings.redis_stream_prefix,
                    "market-data-subscriptions",
                ),
                "strategy_intents": stream_name(active_settings.redis_stream_prefix, "strategy-intents"),
                "order_events": stream_name(active_settings.redis_stream_prefix, "order-events"),
                "heartbeats": stream_name(active_settings.redis_stream_prefix, "heartbeats"),
            },
        }

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return f"""
        <html>
          <head>
            <title>Project Mai Tai</title>
            <style>
              body {{
                background: linear-gradient(180deg, #f5efe3 0%, #f1f7fb 100%);
                color: #173042;
                font-family: Georgia, "Times New Roman", serif;
                margin: 0;
                padding: 2rem;
              }}
              .shell {{
                margin: 0 auto;
                max-width: 1080px;
              }}
              .hero {{
                background: rgba(255, 255, 255, 0.88);
                border: 1px solid rgba(23, 48, 66, 0.15);
                border-radius: 24px;
                padding: 2rem;
                box-shadow: 0 16px 40px rgba(23, 48, 66, 0.08);
              }}
              .grid {{
                display: grid;
                gap: 1rem;
                grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
                margin-top: 1.25rem;
              }}
              .card {{
                background: rgba(255, 255, 255, 0.9);
                border: 1px solid rgba(23, 48, 66, 0.12);
                border-radius: 18px;
                padding: 1rem;
              }}
              h1, h2 {{
                margin: 0 0 0.5rem 0;
              }}
              code {{
                background: rgba(23, 48, 66, 0.08);
                border-radius: 6px;
                padding: 0.15rem 0.35rem;
              }}
            </style>
          </head>
          <body>
            <div class="shell">
              <section class="hero">
                <h1>Project Mai Tai</h1>
                <p>
                  Production-oriented live-trading platform scaffold running in parallel with the
                  legacy system.
                </p>
                <div class="grid">
                  <div class="card">
                    <h2>Environment</h2>
                    <p><code>{active_settings.environment}</code></p>
                  </div>
                  <div class="card">
                    <h2>Control Plane</h2>
                    <p><code>{active_settings.control_plane_base_url}</code></p>
                  </div>
                  <div class="card">
                    <h2>Redis Prefix</h2>
                    <p><code>{active_settings.redis_stream_prefix}</code></p>
                  </div>
                  <div class="card">
                    <h2>Broker Path</h2>
                    <p><code>{active_settings.broker_default_provider}</code> first, Schwab next</p>
                  </div>
                </div>
              </section>
            </div>
          </body>
        </html>
        """

    return app


app = build_app()


def run() -> None:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    uvicorn.run(
        "project_mai_tai.services.control_plane:app",
        host=settings.control_plane_host,
        port=settings.control_plane_port,
        reload=False,
    )
