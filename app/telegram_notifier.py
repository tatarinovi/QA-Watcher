from __future__ import annotations

import html
import json
from collections.abc import Callable
from typing import Final
from urllib.error import URLError
from urllib.request import Request, urlopen

TELEGRAM_API_ROOT: Final = "https://api.telegram.org"
TELEGRAM_MESSAGE_UTF16_LIMIT: Final = 4096
_MESSAGE_SKELETON: Final = (
    "Успешный деплой\nПроект: \nПлощадка: \nВерсия: \nОписание: "
)
TelegramSend = Callable[[Request, float], int]


class TelegramNotificationError(RuntimeError):
    """A token-safe Telegram delivery failure."""


class TelegramNotifier:
    def __init__(
        self,
        *,
        token: str,
        chat_id: str,
        message_thread_id: int | None = None,
        send: TelegramSend | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        if not token or any(character.isspace() or character == "/" for character in token):
            raise ValueError("Telegram token is malformed")
        if not chat_id or not chat_id.removeprefix("-").isdigit():
            raise ValueError("Telegram chat ID must be numeric")
        if message_thread_id is not None and message_thread_id <= 0:
            raise ValueError("Telegram message thread ID must be positive")
        if timeout_seconds <= 0:
            raise ValueError("Telegram timeout must be positive")
        self._url = f"{TELEGRAM_API_ROOT}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._message_thread_id = message_thread_id
        self._send = send or _send_request
        self._timeout_seconds = timeout_seconds

    def deployment_detected(
        self,
        *,
        repository: str,
        environment: str,
        version: str,
        description: str,
    ) -> None:
        remaining_units = TELEGRAM_MESSAGE_UTF16_LIMIT - _utf16_units(_MESSAGE_SKELETON)
        repository_html, remaining_units = _html_line(repository, 255, remaining_units)
        environment_html, remaining_units = _html_line(environment, 32, remaining_units)
        version_html, remaining_units = _html_line(version, 500, remaining_units)
        description_html, _ = _html_line(description, 2500, remaining_units)
        fields: dict[str, str | int] = {
            "chat_id": self._chat_id,
            "parse_mode": "HTML",
            "text": (
                "<b>Успешный деплой</b>\n"
                f"Проект: <i>{repository_html}</i>\n"
                f"Площадка: <i>{environment_html}</i>\n"
                f"Версия: <i>{version_html}</i>\n"
                f"Описание: <i>{description_html}</i>"
            ),
        }
        if self._message_thread_id is not None:
            fields["message_thread_id"] = self._message_thread_id
        payload = json.dumps(
            fields,
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            status = self._send(request, self._timeout_seconds)
        except (OSError, URLError) as exc:
            raise TelegramNotificationError(
                f"Telegram request failed: {type(exc).__name__}"
            ) from None
        if not 200 <= status < 300:
            raise TelegramNotificationError(f"Telegram HTTP {status}")


def _send_request(request: Request, timeout: float) -> int:
    with urlopen(request, timeout=timeout) as response:
        return int(response.status)

def _html_line(value: str, max_chars: int, remaining_units: int) -> tuple[str, int]:
    if not value.strip():
        raise ValueError("Telegram notification values must not be empty")
    used_units = 0
    end = 0
    for end, character in enumerate(value[:max_chars], start=1):
        character_units = 2 if ord(character) > 0xFFFF else 1
        if used_units + character_units > remaining_units:
            end -= 1
            break
        used_units += character_units
    bounded = value[:end]
    return html.escape(bounded, quote=False), remaining_units - used_units


def _utf16_units(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)
