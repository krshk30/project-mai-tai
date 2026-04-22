from __future__ import annotations

import importlib
import json
import logging
import os
from pathlib import Path
import asyncio
import shutil
import socket
import subprocess
import time
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from project_mai_tai.settings import Settings


logger = logging.getLogger(__name__)


def render_alert_name(symbol: str, *, prefix: str) -> str:
    normalized = str(symbol).strip().upper()
    trimmed_prefix = str(prefix).strip()
    if not trimmed_prefix:
        return normalized
    return f"{trimmed_prefix}:{normalized}"


def build_chart_url(chart_url: str, symbol: str) -> str:
    parsed = urlparse(chart_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["symbol"] = str(symbol).strip().upper()
    return urlunparse(parsed._replace(query=urlencode(query)))


def render_alert_message(settings: Settings, symbol: str) -> str:
    template = settings.tradingview_alerts_message_template_json.strip()
    if not template:
        return ""
    return (
        template.replace("{{SYMBOL}}", str(symbol).strip().upper())
        .replace("{{WEBHOOK_URL}}", settings.tradingview_alerts_webhook_url or "")
        .replace("{{WEBHOOK_TOKEN}}", settings.tradingview_alerts_webhook_token or "")
    )


class PlaywrightTradingViewAlertOperator:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._playwright_module: Any | None = None
        self._playwright: Any | None = None
        self._browser_context: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None
        self._lock: Any | None = None
        self._last_error: str | None = None
        self._last_url: str | None = None
        self._auth_required = False
        self._auth_reason: str | None = None
        self._chrome_process: subprocess.Popen[str] | None = None
        self._owns_browser_session = True

    async def add_alert(self, symbol: str) -> None:
        async with self._get_lock():
            page = await self._ensure_page_for_symbol(symbol)
            await self._assert_authenticated(page)
            alert_name = render_alert_name(symbol, prefix=self.settings.tradingview_alerts_alert_name_prefix)
            message = render_alert_message(self.settings, symbol)
            try:
                await self._open_create_alert_dialog(page)
                await self._fill_optional_condition(page)
                await self._fill_optional_message(page, alert_name=alert_name, message=message)
                await self._fill_optional_webhook(page)
                await self._submit_create_dialog(page)
                self._clear_auth_required()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                await self._capture_diagnostics(page, f"add_{symbol}")
                raise

    async def remove_alert(self, symbol: str) -> None:
        async with self._get_lock():
            page = await self._ensure_page_for_symbol(symbol)
            await self._assert_authenticated(page)
            alert_name = render_alert_name(symbol, prefix=self.settings.tradingview_alerts_alert_name_prefix)
            try:
                await self._open_alerts_manager(page)
                await self._search_alerts(page, alert_name)
                deleted = await self._delete_matching_alert(page, alert_name)
                await self._search_alerts(page, alert_name)
                if await self._alert_exists(page, alert_name):
                    raise RuntimeError(f"TradingView alert still present after remove attempt: {alert_name}")
                if not deleted:
                    logger.info(
                        "TradingView alert already absent | symbol=%s alert_name=%s",
                        symbol,
                        alert_name,
                    )
                self._clear_auth_required()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
                await self._capture_diagnostics(page, f"remove_{symbol}")
                raise

    async def status(self) -> dict[str, object]:
        import_available = self._playwright_import_available()
        return {
            "operator": "playwright",
            "ready": import_available and self._browser_context is not None,
            "import_available": import_available,
            "last_error": self._last_error,
            "last_url": self._last_url,
            "auth_required": self._auth_required,
            "auth_reason": self._auth_reason,
            "user_data_dir": self.settings.tradingview_alerts_user_data_dir,
            "headless": self.settings.tradingview_alerts_headless,
            "browser_channel": self.settings.tradingview_alerts_browser_channel,
        }

    async def close(self) -> None:
        if self._page is not None and self._owns_browser_session:
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None
        else:
            self._page = None
        if self._browser_context is not None and self._owns_browser_session:
            try:
                await self._browser_context.close()
            except Exception:
                pass
            self._browser_context = None
        else:
            self._browser_context = None
        if self._browser is not None and self._owns_browser_session:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        else:
            self._browser = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._chrome_process is not None:
            try:
                self._chrome_process.terminate()
                self._chrome_process.wait(timeout=5)
            except Exception:
                try:
                    self._chrome_process.kill()
                except Exception:
                    pass
            self._chrome_process = None

    def _playwright_import_available(self) -> bool:
        return importlib.util.find_spec("playwright.async_api") is not None

    async def _ensure_page_for_symbol(self, symbol: str):
        page = await self._ensure_page()
        url = build_chart_url(self.settings.tradingview_alerts_chart_url, symbol)
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await self._wait_for_chart_studies(page)
        self._last_url = page.url
        await self._detect_auth_gate(page)
        return page

    async def _ensure_page(self):
        if self._page is not None:
            return self._page

        try:
            async_api = importlib.import_module("playwright.async_api")
        except ModuleNotFoundError as exc:
            self._last_error = "playwright is not installed"
            raise RuntimeError(self._last_error) from exc

        self._playwright_module = async_api
        if self._playwright is None:
            self._playwright = await async_api.async_playwright().start()

        launch_options = {
            "user_data_dir": str(Path(self.settings.tradingview_alerts_user_data_dir)),
            "headless": self.settings.tradingview_alerts_headless,
        }
        if self.settings.tradingview_alerts_browser_channel.strip():
            launch_options["channel"] = self.settings.tradingview_alerts_browser_channel.strip()
        try:
            self._browser_context = await self._playwright.chromium.launch_persistent_context(**launch_options)
        except Exception as exc:
            logger.warning("persistent TradingView browser launch failed, attempting CDP fallback: %s", exc)
            self._browser_context = await self._connect_via_cdp_fallback()
        self._page = await self._select_chart_page(self._browser_context.pages)
        if self._page is None:
            self._page = await self._browser_context.new_page()
        self._page.set_default_timeout(self.settings.tradingview_alerts_timeout_ms)
        await self._page.goto(self.settings.tradingview_alerts_chart_url, wait_until="domcontentloaded")
        await self._wait_for_chart_studies(self._page)
        self._last_url = self._page.url
        await self._detect_auth_gate(self._page)
        return self._page

    async def _connect_via_cdp_fallback(self):
        existing_port = self._find_existing_debug_port()
        if existing_port is not None:
            existing_context = await self._connect_to_cdp_port(existing_port)
            if existing_context is not None:
                self._owns_browser_session = False
                return existing_context
        chrome_executable = self._resolve_chrome_executable()
        if not chrome_executable:
            raise RuntimeError("could not resolve local Chrome executable for TradingView automation fallback")
        debug_port = self._reserve_debug_port()
        user_data_dir = str(Path(self.settings.tradingview_alerts_user_data_dir))
        self._chrome_process = subprocess.Popen(
            [
                chrome_executable,
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--new-window",
                self.settings.tradingview_alerts_chart_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self._owns_browser_session = True
        connected_context = await self._connect_to_cdp_port(debug_port)
        if connected_context is not None:
            return connected_context
        raise RuntimeError(f"could not connect to Chrome via CDP fallback on port {debug_port}")

    async def _connect_to_cdp_port(self, debug_port: int) -> Any | None:
        endpoint = f"http://127.0.0.1:{debug_port}"
        deadline = time.time() + 15
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                self._browser = await self._playwright.chromium.connect_over_cdp(endpoint)
                if self._browser.contexts:
                    return self._browser.contexts[0]
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.25)
        logger.warning("failed to connect to Chrome CDP endpoint %s: %s", endpoint, last_error)
        return None

    def _resolve_chrome_executable(self) -> str | None:
        configured_channel = self.settings.tradingview_alerts_browser_channel.strip().lower()
        candidates = []
        if configured_channel == "chrome":
            candidates.extend(
                [
                    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                ]
            )
        candidates.extend(
            [
                shutil.which("chrome") or "",
                shutil.which("chrome.exe") or "",
            ]
        )
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        return None

    def _reserve_debug_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _find_existing_debug_port(self) -> int | None:
        user_data_dir = str(Path(self.settings.tradingview_alerts_user_data_dir))
        powershell = """
$target = [System.IO.Path]::GetFullPath($env:TV_USER_DATA_DIR)
Get-CimInstance Win32_Process |
Where-Object {
    $_.Name -eq 'chrome.exe' -and
    $_.CommandLine -match '--remote-debugging-port=(\\d+)' -and
    $_.CommandLine -like "*$target*"
} |
Select-Object -ExpandProperty CommandLine
"""
        try:
            env = dict(os.environ)
            env["TV_USER_DATA_DIR"] = user_data_dir
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command", powershell],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                env=env,
            )
        except Exception:
            return None
        for line in completed.stdout.splitlines():
            match = re.search(r"--remote-debugging-port=(\d+)", line)
            if match:
                return int(match.group(1))
        return None

    async def _select_chart_page(self, pages: list[Any]) -> Any | None:
        visible_chart_page = None
        chart_page = None
        for page in reversed(pages):
            try:
                if "tradingview.com/chart/" not in page.url:
                    continue
                chart_page = chart_page or page
                visibility_state = await page.evaluate("document.visibilityState")
                if visibility_state == "visible":
                    visible_chart_page = page
                    break
            except Exception:
                continue
        if visible_chart_page is not None:
            return visible_chart_page
        if chart_page is not None:
            return chart_page
        return pages[0] if pages else None

    async def _open_create_alert_dialog(self, page) -> None:
        await self._dismiss_join_for_free_prompt(page)
        try:
            await page.keyboard.press("Alt+A")
            await page.wait_for_timeout(800)
            await self._dismiss_join_for_free_prompt(page)
            if await self._is_create_alert_dialog_open(page):
                return
        except Exception:
            pass

        await self._open_alerts_manager(page)
        await self._click_first(
            page,
            [
                'text="Create alert"',
                'button:has-text("Create alert")',
                'button:has-text("Create Alert")',
                '[role="button"]:has-text("Create alert")',
                '[data-name="alerts-create-button"]',
                '[data-name="header-toolbar-alerts-button"]',
                'button[aria-label*="Alert"]',
                'button:has-text("Alert")',
            ],
            description="create alert button",
            force=True,
        )
        await page.wait_for_timeout(1000)
        await self._dismiss_join_for_free_prompt(page)
        await self._click_toolbar_alert_if_needed(page)
        if await self._is_create_alert_dialog_open(page):
            return
        if await self._join_for_free_prompt_visible(page):
            raise RuntimeError(
                "TradingView blocked alert creation with a 'Join for free' prompt; sign in once in the automation profile"
            )
        raise RuntimeError("could not open TradingView create alert dialog")

    async def _click_toolbar_alert_if_needed(self, page) -> None:
        if await self._is_create_alert_dialog_open(page):
            return
        toolbar_alert = page.locator('text="Alert"').first
        try:
            if await toolbar_alert.count():
                await toolbar_alert.evaluate("(el) => el.click()")
                await page.wait_for_timeout(1200)
        except Exception:
            return

    async def _open_alerts_manager(self, page) -> None:
        await self._click_first(
            page,
            [
                '[data-name="alerts"]',
                'button[aria-label="Alerts"]',
                '[data-name="alerts-manager-button"]',
                'button[aria-label*="Alerts manager"]',
                'button:has-text("Alerts")',
            ],
            description="alerts manager button",
            force=True,
        )
        await page.wait_for_timeout(1000)

    async def _fill_optional_condition(self, page) -> None:
        condition_text = self.settings.tradingview_alerts_condition_text.strip()
        if not condition_text:
            return
        if condition_text == "Any alert() function call":
            await self._select_study_condition(page, study_prefix="Multi-Path Momentum Scalp v1.0", operator_text=condition_text)
            return
        await self._click_first_if_present(
            page,
            [
                '[data-name="condition"]',
                'button:has-text("Condition")',
            ],
            use_dom_click=True,
        )
        await self._click_first_if_present(
            page,
            [
                f'text="{condition_text}"',
            ],
            use_dom_click=True,
        )

    async def _fill_optional_webhook(self, page) -> None:
        webhook_url = (self.settings.tradingview_alerts_webhook_url or "").strip()
        if not webhook_url:
            return
        notifications_button = page.locator('[data-qa-id="alert-notifications-button"]').first
        if await notifications_button.count():
            await notifications_button.evaluate("(el) => el.click()")
            await page.wait_for_timeout(700)
        webhook_checkbox = page.locator('[data-qa-id="webhook"] input[type="checkbox"]').first
        if await webhook_checkbox.count():
            if not await webhook_checkbox.is_checked():
                await page.locator('[data-qa-id="webhook"]').first.evaluate("(el) => el.click()")
                await page.wait_for_timeout(300)
        webhook_input = page.locator('[data-qa-id="ui-lib-Input-input webhook-input-input"]').first
        if await webhook_input.count() == 0:
            raise RuntimeError("could not find TradingView webhook URL field")
        await webhook_input.fill("")
        await webhook_input.fill(webhook_url)
        await self._click_visible_submit(page, expected_label="Apply")

    async def _is_create_alert_dialog_open(self, page) -> bool:
        if await self._join_for_free_prompt_visible(page):
            return False
        selectors = [
            '[data-qa-id="alerts-create-edit-dialog"]',
            '[role="dialog"]',
            'text="Webhook URL"',
            'text="Notifications"',
            'button:has-text("Create")',
            'text="Create alert on"',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                if await locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _assert_authenticated(self, page) -> None:
        await self._detect_auth_gate(page)
        if self._auth_required:
            raise RuntimeError(self._auth_reason or "TradingView automation profile needs a fresh login")

    async def _detect_auth_gate(self, page) -> None:
        try:
            content = await page.content()
        except Exception:
            return
        current_url = page.url
        self._last_url = current_url
        if "/accounts/signin/" in current_url and '"code": "rate_limit"' in content:
            self._set_auth_required(
                "TradingView sign-in endpoint is rate-limited right now; wait and retry the saved browser profile later"
            )
            return
        try:
            if await page.locator("html.is-not-authenticated").count():
                self._set_auth_required(
                    "TradingView automation profile is not signed in; open the persistent browser profile and sign in once"
                )
                return
        except Exception:
            pass
        if await self._join_for_free_prompt_visible(page):
            self._set_auth_required(
                "TradingView automation profile is not signed in; TradingView is showing a Join for free prompt"
            )
            return
        self._clear_auth_required()

    def _set_auth_required(self, reason: str) -> None:
        self._auth_required = True
        self._auth_reason = reason
        self._last_error = reason

    def _clear_auth_required(self) -> None:
        self._auth_required = False
        self._auth_reason = None

    async def _join_for_free_prompt_visible(self, page) -> bool:
        selectors = [
            'text="Join for free"',
            'text="Never miss a trade again"',
        ]
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                if await locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    async def _dismiss_join_for_free_prompt(self, page) -> bool:
        if not await self._join_for_free_prompt_visible(page):
            return False
        for selector in [
            'button[aria-label="Close"]',
            '[data-name="close"]',
            '[class*="close"]',
            'text="×"',
        ]:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                await locator.click(force=True)
                await page.wait_for_timeout(500)
                if not await self._join_for_free_prompt_visible(page):
                    return True
            except Exception:
                continue
        return False

    async def _fill_optional_message(self, page, *, alert_name: str, message: str) -> None:
        if not alert_name and not message:
            return
        message_button = page.locator('[data-qa-id="alert-message-button"]').first
        if await message_button.count():
            await message_button.evaluate("(el) => el.click()")
            await page.wait_for_timeout(700)
        alert_name_input = page.locator('[data-qa-id="ui-lib-Input-input alert-name-input"]').first
        if alert_name and await alert_name_input.count():
            await alert_name_input.fill("")
            await alert_name_input.fill(alert_name)
        message_input = page.locator('[data-qa-id="ui-lib-textarea-middle-slot"]').first
        if message and await message_input.count():
            await message_input.fill("")
            await message_input.fill(message)
        await self._click_visible_submit(page, expected_label="Apply")

    async def _submit_create_dialog(self, page) -> None:
        await self._click_visible_submit(page, expected_label="Create")
        await page.wait_for_timeout(1000)

    async def _search_alerts(self, page, alert_name: str) -> None:
        await self._click_first_if_present(
            page,
            [
                '[data-name="alerts-search-button"]',
                'button[title="Search"]',
            ],
            use_dom_click=True,
        )
        await page.wait_for_timeout(300)
        await self._fill_first_if_present(
            page,
            [
                '[data-name="alerts-search-input"] input',
                'input[placeholder*="Search"]',
                'input[type="search"]',
            ],
            alert_name,
        )
        await page.wait_for_timeout(500)

    async def _delete_matching_alert(self, page, alert_name: str) -> bool:
        alert_name_locator = page.locator('[data-name="alert-item-name"]').filter(has_text=alert_name).first
        if await alert_name_locator.count():
            row = alert_name_locator.locator("xpath=ancestor::*[.//*[@data-name='alert-delete-button']][1]").first
            if await row.count():
                delete_button = row.locator('[data-name="alert-delete-button"]').first
                if await delete_button.count():
                    await delete_button.evaluate("(el) => el.click()")
                    await page.wait_for_timeout(700)
                    await self._click_first_if_present(
                        page,
                        [
                            'button:has-text("Delete")',
                            'button:has-text("Confirm")',
                        ],
                        use_dom_click=True,
                    )
                    await page.wait_for_timeout(1000)
                    return True

        row = page.locator(f'text="{alert_name}"').first
        if await row.count() == 0:
            return False
        try:
            await row.click()
        except Exception:
            await row.evaluate("(el) => el.click()")
        await page.wait_for_timeout(300)
        await self._click_first(
            page,
            [
                '[data-name="alert-delete-button"]',
                'button[aria-label*="Delete"]',
                'button:has-text("Delete")',
                'button:has-text("Remove")',
            ],
            description="delete alert button",
            force=True,
        )
        await self._click_first_if_present(
            page,
            [
                'button:has-text("Delete")',
                'button:has-text("Confirm")',
            ],
            use_dom_click=True,
        )
        await page.wait_for_timeout(1000)
        return True

    async def _alert_exists(self, page, alert_name: str) -> bool:
        candidates = [
            page.locator('[data-name="alert-item-name"]').filter(has_text=alert_name).first,
            page.locator(f'text="{alert_name}"').first,
        ]
        for locator in candidates:
            try:
                if await locator.count():
                    return True
            except Exception:
                continue
        return False

    async def _click_first(self, page, selectors: list[str], *, description: str, force: bool = False) -> None:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                await locator.click(force=force)
                return
            except Exception:
                continue
        raise RuntimeError(f"could not find TradingView {description}")

    async def _click_first_if_present(self, page, selectors: list[str], *, use_dom_click: bool = False) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                if use_dom_click:
                    await locator.evaluate("(el) => el.click()")
                else:
                    await locator.click()
                return True
            except Exception:
                continue
        return False

    async def _fill_first_if_present(self, page, selectors: list[str], value: str) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() == 0:
                    continue
                await locator.fill("")
                await locator.fill(value)
                return True
            except Exception:
                continue
        return False

    async def _select_study_condition(self, page, *, study_prefix: str, operator_text: str) -> None:
        main_series = page.locator('[data-qa-id="ui-kit-disclosure-control main-series-select"]').first
        available_options: list[str] = []
        current_label = ""
        try:
            current_label = await main_series.inner_text()
        except Exception:
            current_label = ""
        if study_prefix not in current_label:
            for attempt in range(3):
                await self._wait_for_chart_studies(page)
                await main_series.evaluate("(el) => el.click()")
                await page.wait_for_timeout(900)
                options = page.locator('[role="option"]')
                option_count = await options.count()
                available_options = []
                matched = False
                for index in range(option_count):
                    option = options.nth(index)
                    text = (await option.inner_text()).strip()
                    if not text:
                        continue
                    available_options.append(text)
                    if study_prefix in text:
                        await option.evaluate("(el) => el.click()")
                        await page.wait_for_timeout(900)
                        matched = True
                        break
                if matched:
                    break
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
                await page.wait_for_timeout(2500 + (attempt * 1500))
            current_label = await main_series.inner_text()
            if study_prefix not in current_label:
                available = ", ".join(available_options) if available_options else "none"
                raise RuntimeError(
                    f"could not select TradingView study condition source: {study_prefix}; available options: {available}"
                )
        operator_dropdown = page.locator('[data-qa-id="operator-dropdown"]').first
        await operator_dropdown.evaluate("(el) => el.click()")
        await page.wait_for_timeout(600)
        options = page.locator('[role="option"]')
        option_count = await options.count()
        for index in range(option_count):
            option = options.nth(index)
            text = await option.inner_text()
            if text.strip() == operator_text:
                await option.evaluate("(el) => el.click()")
                await page.wait_for_timeout(700)
                return
        raise RuntimeError(f"could not select TradingView operator: {operator_text}")

    async def _wait_for_chart_studies(self, page) -> None:
        """Give TradingView chart studies time to hydrate before opening alert sources."""
        try:
            await page.wait_for_function(
                """() => {
                    const text = document.body?.innerText || '';
                    return text.includes('Multi-Path Momentum Scalp') || text.includes('MACD close 12 26 9');
                }""",
                timeout=12000,
            )
            await page.wait_for_timeout(2500)
        except Exception:
            return

    async def _click_visible_submit(self, page, *, expected_label: str) -> None:
        buttons = page.locator('[data-qa-id="submit"]')
        count = await buttons.count()
        for index in range(count - 1, -1, -1):
            button = buttons.nth(index)
            try:
                if not await button.is_visible():
                    continue
                label = (await button.inner_text()).strip()
                if expected_label and label != expected_label:
                    continue
                await button.evaluate("(el) => el.click()")
                await page.wait_for_timeout(700)
                return
            except Exception:
                continue
        raise RuntimeError(f"could not find TradingView submit button for {expected_label}")

    def _get_lock(self):
        if self._lock is None:
            import asyncio

            self._lock = asyncio.Lock()
        return self._lock

    async def _capture_diagnostics(self, page, name: str) -> None:
        diagnostics_dir = Path(self.settings.tradingview_alerts_user_data_dir).parent / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        timestamp = str(int(__import__("time").time()))
        screenshot_path = diagnostics_dir / f"{name}_{timestamp}.png"
        html_path = diagnostics_dir / f"{name}_{timestamp}.html"
        try:
            await page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            logger.exception("failed to capture TradingView screenshot diagnostics")
        try:
            html_path.write_text(await page.content(), encoding="utf-8")
        except Exception:
            logger.exception("failed to capture TradingView HTML diagnostics")


def describe_message_template(settings: Settings) -> dict[str, object]:
    template = settings.tradingview_alerts_message_template_json.strip()
    if not template:
        return {"configured": False}
    try:
        parsed = json.loads(render_alert_message(settings, "AAPL"))
    except json.JSONDecodeError:
        return {"configured": True, "valid_json": False}
    return {"configured": True, "valid_json": isinstance(parsed, dict), "keys": sorted(parsed.keys())}
