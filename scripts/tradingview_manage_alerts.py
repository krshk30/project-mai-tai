from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


DEFAULT_CHART_URL = "https://www.tradingview.com/chart/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List or delete TradingView alerts using a persistent browser profile.")
    parser.add_argument("--user-data-dir", required=True, help="Persistent browser profile directory.")
    parser.add_argument("--browser-channel", default="chrome", help="Browser channel, default: chrome.")
    parser.add_argument("--chart-url", default=DEFAULT_CHART_URL, help=f"Chart URL to open, default: {DEFAULT_CHART_URL}")
    parser.add_argument("--headless", action="store_true", help="Run headless.")
    parser.add_argument("--action", choices=["list", "delete-prefix", "delete-all"], default="list")
    parser.add_argument("--prefix", default="MAI_TAI:", help="Alert-name prefix for delete-prefix mode.")
    parser.add_argument("--output", help="Optional JSON output path.")
    return parser


async def open_alerts_panel(page) -> None:
    if await alerts_panel_open(page):
        await ensure_alerts_tab(page)
        return
    for selector in [
        '[data-name="alerts"]',
        'button[aria-label="Alerts"]',
        '[data-name="alerts-manager-button"]',
        'button:has-text("Alerts")',
    ]:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            await locator.click(force=True)
            await page.wait_for_timeout(1200)
            await ensure_alerts_tab(page)
            return
        except Exception:
            continue
    raise RuntimeError("could not open TradingView Alerts panel")


async def alerts_panel_open(page) -> bool:
    for selector in [
        '[data-name="alert-item-name"]',
        'text="Create alert"',
        'text="Log"',
    ]:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def ensure_alerts_tab(page) -> None:
    for selector in [
        'button:has-text("Alerts")',
        '[role="tab"]:has-text("Alerts")',
        'text="Alerts"',
    ]:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            aria_selected = await locator.get_attribute("aria-selected")
            classes = (await locator.get_attribute("class")) or ""
            if aria_selected == "true" or "active" in classes.lower():
                await page.wait_for_timeout(500)
                return
            await locator.click(force=True)
            await page.wait_for_timeout(1200)
            return
        except Exception:
            continue


async def list_alert_names(page) -> list[str]:
    names: list[str] = []
    locator = page.locator('[data-name="alert-item-name"]')
    count = await locator.count()
    for index in range(count):
        try:
            text = (await locator.nth(index).inner_text()).strip()
        except Exception:
            continue
        if text:
            names.append(text)
    return names


async def list_alert_names_stable(page) -> list[str]:
    latest: list[str] = []
    for _ in range(4):
        await page.wait_for_timeout(800)
        names = await list_alert_names(page)
        if names:
            latest = names
            break
    return latest


async def refresh_and_list_alert_names(page) -> list[str]:
    await open_alerts_panel(page)
    names = await list_alert_names_stable(page)
    if names:
        return names
    # TradingView sometimes needs one more wait cycle before rows appear.
    await page.wait_for_timeout(1200)
    return await list_alert_names_stable(page)


async def delete_alert_by_name(page, alert_name: str) -> bool:
    alert_name_locator = page.locator('[data-name="alert-item-name"]').filter(has_text=alert_name).first
    if await alert_name_locator.count():
        row = alert_name_locator.locator("xpath=ancestor::*[.//*[@data-name='alert-delete-button']][1]").first
        if await row.count():
            delete_button = row.locator('[data-name="alert-delete-button"]').first
            if await delete_button.count():
                try:
                    await delete_button.evaluate("(el) => el.click()")
                except Exception:
                    await delete_button.click(force=True)
                await page.wait_for_timeout(700)
                for selector in ['button:has-text("Delete")', 'button:has-text("Confirm")']:
                    locator = page.locator(selector).first
                    try:
                        if await locator.count():
                            await locator.evaluate("(el) => el.click()")
                            break
                    except Exception:
                        continue
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
    for selector in [
        '[data-name="alert-delete-button"]',
        'button[aria-label*="Delete"]',
        'button:has-text("Delete")',
        'button:has-text("Remove")',
    ]:
        locator = page.locator(selector).first
        try:
            if await locator.count() == 0:
                continue
            await locator.evaluate("(el) => el.click()")
            await page.wait_for_timeout(600)
            break
        except Exception:
            continue
    for selector in ['button:has-text("Delete")', 'button:has-text("Confirm")']:
        locator = page.locator(selector).first
        try:
            if await locator.count():
                await locator.evaluate("(el) => el.click()")
                break
        except Exception:
            continue
    await page.wait_for_timeout(1000)
    return True


async def run(args: argparse.Namespace) -> dict[str, object]:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=args.user_data_dir,
            channel=args.browser_channel,
            headless=args.headless,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(args.chart_url, wait_until="domcontentloaded", timeout=60_000)
            except PlaywrightTimeoutError:
                await page.goto(args.chart_url, wait_until="load", timeout=60_000)
            await page.wait_for_timeout(3000)
            before = await refresh_and_list_alert_names(page)
            await page.screenshot(path=str(Path(args.user_data_dir).parent / "alerts_before.png"), full_page=True)
            deleted: list[str] = []
            if args.action == "delete-prefix":
                for name in list(before):
                    if name.startswith(args.prefix):
                        if await delete_alert_by_name(page, name):
                            deleted.append(name)
                            await refresh_and_list_alert_names(page)
            elif args.action == "delete-all":
                for name in list(before):
                    if await delete_alert_by_name(page, name):
                        deleted.append(name)
                        await refresh_and_list_alert_names(page)
            after = await refresh_and_list_alert_names(page)
            await page.screenshot(path=str(Path(args.user_data_dir).parent / "alerts_after.png"), full_page=True)
            result = {
                "action": args.action,
                "prefix": args.prefix,
                "before": before,
                "deleted": deleted,
                "after": after,
                "page_url": page.url,
                "title": await page.title(),
            }
            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            return result
        finally:
            await context.close()


def main() -> int:
    args = build_parser().parse_args()
    result = asyncio.run(run(args))
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
