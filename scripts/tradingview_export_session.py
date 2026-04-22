from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil
import tempfile

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_URL = "https://www.tradingview.com/chart/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export TradingView session state from a local browser profile.")
    parser.add_argument("--user-data-dir", help="Path to the signed-in Chrome user data directory.")
    parser.add_argument(
        "--cdp-url",
        help="Optional existing Chrome remote-debugging endpoint, e.g. http://127.0.0.1:51299",
    )
    parser.add_argument("--output", required=True, help="Path to write the exported session JSON.")
    parser.add_argument("--browser-channel", default="chrome", help="Playwright browser channel, default: chrome.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"TradingView URL to inspect, default: {DEFAULT_URL}")
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Launch headless. Leave off for the safest export from a known-good interactive profile.",
    )
    return parser


def export_session(*, user_data_dir: Path, output_path: Path, browser_channel: str, url: str, headless: bool) -> None:
    if not user_data_dir:
        raise ValueError("user_data_dir is required when exporting from a copied profile")
    temp_profile_root = Path(tempfile.mkdtemp(prefix="tv-session-export-"))
    temp_profile = temp_profile_root / "user_data"
    shutil.copytree(user_data_dir, temp_profile)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(temp_profile),
                channel=browser_channel,
                headless=headless,
            )
            try:
                page = _get_or_create_page(context, url=url)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                except PlaywrightTimeoutError:
                    page.goto(url, wait_until="load", timeout=60_000)
                page.wait_for_timeout(3_000)
                origin_url = _origin_url(page.url, fallback=url)
                cookies = context.cookies([origin_url])
                local_storage = page.evaluate("() => Object.fromEntries(Object.entries(window.localStorage))")
                session_storage = page.evaluate("() => Object.fromEntries(Object.entries(window.sessionStorage))")
                payload = {
                    "exported_at": datetime.now(UTC).isoformat(),
                    "source_url": page.url,
                    "origin_url": origin_url,
                    "user_agent": page.evaluate("() => navigator.userAgent"),
                    "cookies": cookies,
                    "local_storage": local_storage,
                    "session_storage": session_storage,
                }
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            finally:
                context.close()
    finally:
        shutil.rmtree(temp_profile_root, ignore_errors=True)


def export_session_via_cdp(*, cdp_url: str, output_path: Path, url: str) -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_url)
        try:
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = _get_or_create_page(context, url=url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                page.goto(url, wait_until="load", timeout=60_000)
            page.wait_for_timeout(3_000)
            origin_url = _origin_url(page.url, fallback=url)
            cookies = context.cookies([origin_url])
            local_storage = page.evaluate("() => Object.fromEntries(Object.entries(window.localStorage))")
            session_storage = page.evaluate("() => Object.fromEntries(Object.entries(window.sessionStorage))")
            payload = {
                "exported_at": datetime.now(UTC).isoformat(),
                "source_url": page.url,
                "origin_url": origin_url,
                "user_agent": page.evaluate("() => navigator.userAgent"),
                "cookies": cookies,
                "local_storage": local_storage,
                "session_storage": session_storage,
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        finally:
            browser.close()


def _get_or_create_page(context, *, url: str):
    for page in context.pages:
        if "tradingview.com" in page.url:
            return page
    page = context.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    return page


def _origin_url(page_url: str, *, fallback: str) -> str:
    candidate = page_url if page_url.startswith("http") else fallback
    if candidate.startswith("https://www.tradingview.com"):
        return "https://www.tradingview.com"
    return "https://www.tradingview.com"


def main() -> int:
    args = build_parser().parse_args()
    if args.cdp_url:
        export_session_via_cdp(
            cdp_url=args.cdp_url,
            output_path=Path(args.output),
            url=args.url,
        )
        return 0
    if not args.user_data_dir:
        raise SystemExit("--user-data-dir is required unless --cdp-url is provided")
    export_session(
        user_data_dir=Path(args.user_data_dir),
        output_path=Path(args.output),
        browser_channel=args.browser_channel,
        url=args.url,
        headless=bool(args.headless),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
