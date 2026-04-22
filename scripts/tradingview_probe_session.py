from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import shutil

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DEFAULT_URL = "https://www.tradingview.com/chart/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inject a TradingView session into a fresh profile and probe login state.")
    parser.add_argument("--session-file", required=True, help="Path to exported TradingView session JSON.")
    parser.add_argument("--user-data-dir", required=True, help="Fresh Linux/Windows-native browser profile directory.")
    parser.add_argument("--output", required=True, help="Path to write probe results JSON.")
    parser.add_argument("--browser-channel", default="", help="Optional Playwright browser channel, e.g. chrome.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"TradingView URL to probe, default: {DEFAULT_URL}")
    parser.add_argument("--headless", action="store_true", help="Run headless for unattended probes.")
    return parser


def probe_session(
    *,
    session_file: Path,
    user_data_dir: Path,
    output_path: Path,
    browser_channel: str,
    url: str,
    headless: bool,
) -> None:
    session = json.loads(session_file.read_text(encoding="utf-8"))
    if user_data_dir.exists():
        shutil.rmtree(user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        launch_options: dict[str, object] = {
            "user_data_dir": str(user_data_dir),
            "headless": headless,
            "user_agent": session.get("user_agent") or None,
        }
        if browser_channel.strip():
            launch_options["channel"] = browser_channel.strip()
        context = playwright.chromium.launch_persistent_context(**launch_options)
        try:
            page = context.new_page()
            cookies = [_normalize_cookie(cookie) for cookie in session.get("cookies", [])]
            if cookies:
                context.add_cookies(cookies)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                page.goto(url, wait_until="load", timeout=60_000)

            if session.get("local_storage"):
                page.evaluate(
                    """(items) => {
                        for (const [key, value] of Object.entries(items)) {
                            window.localStorage.setItem(key, value);
                        }
                    }""",
                    session["local_storage"],
                )
            if session.get("session_storage"):
                page.evaluate(
                    """(items) => {
                        for (const [key, value] of Object.entries(items)) {
                            window.sessionStorage.setItem(key, value);
                        }
                    }""",
                    session["session_storage"],
                )

            page.reload(wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3_000)
            result = _build_probe_result(page)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        finally:
            context.close()


def _normalize_cookie(cookie: dict[str, object]) -> dict[str, object]:
    allowed_keys = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
    }
    normalized = {key: value for key, value in cookie.items() if key in allowed_keys}
    same_site = normalized.get("sameSite")
    if same_site not in {"Strict", "Lax", "None"}:
        normalized.pop("sameSite", None)
    return normalized


def _build_probe_result(page) -> dict[str, object]:
    content = page.content()
    is_logged_in = page.evaluate(
        """() => {
            const signIn = document.querySelector('[data-name="header-user-menu-sign-in"]');
            const userMenu = document.querySelector('[data-name="header-user-menu-button"]');
            const authClass = document.documentElement.classList.contains('is-authenticated');
            return Boolean((userMenu || authClass) && !signIn);
        }"""
    )
    return {
        "probed_at": datetime.now(UTC).isoformat(),
        "page_url": page.url,
        "title": page.title(),
        "logged_in": bool(is_logged_in),
        "login_redirected": "/accounts/signin/" in page.url,
        "rate_limited": '"code": "rate_limit"' in content,
        "has_user_menu": '[data-name="header-user-menu-button"]' in content,
        "has_sign_in_cta": '[data-name="header-user-menu-sign-in"]' in content,
    }


def main() -> int:
    args = build_parser().parse_args()
    probe_session(
        session_file=Path(args.session_file),
        user_data_dir=Path(args.user_data_dir),
        output_path=Path(args.output),
        browser_channel=args.browser_channel,
        url=args.url,
        headless=bool(args.headless),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
