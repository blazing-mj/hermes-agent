"""Regression tests for Telegram typing indicator lifecycle.

Telegram's `sendChatAction(typing)` is a one-shot state that lasts for a few
seconds. Re-arming it after the final response makes the user see "typing..."
after Hermes is already idle. Final gateway sends must therefore suppress the
post-send re-arm, while intermediate/progress sends can still refresh typing.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    mod = MagicMock()
    mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    mod.constants.ChatType.GROUP = "group"
    mod.constants.ChatType.SUPERGROUP = "supergroup"
    mod.constants.ChatType.CHANNEL = "channel"
    mod.constants.ChatType.PRIVATE = "private"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, mod)


_ensure_telegram_mock()

from gateway.platforms.telegram import TelegramAdapter  # noqa: E402


@pytest.fixture()
def telegram_adapter():
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    adapter._bot = SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)),
        send_chat_action=AsyncMock(),
    )
    return adapter


@pytest.mark.asyncio
async def test_telegram_final_send_can_suppress_typing_rearm(telegram_adapter):
    """A final response send must not create a fresh idle typing bubble."""
    result = await telegram_adapter.send(
        chat_id="12345",
        content="Done.",
        metadata={"notify": True, "suppress_typing_after_send": True},
    )

    assert result.success
    telegram_adapter._bot.send_message.assert_awaited_once()
    telegram_adapter._bot.send_chat_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_telegram_progress_send_still_rearms_typing_by_default(telegram_adapter):
    """Intermediate/progress sends keep the old behavior unless suppressed."""
    result = await telegram_adapter.send(
        chat_id="12345",
        content="Checking…",
        metadata={"notify": False},
    )

    assert result.success
    telegram_adapter._bot.send_message.assert_awaited_once()
    telegram_adapter._bot.send_chat_action.assert_awaited_once()


class _RecordingAdapter(BasePlatformAdapter):  # type: ignore[misc]
    async def connect(self):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {"name": chat_id, "type": "dm"}

    async def send(self, *args, **kwargs):
        self.sent.append(kwargs)  # type: ignore[attr-defined]
        return SendResult(success=True, message_id="m1", retryable=False)

    async def _keep_typing(self, chat_id, interval=2.0, metadata=None, stop_event=None):
        if stop_event is not None:
            await stop_event.wait()

    async def stop_typing(self, chat_id):
        self.stop_typing_calls.append(chat_id)  # type: ignore[attr-defined]


def _make_recording_adapter() -> _RecordingAdapter:
    adapter = object.__new__(_RecordingAdapter)
    adapter.config = PlatformConfig(enabled=True, token="***")
    adapter.platform = Platform.TELEGRAM
    adapter._message_handler = AsyncMock(return_value="Final answer")
    adapter._busy_session_handler = None
    adapter._active_sessions = {}
    adapter._pending_messages = {}
    adapter._session_tasks = {}
    adapter._background_tasks = set()
    adapter._post_delivery_callbacks = {}
    adapter._expected_cancelled_tasks = set()
    adapter._fatal_error_code = None
    adapter._fatal_error_message = None
    adapter._fatal_error_retryable = True
    adapter._fatal_error_handler = None
    adapter._running = True
    adapter._auto_tts_default = False
    adapter._auto_tts_enabled_chats = set()
    adapter._auto_tts_disabled_chats = set()
    adapter._typing_paused = set()
    adapter.sent = []  # type: ignore[attr-defined]
    adapter.stop_typing_calls = []  # type: ignore[attr-defined]
    return adapter


def _make_event() -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="u1",
        ),
        message_id="incoming-1",
    )


@pytest.mark.asyncio
async def test_gateway_final_text_send_marks_typing_rearm_suppressed():
    """The gateway final-response path must mark Telegram sends as final.

    This is the production path for Telegram chat replies; without this metadata
    TelegramAdapter.send() cannot tell a final answer from a progress message.
    """
    adapter = _make_recording_adapter()

    await adapter.handle_message(_make_event())
    tasks = list(adapter._background_tasks)
    assert tasks, "handle_message should spawn the background processor"
    await tasks[0]

    assert adapter.sent, "final response should be sent"  # type: ignore[attr-defined]
    metadata = adapter.sent[0]["metadata"]  # type: ignore[attr-defined]
    assert metadata["notify"] is True
    assert metadata["suppress_typing_after_send"] is True
