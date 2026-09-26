import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import ForceReply, InlineKeyboardMarkup

from src.bot.handlers.message import _format_progress_update
from src.claude.ask_user_question import (
    ASK_USER_QUESTION_EXPIRED_MESSAGE,
    AskUserQuestionRegistry,
    ask_user_question_callback_handler,
    consume_ask_user_question_other_reply,
)
from src.claude.sdk_integration import StreamUpdate

TEST_AQ_TOKEN = "tok" + "123456789"


def _message(chat_id: int, thread_id: int | None = None):
    return SimpleNamespace(
        chat_id=chat_id,
        message_thread_id=thread_id,
        reply_text=AsyncMock(),
    )


def _callback_update(chat_id: int, thread_id: int | None, data: str):
    query = SimpleNamespace(
        data=data,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
        edit_message_text=AsyncMock(),
        message=_message(chat_id, thread_id),
    )
    return SimpleNamespace(callback_query=query)


def _text_update(chat_id: int, thread_id: int | None, text: str):
    return SimpleNamespace(
        effective_message=SimpleNamespace(
            chat_id=chat_id,
            message_thread_id=thread_id,
            text=text,
        )
    )


def _context(registry: AskUserQuestionRegistry):
    return SimpleNamespace(bot_data={"ask_user_question_registry": registry})


def test_token_generated_is_unique_and_12_chars():
    registry = AskUserQuestionRegistry()

    tokens = {registry.generate_token() for _ in range(100)}

    assert len(tokens) == 100
    assert all(len(token) == 12 for token in tokens)


async def test_registry_isolates_pending_questions_by_chat_and_thread():
    registry = AskUserQuestionRegistry()
    token = "same-token12"
    pending = registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=token,
        question="Pick",
        options=["A", "B"],
        multi_select=False,
        allow_other=False,
    )

    handled = await registry.resolve_callback(
        chat_id=2,
        message_thread_id=10,
        data=f"aq:{token}:0",
    )

    assert handled is False
    assert pending.future.done() is False


async def test_single_select_callback_resolves_future_with_selected_choice():
    registry = AskUserQuestionRegistry()
    pending = registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=TEST_AQ_TOKEN,
        question="Pick",
        options=["A", "B"],
        multi_select=False,
        allow_other=False,
    )

    handled = await registry.resolve_callback(1, 10, f"aq:{TEST_AQ_TOKEN}:0")

    assert handled is True
    assert pending.future.result() == {"choice": "A"}


async def test_multi_select_toggles_and_confirm_resolve_future_with_choices():
    registry = AskUserQuestionRegistry()
    pending = registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=TEST_AQ_TOKEN,
        question="Pick",
        options=["A", "B", "C"],
        multi_select=True,
        allow_other=False,
    )

    assert await registry.resolve_callback(1, 10, f"aq:{TEST_AQ_TOKEN}:t:0") is True
    assert await registry.resolve_callback(1, 10, f"aq:{TEST_AQ_TOKEN}:t:2") is True
    assert pending.future.done() is False
    assert await registry.resolve_callback(1, 10, f"aq:{TEST_AQ_TOKEN}:c") is True

    assert pending.future.result() == {"choices": ["A", "C"]}


async def test_other_callback_marks_pending_reply_and_next_text_resolves_future():
    registry = AskUserQuestionRegistry()
    pending = registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=TEST_AQ_TOKEN,
        question="Pick",
        options=["A", "B"],
        multi_select=False,
        allow_other=True,
    )

    assert await registry.resolve_callback(1, 10, f"aq:{TEST_AQ_TOKEN}:o") is True
    assert registry.has_pending_other_reply(1, 10, TEST_AQ_TOKEN) is True

    handled = await registry.resolve_other_reply(1, 10, "free text")

    assert handled is True
    assert pending.future.result() == {"choice": "free text"}


async def test_timeout_resolves_with_sentinel_and_notifies_chat(monkeypatch):
    registry = AskUserQuestionRegistry()
    bot = SimpleNamespace(send_message=AsyncMock())

    async def fast_timeout(awaitable, timeout):
        await asyncio.sleep(0)
        raise asyncio.TimeoutError

    monkeypatch.setattr(asyncio, "wait_for", fast_timeout)

    result = await registry.ask(
        bot=bot,
        chat_id=1,
        message_thread_id=10,
        tool_input={"question": "Pick", "choices": ["A"]},
        timeout_seconds=600,
    )

    assert result == ASK_USER_QUESTION_EXPIRED_MESSAGE
    assert bot.send_message.await_count == 2
    assert "expirou" in bot.send_message.await_args_list[-1].kwargs["text"]


async def test_orphan_callback_is_acknowledged_as_expired():
    registry = AskUserQuestionRegistry()
    update = _callback_update(1, 10, "aq:missingtoken1:0")
    context = _context(registry)

    await ask_user_question_callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once()
    assert "expirou" in update.callback_query.answer.await_args.kwargs["text"]


async def test_callback_handler_single_select_acknowledges_and_resolves():
    registry = AskUserQuestionRegistry()
    pending = registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=TEST_AQ_TOKEN,
        question="Pick",
        options=["A", "B"],
        multi_select=False,
        allow_other=False,
    )
    update = _callback_update(1, 10, f"aq:{TEST_AQ_TOKEN}:0")
    context = _context(registry)

    await ask_user_question_callback_handler(update, context)

    update.callback_query.answer.assert_awaited_once()
    assert pending.future.result() == {"choice": "A"}


async def test_callback_handler_other_sends_force_reply():
    registry = AskUserQuestionRegistry()
    registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=TEST_AQ_TOKEN,
        question="Pick",
        options=["A", "B"],
        multi_select=False,
        allow_other=True,
    )
    update = _callback_update(1, 10, f"aq:{TEST_AQ_TOKEN}:o")
    context = _context(registry)

    await ask_user_question_callback_handler(update, context)

    reply_markup = update.callback_query.message.reply_text.await_args.kwargs[
        "reply_markup"
    ]
    assert isinstance(reply_markup, ForceReply)


async def test_other_reply_consumer_resolves_pending_other_and_stops_message_flow():
    registry = AskUserQuestionRegistry()
    pending = registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=TEST_AQ_TOKEN,
        question="Pick",
        options=["A", "B"],
        multi_select=False,
        allow_other=True,
    )
    await registry.resolve_callback(1, 10, f"aq:{TEST_AQ_TOKEN}:o")

    handled = await consume_ask_user_question_other_reply(
        _text_update(1, 10, "custom answer"),
        _context(registry),
    )

    assert handled is True
    assert pending.future.result() == {"choice": "custom answer"}


def test_rendered_keyboard_uses_expected_callback_data_and_one_button_per_option():
    registry = AskUserQuestionRegistry()
    pending = registry.create_pending_question(
        chat_id=1,
        message_thread_id=10,
        token=TEST_AQ_TOKEN,
        question="Pick",
        options=["A", "B"],
        multi_select=False,
        allow_other=True,
    )

    markup = registry.build_reply_markup(pending)

    assert isinstance(markup, InlineKeyboardMarkup)
    buttons = [row[0] for row in markup.inline_keyboard]
    assert [button.callback_data for button in buttons] == [
        f"aq:{TEST_AQ_TOKEN}:0",
        f"aq:{TEST_AQ_TOKEN}:1",
        f"aq:{TEST_AQ_TOKEN}:o",
    ]


@pytest.mark.parametrize(
    "tool_names,expected",
    [
        (["AskUserQuestion"], None),
        (["Read", "AskUserQuestion"], "🔧 <b>Using tools:</b> Read"),
    ],
)
async def test_progress_banner_filters_ask_user_question(tool_names, expected):
    update = StreamUpdate(
        type="assistant",
        tool_calls=[{"name": tool_name} for tool_name in tool_names],
    )

    assert await _format_progress_update(update) == expected
