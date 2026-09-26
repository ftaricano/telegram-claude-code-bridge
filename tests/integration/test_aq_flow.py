import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
from telegram import InlineKeyboardMarkup

from src.claude.ask_user_question import (
    AskUserQuestionRegistry,
    ask_user_question_callback_handler,
)
from src.claude.sdk_integration import ClaudeSDKManager
from src.config.settings import Settings


def _make_result_message(result=None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1000,
        duration_api_ms=800,
        is_error=False,
        num_turns=1,
        session_id="test-session",
        total_cost_usd=0.01,
        result=result,
    )


class _HookCapturingClient:
    captured_options = None

    def __init__(self, options):
        self.options = options
        type(self).captured_options = options
        self._query = SimpleNamespace(receive_messages=self._receive_messages)
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.query = AsyncMock()

    async def _receive_messages(self):
        yield AssistantMessage(
            content=[
                ToolUseBlock(
                    id="ask-1",
                    name="AskUserQuestion",
                    input={"question": "Pick one", "choices": ["A", "B"]},
                )
            ],
            model="claude-sonnet-4-20250514",
        )
        yield _make_result_message()


def _make_manager(tmp_path):
    return ClaudeSDKManager(
        Settings(
            telegram_bot_token="test:token",
            telegram_bot_username="testbot",
            approved_directory=tmp_path,
            claude_timeout_seconds=2,
            enable_mcp=False,
        )
    )


async def test_pret_tool_hook_sends_keyboard_and_returns_selected_choice(tmp_path):
    registry = AskUserQuestionRegistry()
    bot = SimpleNamespace(send_message=AsyncMock())
    manager = _make_manager(tmp_path)

    with (
        patch("src.claude.sdk_integration.parse_message", side_effect=lambda x: x),
        patch(
            "src.claude.sdk_integration.ClaudeSDKClient",
            side_effect=_HookCapturingClient,
        ),
    ):
        task = asyncio.create_task(
            manager.execute_command(
                prompt="Test",
                working_directory=Path("/test"),
                ask_user_question_bot=bot,
                ask_user_question_chat_id=1,
                ask_user_question_thread_id=10,
                ask_user_question_registry=registry,
            )
        )
        await asyncio.sleep(0)

        for _ in range(20):
            if _HookCapturingClient.captured_options is not None:
                break
            await asyncio.sleep(0.01)

        hook = _HookCapturingClient.captured_options.hooks["PreToolUse"][0].hooks[0]
        hook_task = asyncio.create_task(
            hook(
                {
                    "tool_name": "AskUserQuestion",
                    "tool_input": {"question": "Pick one", "choices": ["A", "B"]},
                },
                "ask-1",
                SimpleNamespace(signal=None),
            )
        )
        await asyncio.sleep(0)

        bot.send_message.assert_awaited_once()
        assert bot.send_message.await_args.kwargs["text"] == "Pick one"
        assert isinstance(
            bot.send_message.await_args.kwargs["reply_markup"], InlineKeyboardMarkup
        )

        token = next(iter(registry.pending_tokens()))
        update = SimpleNamespace(
            callback_query=SimpleNamespace(
                data=f"aq:{token}:1",
                answer=AsyncMock(),
                edit_message_reply_markup=AsyncMock(),
                message=SimpleNamespace(
                    chat_id=1,
                    message_thread_id=10,
                    reply_text=AsyncMock(),
                ),
            )
        )
        context = SimpleNamespace(bot_data={"ask_user_question_registry": registry})
        await ask_user_question_callback_handler(update, context)

        hook_result = await hook_task
        await task

    assert hook_result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads(
        hook_result["hookSpecificOutput"]["permissionDecisionReason"]
    ) == {"choice": "B"}


async def test_parallel_ask_user_question_flows_do_not_interfere(tmp_path):
    registry = AskUserQuestionRegistry()
    bot = SimpleNamespace(send_message=AsyncMock())
    hook_a = registry.build_pre_tool_use_hook(
        bot=bot,
        chat_id=1,
        message_thread_id=10,
        timeout_seconds=600,
    )
    hook_b = registry.build_pre_tool_use_hook(
        bot=bot,
        chat_id=2,
        message_thread_id=10,
        timeout_seconds=600,
    )

    task_a = asyncio.create_task(
        hook_a(
            {"tool_name": "AskUserQuestion", "tool_input": {"choices": ["A", "B"]}},
            "ask-a",
            SimpleNamespace(signal=None),
        )
    )
    task_b = asyncio.create_task(
        hook_b(
            {"tool_name": "AskUserQuestion", "tool_input": {"choices": ["C", "D"]}},
            "ask-b",
            SimpleNamespace(signal=None),
        )
    )
    await asyncio.sleep(0)

    tokens = list(registry.pending_tokens())
    assert len(tokens) == 2
    for token in tokens:
        pending = registry.get_pending_by_token(token)
        if pending and pending.chat_id == 1:
            token_a = token
        elif pending and pending.chat_id == 2:
            token_b = token

    assert await registry.resolve_callback(2, 10, f"aq:{token_a}:0") is False
    assert await registry.resolve_callback(1, 10, f"aq:{token_a}:1") is True
    assert await registry.resolve_callback(2, 10, f"aq:{token_b}:0") is True

    result_a = await task_a
    result_b = await task_b

    assert json.loads(result_a["hookSpecificOutput"]["permissionDecisionReason"]) == {
        "choice": "B"
    }
    assert json.loads(result_b["hookSpecificOutput"]["permissionDecisionReason"]) == {
        "choice": "C"
    }
