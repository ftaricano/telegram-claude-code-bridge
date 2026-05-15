"""Tests for Telegram media-group photo batching."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.bot.orchestrator import MessageOrchestrator
from src.config import create_test_config


class FakeImageHandler:
    """Image handler fake that records process_image calls."""

    def __init__(self):
        self.calls = []

    async def process_image(self, photo, caption=None):
        self.calls.append((photo, caption))
        return SimpleNamespace(
            base64_data=f"base64-{photo.file_unique_id}",
            prompt=caption or f"prompt-{photo.file_unique_id}",
            metadata={"format": "png"},
        )


def _settings(tmp_path: Path):
    return create_test_config(
        approved_directory=str(tmp_path),
        agentic_mode=True,
        media_group_window_seconds=0.05,
    )


def _update(
    *,
    chat_id: int = 100,
    message_id: int = 1,
    media_group_id: str | None = None,
    caption: str | None = None,
):
    update = MagicMock()
    update.effective_user.id = 123
    update.effective_chat.id = chat_id
    update.message.chat_id = chat_id
    update.message.message_id = message_id
    update.message.message_thread_id = None
    update.message.media_group_id = media_group_id
    update.message.caption = caption
    update.message.photo = [
        SimpleNamespace(file_unique_id=f"{chat_id}-{message_id}-thumb"),
        SimpleNamespace(file_unique_id=f"{chat_id}-{message_id}-full"),
    ]
    update.message.chat.id = chat_id
    update.message.chat.send_action = AsyncMock()
    update.message.reply_text = AsyncMock()
    progress_msg = AsyncMock()
    progress_msg.delete = AsyncMock()
    update.message.reply_text.return_value = progress_msg
    update.effective_message = update.message
    return update


def _context(settings, image_handler):
    features = MagicMock()
    features.get_image_handler.return_value = image_handler

    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "settings": settings,
        "features": features,
        "rate_limiter": None,
        "audit_logger": None,
    }
    return context


async def test_single_photo_no_media_group_id_unchanged(tmp_path):
    settings = _settings(tmp_path)
    image_handler = FakeImageHandler()
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator._handle_agentic_media_message = AsyncMock()

    update = _update(caption="describe this")
    context = _context(settings, image_handler)

    await orchestrator.agentic_photo(update, context)

    assert image_handler.calls == [(update.message.photo[-1], "describe this")]
    orchestrator._handle_agentic_media_message.assert_awaited_once()
    call = orchestrator._handle_agentic_media_message.await_args.kwargs
    assert call["prompt"] == "describe this"
    assert call["images"] == [{"data": "base64-100-1-full", "media_type": "image/png"}]


async def test_three_photos_same_group_join_into_one_claude_call(tmp_path):
    settings = _settings(tmp_path)
    image_handler = FakeImageHandler()
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator._handle_agentic_media_message = AsyncMock()
    context = _context(settings, image_handler)

    updates = [
        _update(message_id=1, media_group_id="album-1"),
        _update(message_id=2, media_group_id="album-1", caption="what are these?"),
        _update(message_id=3, media_group_id="album-1"),
    ]

    for update in updates:
        await orchestrator.agentic_photo(update, context)
    task = orchestrator._media_group_tasks["album-1"]
    await task

    orchestrator._handle_agentic_media_message.assert_awaited_once()
    call = orchestrator._handle_agentic_media_message.await_args.kwargs
    assert call["prompt"] == "what are these?"
    assert call["images"] == [
        {"data": "base64-100-1-full", "media_type": "image/png"},
        {"data": "base64-100-2-full", "media_type": "image/png"},
        {"data": "base64-100-3-full", "media_type": "image/png"},
    ]
    updates[0].message.reply_text.assert_awaited_once_with("Working...")
    updates[1].message.reply_text.assert_not_awaited()
    updates[2].message.reply_text.assert_not_awaited()


async def test_late_arrival_within_window_is_included(tmp_path):
    settings = _settings(tmp_path)
    image_handler = FakeImageHandler()
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator._handle_agentic_media_message = AsyncMock()
    context = _context(settings, image_handler)

    await orchestrator.agentic_photo(
        _update(message_id=1, media_group_id="album-late"), context
    )
    await orchestrator.agentic_photo(
        _update(message_id=2, media_group_id="album-late"), context
    )
    await asyncio.sleep(settings.media_group_window_seconds / 2)
    await orchestrator.agentic_photo(
        _update(message_id=3, media_group_id="album-late"), context
    )
    task = orchestrator._media_group_tasks["album-late"]
    await task

    orchestrator._handle_agentic_media_message.assert_awaited_once()
    call = orchestrator._handle_agentic_media_message.await_args.kwargs
    assert [image["data"] for image in call["images"]] == [
        "base64-100-1-full",
        "base64-100-2-full",
        "base64-100-3-full",
    ]


async def test_two_concurrent_groups_in_different_chats_isolated(tmp_path):
    settings = _settings(tmp_path)
    image_handler = FakeImageHandler()
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator._handle_agentic_media_message = AsyncMock()
    context = _context(settings, image_handler)

    await orchestrator.agentic_photo(
        _update(chat_id=100, message_id=1, media_group_id="album-a", caption="A"),
        context,
    )
    await orchestrator.agentic_photo(
        _update(chat_id=200, message_id=1, media_group_id="album-b", caption="B"),
        context,
    )
    await orchestrator.agentic_photo(
        _update(chat_id=100, message_id=2, media_group_id="album-a"),
        context,
    )
    await orchestrator.agentic_photo(
        _update(chat_id=200, message_id=2, media_group_id="album-b"),
        context,
    )

    await asyncio.gather(
        orchestrator._media_group_tasks["album-a"],
        orchestrator._media_group_tasks["album-b"],
    )

    calls = orchestrator._handle_agentic_media_message.await_args_list
    assert len(calls) == 2
    grouped = {
        call.kwargs["prompt"]: [image["data"] for image in call.kwargs["images"]]
        for call in calls
    }
    assert grouped == {
        "A": ["base64-100-1-full", "base64-100-2-full"],
        "B": ["base64-200-1-full", "base64-200-2-full"],
    }


async def test_default_prompt_when_no_caption(tmp_path):
    settings = _settings(tmp_path)
    image_handler = FakeImageHandler()
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator._handle_agentic_media_message = AsyncMock()
    context = _context(settings, image_handler)

    await orchestrator.agentic_photo(
        _update(message_id=1, media_group_id="album-default"), context
    )
    await orchestrator.agentic_photo(
        _update(message_id=2, media_group_id="album-default"), context
    )
    task = orchestrator._media_group_tasks["album-default"]
    await task

    orchestrator._handle_agentic_media_message.assert_awaited_once()
    call = orchestrator._handle_agentic_media_message.await_args.kwargs
    assert call["prompt"] == "Analyze these images."


async def test_buffer_state_cleaned_after_successful_flush(tmp_path):
    """All three media-group dicts must be empty once a flush completes."""
    settings = _settings(tmp_path)
    image_handler = FakeImageHandler()
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator._handle_agentic_media_message = AsyncMock()
    context = _context(settings, image_handler)

    await orchestrator.agentic_photo(
        _update(message_id=1, media_group_id="album-clean"), context
    )
    await orchestrator.agentic_photo(
        _update(message_id=2, media_group_id="album-clean", caption="caption"), context
    )
    task = orchestrator._media_group_tasks["album-clean"]
    await task

    assert orchestrator._media_group_buffer == {}
    assert orchestrator._media_group_tasks == {}
    assert orchestrator._media_group_progress == {}


async def test_buffer_state_cleaned_when_process_image_fails(tmp_path):
    """An exception raised mid-flush must NOT leak buffer/tasks/progress dicts."""
    settings = _settings(tmp_path)
    orchestrator = MessageOrchestrator(settings, {})
    orchestrator._handle_agentic_media_message = AsyncMock()

    failing_handler = MagicMock()
    failing_handler.process_image = AsyncMock(side_effect=RuntimeError("boom"))
    context = _context(settings, failing_handler)

    await orchestrator.agentic_photo(
        _update(message_id=1, media_group_id="album-boom", caption="x"), context
    )
    await orchestrator.agentic_photo(
        _update(message_id=2, media_group_id="album-boom"), context
    )
    task = orchestrator._media_group_tasks["album-boom"]
    await task

    # The dicts were popped under the lock BEFORE the heavy work began, so the
    # raised exception must not leave stale entries behind.
    assert orchestrator._media_group_buffer == {}
    assert orchestrator._media_group_tasks == {}
    assert orchestrator._media_group_progress == {}
    orchestrator._handle_agentic_media_message.assert_not_awaited()


async def test_flush_acquires_topic_lock_serializing_against_text(tmp_path):
    """While the album flush holds the topic lock, a text turn must wait."""
    settings = _settings(tmp_path)
    image_handler = FakeImageHandler()
    orchestrator = MessageOrchestrator(settings, {})
    context = _context(settings, image_handler)

    flush_started = asyncio.Event()
    release_flush = asyncio.Event()

    async def slow_handle_media(**_kwargs):
        flush_started.set()
        await release_flush.wait()

    orchestrator._handle_agentic_media_message = AsyncMock(
        side_effect=slow_handle_media
    )

    await orchestrator.agentic_photo(
        _update(message_id=1, media_group_id="album-lock", caption="hi"), context
    )
    await orchestrator.agentic_photo(
        _update(message_id=2, media_group_id="album-lock"), context
    )
    flush_task = orchestrator._media_group_tasks["album-lock"]

    await flush_started.wait()
    # Topic lock should now be held by the flush task. A second acquirer must
    # not be able to grab it until we release the flush.
    topic_key = orchestrator._current_topic_key(
        _update(message_id=1, media_group_id="album-lock"), context
    )
    contender = orchestrator._topic_lock(topic_key)
    assert contender.locked()

    release_flush.set()
    await flush_task

    assert not contender.locked()
    assert orchestrator._media_group_buffer == {}
    assert orchestrator._media_group_tasks == {}
    assert orchestrator._media_group_progress == {}
