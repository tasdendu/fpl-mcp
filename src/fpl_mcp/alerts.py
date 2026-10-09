"""Best-effort Telegram and webhook messages (login alerts and advisor briefings)."""

from __future__ import annotations

import logging

import httpx

from .config import Settings

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4000  # Telegram allows 4096 characters per message


def telegram_configured(settings: Settings) -> bool:
    return bool(settings.alert_telegram_bot_token and settings.alert_telegram_chat_id)


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on paragraph/line boundaries so each chunk fits one Telegram message."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current.strip():
        chunks.append(current)
    return [chunk.strip() for chunk in chunks if chunk.strip()]


async def send_message(settings: Settings, text: str) -> bool:
    """Send plain text to every configured channel; never raises. True if any succeeded."""
    delivered = False
    async with httpx.AsyncClient(timeout=15) as http:
        if telegram_configured(settings):
            url = f"https://api.telegram.org/bot{settings.alert_telegram_bot_token}/sendMessage"
            try:
                for chunk in split_message(text):
                    response = await http.post(
                        url, json={"chat_id": settings.alert_telegram_chat_id, "text": chunk}
                    )
                    response.raise_for_status()
                delivered = True
            except httpx.HTTPError as exc:
                log.warning("Telegram message failed: %s", exc)
        if settings.alert_webhook_url:
            try:
                # "text" suits Slack and most webhooks; "content" suits Discord.
                response = await http.post(
                    settings.alert_webhook_url, json={"text": text, "content": text[:2000]}
                )
                response.raise_for_status()
                delivered = True
            except httpx.HTTPError as exc:
                log.warning("Webhook message failed: %s", exc)
    return delivered


async def send_alert(settings: Settings, message: str) -> None:
    """Login/health alert: logged as a warning and sent to every channel."""
    text = f"⚠️ Tashi FPL MCP: {message}"
    log.warning(text)
    await send_message(settings, text)
