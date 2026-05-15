"""Notification helpers for the paper trader."""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

MAX_TELEGRAM_MESSAGE_CHARS = 3900


def telegram_configured() -> bool:
    return bool(_bot_token() and _chat_id())


def send_telegram(message: str, *, disable_notification: bool = False) -> bool:
    """Send a plain-text Telegram message if bot credentials are configured."""
    token = _bot_token()
    chat_id = _chat_id()
    if not token or not chat_id:
        return False

    ok = True
    for chunk in _message_chunks(message):
        ok = _send_telegram_chunk(
            token,
            chat_id,
            chunk,
            disable_notification=disable_notification,
        ) and ok
    return ok


def _send_telegram_chunk(
    token: str,
    chat_id: str,
    message: str,
    *,
    disable_notification: bool,
) -> bool:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_notification": disable_notification,
            },
            timeout=10,
        )
        try:
            data: dict[str, Any] = response.json()
        except ValueError:
            data = {"ok": False, "description": response.text}

        if response.status_code >= 400:
            log.warning(
                "Telegram notification failed: HTTP %s - %s",
                response.status_code,
                data.get("description", response.text),
            )
            return False

        if not data.get("ok", False):
            log.warning("Telegram notification failed: %s", data)
            return False
        return True
    except Exception as exc:
        log.warning("Telegram notification failed: %s", _safe_error(exc, token))
        return False


def _bot_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")


def _chat_id() -> str | None:
    return os.getenv("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_USER_ID")


def _message_chunks(message: str) -> list[str]:
    if len(message) <= MAX_TELEGRAM_MESSAGE_CHARS:
        return [message]

    chunks: list[str] = []
    remaining = message
    while remaining:
        chunk = remaining[:MAX_TELEGRAM_MESSAGE_CHARS]
        split_at = chunk.rfind("\n")
        if split_at > 500:
            chunk = chunk[:split_at]
        chunks.append(chunk)
        remaining = remaining[len(chunk):].lstrip()
    return chunks


def _safe_error(exc: Exception, token: str) -> str:
    return str(exc).replace(token, "<telegram-token>")
