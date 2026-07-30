from __future__ import annotations

import html
import json
from urllib.error import URLError
from urllib.request import Request

import pytest

from app.telegram_notifier import TelegramNotificationError, TelegramNotifier


def test_telegram_notifier_sends_factual_html_escaped_deployment_message() -> None:
    requests: list[tuple[Request, float]] = []

    def send(request: Request, timeout: float) -> int:
        requests.append((request, timeout))
        return 200

    notifier = TelegramNotifier(
        token="123456:test-token",
        chat_id="-1001234567890",
        send=send,
        timeout_seconds=5,
    )

    notifier.deployment_detected(
        repository="qa<&",
        environment="test",
        version="Release  <1>",
        description="Fix & deploy\nKeep  spacing",
    )

    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url == "https://api.telegram.org/bot123456:test-token/sendMessage"
    assert timeout == 5
    assert request.get_header("Content-type") == "application/json"
    assert isinstance(request.data, bytes)
    assert json.loads(request.data) == {
        "chat_id": "-1001234567890",
        "parse_mode": "HTML",
        "text": (
            "<b>Успешный деплой</b>\n"
            "Проект: <i>qa&lt;&amp;</i>\n"
            "Площадка: <i>test</i>\n"
            "Версия: <i>Release  &lt;1&gt;</i>\n"
            "Описание: <i>Fix &amp; deploy\nKeep  spacing</i>"
        ),
    }
    assert "123456:test-token" not in request.data.decode()


def test_telegram_transport_error_does_not_expose_bot_token() -> None:
    def fail(_request: Request, _timeout: float) -> int:
        raise URLError("https://api.telegram.org/bot123456:test-token/sendMessage")

    notifier = TelegramNotifier(
        token="123456:test-token",
        chat_id="-1001234567890",
        send=fail,
    )

    with pytest.raises(TelegramNotificationError) as captured:
        notifier.deployment_detected(
            repository="qa-watcher",
            environment="test",
            version="Release",
            description="—",
        )

    assert "123456:test-token" not in str(captured.value)


def test_telegram_notifier_routes_to_supergroup_topic_without_changing_text() -> None:
    requests: list[Request] = []

    def send(request: Request, _timeout: float) -> int:
        requests.append(request)
        return 200

    notifier = TelegramNotifier(
        token="123456:test-token",
        chat_id="-1001234567890",
        message_thread_id=42,
        send=send,
    )

    notifier.deployment_detected(
        repository="qa-watcher",
        environment="test",
        version="Release",
        description="—",
    )

    assert isinstance(requests[0].data, bytes)
    assert json.loads(requests[0].data) == {
        "chat_id": "-1001234567890",
        "message_thread_id": 42,
        "parse_mode": "HTML",
        "text": (
            "<b>Успешный деплой</b>\n"
            "Проект: <i>qa-watcher</i>\n"
            "Площадка: <i>test</i>\n"
            "Версия: <i>Release</i>\n"
            "Описание: <i>—</i>"
        ),
    }


def test_telegram_notifier_respects_utf16_message_limit() -> None:
    requests: list[Request] = []

    def send(request: Request, _timeout: float) -> int:
        requests.append(request)
        return 200

    notifier = TelegramNotifier(
        token="123456:test-token",
        chat_id="-1001234567890",
        send=send,
    )

    notifier.deployment_detected(
        repository="🚀" * 255,
        environment="🚀" * 32,
        version="🚀" * 500,
        description="🚀" * 2500,
    )

    assert isinstance(requests[0].data, bytes)
    rendered = json.loads(requests[0].data)["text"]
    plain_text = html.unescape(
        rendered.replace("<b>", "")
        .replace("</b>", "")
        .replace("<i>", "")
        .replace("</i>", "")
    )
    assert len(plain_text.encode("utf-16-le")) // 2 <= 4096
