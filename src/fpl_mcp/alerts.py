"""Best-effort alerts for FPL login problems (Telegram and/or a generic webhook)."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)


async def send_alert(settings: Settings, message: str) -> None:
    """Send to every configured channel; never raises."""
    text = f"[Tashi FPL MCP] {message}"
    async with httpx.AsyncClient(timeout=10) as http:
        if settings.alert_telegram_bot_token and settings.alert_telegram_chat_id:
            try:
                response = await http.post(
                    f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendMessage",
                    json={"chat_id": settings.alert_telegram_chat_id, "text": text},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Telegram alert failed: %s", exc)
        if settings.alert_webhook_url:
            try:
                # "text" suits Slack and most webhooks; "content" suits Discord.
                response = await http.post(
                    settings.alert_webhook_url, json={"text": text, "content": text}
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.warning("Webhook alert failed: %s", exc)
    log.warning(text)
