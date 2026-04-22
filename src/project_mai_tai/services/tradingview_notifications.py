from __future__ import annotations

import asyncio
from email.message import EmailMessage
import smtplib
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request, build_opener

from project_mai_tai.settings import Settings


class TradingViewAlertNotifier(Protocol):
    async def send_relogin_required(self, *, reason: str, operator_status: dict[str, object]) -> None: ...

    async def status(self) -> dict[str, object]: ...


class NoopTradingViewAlertNotifier:
    async def send_relogin_required(self, *, reason: str, operator_status: dict[str, object]) -> None:
        del reason, operator_status
        return None

    async def status(self) -> dict[str, object]:
        return {"provider": "none", "enabled": False}


class SMTPTradingViewAlertNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send_relogin_required(self, *, reason: str, operator_status: dict[str, object]) -> None:
        await asyncio.to_thread(self._send_sync, reason=reason, operator_status=operator_status)

    async def status(self) -> dict[str, object]:
        return {
            "provider": "smtp",
            "enabled": True,
            "from_address": self.settings.tradingview_alerts_notification_smtp_from,
            "to_addresses": self.settings.tradingview_alerts_notification_smtp_to_list,
        }

    def _send_sync(self, *, reason: str, operator_status: dict[str, object]) -> None:
        recipients = self.settings.tradingview_alerts_notification_smtp_to_list
        if not recipients:
            raise RuntimeError("TradingView SMTP notifier requires at least one recipient address")
        message = EmailMessage()
        message["Subject"] = "TradingView login required for Mai Tai alerts"
        message["From"] = self.settings.tradingview_alerts_notification_smtp_from
        message["To"] = ", ".join(recipients)
        message.set_content(_render_notification_body(settings=self.settings, reason=reason, operator_status=operator_status))

        with smtplib.SMTP(
            self.settings.tradingview_alerts_notification_smtp_host,
            self.settings.tradingview_alerts_notification_smtp_port,
            timeout=20,
        ) as smtp:
            if self.settings.tradingview_alerts_notification_smtp_starttls:
                smtp.starttls()
            username = self.settings.tradingview_alerts_notification_smtp_username or ""
            password = self.settings.tradingview_alerts_notification_smtp_password or ""
            if username:
                smtp.login(username, password)
            smtp.send_message(message)


class TwilioSMSAlertNotifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def send_relogin_required(self, *, reason: str, operator_status: dict[str, object]) -> None:
        await asyncio.to_thread(self._send_sync, reason=reason, operator_status=operator_status)

    async def status(self) -> dict[str, object]:
        return {
            "provider": "twilio_sms",
            "enabled": True,
            "from_number": self.settings.tradingview_alerts_notification_twilio_from_number,
            "to_number": self.settings.tradingview_alerts_notification_twilio_to_number,
        }

    def _send_sync(self, *, reason: str, operator_status: dict[str, object]) -> None:
        account_sid = self.settings.tradingview_alerts_notification_twilio_account_sid or ""
        auth_token = self.settings.tradingview_alerts_notification_twilio_auth_token or ""
        if not account_sid or not auth_token:
            raise RuntimeError("TradingView Twilio notifier requires account SID and auth token")
        body = _render_notification_body(settings=self.settings, reason=reason, operator_status=operator_status)
        form = urlencode(
            {
                "To": self.settings.tradingview_alerts_notification_twilio_to_number,
                "From": self.settings.tradingview_alerts_notification_twilio_from_number,
                "Body": body,
            }
        ).encode("utf-8")
        request = Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
            data=form,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        password_mgr = HTTPPasswordMgrWithDefaultRealm()
        password_mgr.add_password(None, request.full_url, account_sid, auth_token)
        handler = HTTPBasicAuthHandler(password_mgr)
        opener = build_opener(handler)
        with opener.open(request, timeout=20) as response:
            if response.status >= 400:
                raise RuntimeError(f"Twilio SMS send failed with status {response.status}")


def build_tradingview_alert_notifier(settings: Settings) -> TradingViewAlertNotifier:
    provider = settings.tradingview_alerts_notification_provider.strip().lower()
    if provider == "smtp":
        return SMTPTradingViewAlertNotifier(settings)
    if provider == "twilio_sms":
        return TwilioSMSAlertNotifier(settings)
    return NoopTradingViewAlertNotifier()


def _render_notification_body(
    *,
    settings: Settings,
    reason: str,
    operator_status: dict[str, object],
) -> str:
    lines = [
        "Mai Tai TradingView alert automation needs a fresh login.",
        "",
        f"Reason: {reason}",
        f"Operator: {settings.tradingview_alerts_operator}",
        f"User data dir: {settings.tradingview_alerts_user_data_dir}",
    ]
    last_url = operator_status.get("last_url")
    if last_url:
        lines.append(f"Last URL: {last_url}")
    auth_reason = operator_status.get("auth_reason")
    if auth_reason and auth_reason != reason:
        lines.append(f"Auth detail: {auth_reason}")
    lines.extend(
        [
            "",
            "Open the TradingView automation profile on this machine, sign in once, and restart or retry the alert service.",
        ]
    )
    return "\n".join(lines)
