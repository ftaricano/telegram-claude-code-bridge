"""AskUserQuestion Telegram bridge support."""

from __future__ import annotations

import asyncio
import json
import secrets
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import structlog
from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from .context_manager import GENERAL_TOPIC_SENTINEL

logger = structlog.get_logger()

ASK_USER_QUESTION_TOOL_NAME = "AskUserQuestion"
ASK_USER_QUESTION_EXPIRED_MESSAGE = "user did not respond"
ASK_USER_QUESTION_ORPHAN_MESSAGE = "essa pergunta expirou, refaça o pedido"
ASK_USER_QUESTION_TIMEOUT_SECONDS = 600


@dataclass
class PendingAskUserQuestion:
    """In-memory pending AskUserQuestion state."""

    chat_id: int
    message_thread_id: int
    token: str
    question: str
    options: list[str]
    multi_select: bool
    allow_other: bool
    future: asyncio.Future[dict[str, Any] | str]
    selected_indexes: set[int] = field(default_factory=set)


def _normalize_thread_id(message_thread_id: Optional[int]) -> int:
    return message_thread_id or GENERAL_TOPIC_SENTINEL


def _option_label(option: Any) -> str:
    if isinstance(option, dict):
        label = option.get("label") or option.get("value") or option.get("text")
        if label is None:
            label = option.get("description")
        return str(label or "").strip()
    return str(option).strip()


def _extract_options(tool_input: dict[str, Any]) -> list[str]:
    raw_options = tool_input.get("choices")
    if raw_options is None:
        raw_options = tool_input.get("options")
    if raw_options is None:
        raw_options = []
    if not isinstance(raw_options, Iterable) or isinstance(raw_options, (str, bytes)):
        return []
    return [label for option in raw_options if (label := _option_label(option))]


def _extract_bool(tool_input: dict[str, Any], *keys: str) -> bool:
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, bool):
            return value
    return False


class AskUserQuestionRegistry:
    """Tracks in-flight AskUserQuestion prompts by chat, topic and token."""

    def __init__(self) -> None:
        self._pending: dict[tuple[int, int, str], PendingAskUserQuestion] = {}
        self._pending_other_reply: dict[
            tuple[int, int, str], PendingAskUserQuestion
        ] = {}

    def generate_token(self) -> str:
        """Generate the 12-character ephemeral callback token."""
        return secrets.token_urlsafe(9)

    def pending_tokens(self) -> list[str]:
        """Return pending tokens for tests and diagnostics."""
        return [token for _chat_id, _thread_id, token in self._pending]

    def get_pending_by_token(self, token: str) -> Optional[PendingAskUserQuestion]:
        """Find a pending question by token for diagnostics/tests."""
        for (_chat_id, _thread_id, current_token), pending in self._pending.items():
            if current_token == token:
                return pending
        return None

    def create_pending_question(
        self,
        *,
        chat_id: int,
        message_thread_id: Optional[int],
        token: str,
        question: str,
        options: list[str],
        multi_select: bool,
        allow_other: bool,
    ) -> PendingAskUserQuestion:
        """Create pending state without sending Telegram messages."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
        normalized_thread_id = _normalize_thread_id(message_thread_id)
        pending = PendingAskUserQuestion(
            chat_id=chat_id,
            message_thread_id=normalized_thread_id,
            token=token,
            question=question,
            options=options,
            multi_select=multi_select,
            allow_other=allow_other,
            future=loop.create_future(),
        )
        key = (chat_id, normalized_thread_id, token)
        self._pending[key] = pending
        pending.future.add_done_callback(lambda _future: self._cleanup(key))
        return pending

    def _cleanup(self, key: tuple[int, int, str]) -> None:
        pending = self._pending.pop(key, None)
        if pending is not None:
            self._pending_other_reply.pop(key, None)

    def build_reply_markup(
        self, pending: PendingAskUserQuestion
    ) -> InlineKeyboardMarkup:
        """Render the inline keyboard for a pending question."""
        rows: list[list[InlineKeyboardButton]] = []
        for idx, option in enumerate(pending.options):
            prefix = (
                "✓ " if pending.multi_select and idx in pending.selected_indexes else ""
            )
            callback_suffix = f"t:{idx}" if pending.multi_select else str(idx)
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{prefix}{option}",
                        callback_data=f"aq:{pending.token}:{callback_suffix}",
                    )
                ]
            )
        if pending.allow_other:
            rows.append(
                [
                    InlineKeyboardButton(
                        "✏️ Outro",
                        callback_data=f"aq:{pending.token}:o",
                    )
                ]
            )
        if pending.multi_select:
            rows.append(
                [
                    InlineKeyboardButton(
                        "✅ Confirmar",
                        callback_data=f"aq:{pending.token}:c",
                    )
                ]
            )
        return InlineKeyboardMarkup(rows)

    async def ask(
        self,
        *,
        bot: Any,
        chat_id: int,
        message_thread_id: Optional[int],
        tool_input: dict[str, Any],
        timeout_seconds: int = ASK_USER_QUESTION_TIMEOUT_SECONDS,
    ) -> dict[str, Any] | str:
        """Send a Telegram question and wait for the callback answer."""
        token = self.generate_token()
        question = str(tool_input.get("question") or "Choose an option:").strip()
        pending = self.create_pending_question(
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            token=token,
            question=question,
            options=_extract_options(tool_input),
            multi_select=_extract_bool(tool_input, "multiSelect", "multi_select"),
            allow_other=_extract_bool(tool_input, "allowOther", "allow_other", "other"),
        )
        send_kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": question,
            "reply_markup": self.build_reply_markup(pending),
        }
        if pending.message_thread_id != GENERAL_TOPIC_SENTINEL:
            send_kwargs["message_thread_id"] = pending.message_thread_id
        await bot.send_message(**send_kwargs)

        try:
            return await asyncio.wait_for(pending.future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            if not pending.future.done():
                pending.future.set_result(ASK_USER_QUESTION_EXPIRED_MESSAGE)
            await self._notify_timeout(bot, chat_id, pending.message_thread_id)
            return ASK_USER_QUESTION_EXPIRED_MESSAGE

    async def _notify_timeout(self, bot: Any, chat_id: int, thread_id: int) -> None:
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "text": "A pergunta expirou sem resposta. Refaça o pedido para tentar de novo.",
        }
        if thread_id != GENERAL_TOPIC_SENTINEL:
            kwargs["message_thread_id"] = thread_id
        await bot.send_message(**kwargs)

    async def resolve_callback(
        self, chat_id: int, message_thread_id: Optional[int], data: str
    ) -> bool:
        """Resolve or update a pending question from callback data."""
        parts = data.split(":")
        if len(parts) < 3 or parts[0] != "aq":
            return False
        token = parts[1]
        key = (chat_id, _normalize_thread_id(message_thread_id), token)
        pending = self._pending.get(key)
        if pending is None or pending.future.done():
            return False

        action = parts[2]
        if action == "t" and len(parts) == 4 and pending.multi_select:
            idx = int(parts[3])
            if idx in pending.selected_indexes:
                pending.selected_indexes.remove(idx)
            else:
                pending.selected_indexes.add(idx)
            return True

        if action == "c" and pending.multi_select:
            pending.future.set_result(
                {
                    "choices": [
                        pending.options[idx]
                        for idx in sorted(pending.selected_indexes)
                        if 0 <= idx < len(pending.options)
                    ]
                }
            )
            return True

        if action == "o" and pending.allow_other:
            self._pending_other_reply[key] = pending
            return True

        idx = int(action)
        if 0 <= idx < len(pending.options):
            pending.future.set_result({"choice": pending.options[idx]})
            return True
        return False

    def has_pending_other_reply(
        self, chat_id: int, message_thread_id: Optional[int], token: str
    ) -> bool:
        key = (chat_id, _normalize_thread_id(message_thread_id), token)
        return key in self._pending_other_reply

    async def resolve_other_reply(
        self, chat_id: int, message_thread_id: Optional[int], text: str
    ) -> bool:
        """Resolve the next free-text reply for a chat/topic."""
        normalized_thread_id = _normalize_thread_id(message_thread_id)
        for key, pending in list(self._pending_other_reply.items()):
            key_chat_id, key_thread_id, _token = key
            if key_chat_id != chat_id or key_thread_id != normalized_thread_id:
                continue
            if not pending.future.done():
                pending.future.set_result({"choice": text})
            self._pending_other_reply.pop(key, None)
            return True
        return False

    def build_pre_tool_use_hook(
        self,
        *,
        bot: Any,
        chat_id: int,
        message_thread_id: Optional[int],
        timeout_seconds: int = ASK_USER_QUESTION_TIMEOUT_SECONDS,
    ) -> Any:
        """Build the SDK PreToolUse hook for AskUserQuestion."""

        async def hook(
            hook_input: dict[str, Any], _tool_use_id: str | None, _hook_context: Any
        ) -> dict[str, Any]:
            if hook_input.get("tool_name") != ASK_USER_QUESTION_TOOL_NAME:
                return {}
            result = await self.ask(
                bot=bot,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                tool_input=hook_input.get("tool_input") or {},
                timeout_seconds=timeout_seconds,
            )
            if result == ASK_USER_QUESTION_EXPIRED_MESSAGE:
                reason = ASK_USER_QUESTION_EXPIRED_MESSAGE
            else:
                reason = json.dumps(result, ensure_ascii=False)
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }

        return hook


DEFAULT_ASK_USER_QUESTION_REGISTRY = AskUserQuestionRegistry()


def registry_from_context(
    context: ContextTypes.DEFAULT_TYPE,
) -> AskUserQuestionRegistry:
    registry = context.bot_data.get("ask_user_question_registry")
    if isinstance(registry, AskUserQuestionRegistry):
        return registry
    context.bot_data["ask_user_question_registry"] = DEFAULT_ASK_USER_QUESTION_REGISTRY
    return DEFAULT_ASK_USER_QUESTION_REGISTRY


async def ask_user_question_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle AskUserQuestion inline keyboard callbacks."""
    query = update.callback_query
    if query is None:
        return
    message = query.message
    chat_id = int(getattr(message, "chat_id", 0))
    thread_id = getattr(message, "message_thread_id", None)
    registry = registry_from_context(context)
    data = str(query.data or "")
    handled = await registry.resolve_callback(chat_id, thread_id, data)
    if not handled:
        await query.answer(text=ASK_USER_QUESTION_ORPHAN_MESSAGE, show_alert=False)
        return

    await query.answer()
    parts = data.split(":")
    token = parts[1] if len(parts) > 1 else ""
    pending = registry.get_pending_by_token(token)
    if pending is not None and len(parts) >= 3 and parts[2] == "t":
        await query.edit_message_reply_markup(
            reply_markup=registry.build_reply_markup(pending)
        )
    elif pending is not None and len(parts) >= 3 and parts[2] == "o":
        reply_text = getattr(message, "reply_text", None)
        if reply_text is not None:
            await reply_text(
                "Digite sua resposta:",
                reply_markup=ForceReply(selective=True),
            )
    else:
        await query.edit_message_reply_markup(reply_markup=None)


async def consume_ask_user_question_other_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Consume free-text Other replies before they become new Claude turns."""
    message = update.effective_message
    if message is None:
        return False
    text = getattr(message, "text", None)
    if not isinstance(text, str) or not text.strip():
        return False
    registry = registry_from_context(context)
    return await registry.resolve_other_reply(
        int(getattr(message, "chat_id", 0)),
        getattr(message, "message_thread_id", None),
        text.strip(),
    )
