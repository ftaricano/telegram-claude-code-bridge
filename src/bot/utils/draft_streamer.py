"""Stream partial responses to Telegram via sendMessageDraft."""

import asyncio
import hashlib
import secrets
import time
from typing import Any, List, Optional

import structlog
import telegram

from src.bot.utils.telegram_resilience import resilient_telegram_call
from src.utils.constants import TELEGRAM_MAX_MESSAGE_LENGTH

logger = structlog.get_logger()

# Max tool lines shown in the draft header
_MAX_TOOL_LINES = 10

# JAR-155: how long (seconds) to pause draft sends after a failure before
# retrying. Previously the streamer disabled permanently on the first error,
# meaning a single Telegram rate-limit blip would kill progress updates for
# the rest of the session.
_DRAFT_FAILURE_COOLDOWN = 30.0
_MAX_DRAFT_SPLIT_CHUNKS = 10
_DRAFT_SPLIT_LOSS_NOTICE = "\n[draft truncated after split safety limit]"


def generate_draft_id() -> int:
    """Generate a non-zero positive draft ID.

    The same draft_id causes Telegram to animate text transitions instead of
    replacing the draft wholesale, giving a smooth streaming effect.
    """
    return secrets.randbits(30) | 1


class DraftStreamer:
    """Accumulates streamed text and sends periodic drafts to Telegram.

    The draft is composed of two sections:

    1. **Tool header** — compact lines showing tool calls and reasoning
       snippets as they arrive, e.g. ``"📖 Read  |  🔍 Grep  |  🐚 Bash"``.
    2. **Response body** — the actual assistant response text, streamed
       token-by-token.

    Both sections are combined into a single draft message and sent via
    ``sendMessageDraft``.

    Key design decisions:
    - Plain text drafts (no parse_mode) to avoid partial HTML/markdown errors.
    - Tail-truncation for messages >4096 chars: shows ``"\\u2026" + last 4093 chars``.
    - Self-disabling: any API error silently disables the streamer so the
      request continues with normal (non-streaming) delivery.
    """

    def __init__(
        self,
        bot: telegram.Bot,
        chat_id: int,
        draft_id: int,
        message_thread_id: Optional[int] = None,
        throttle_interval: float = 0.3,
        retry_attempts: int = 1,
        session_id: Optional[str] = None,
        durability: Optional[Any] = None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.draft_id = draft_id
        self.message_thread_id = message_thread_id
        self.throttle_interval = throttle_interval
        self.retry_attempts = retry_attempts
        self.session_id = session_id
        self.durability = durability

        self._tool_lines: List[str] = []
        self._accumulated_text = ""
        self._last_send_time = 0.0
        self._enabled = True
        self._disabled_until: float = 0.0  # epoch; 0 = not in cooldown
        self._in_flight_lock = asyncio.Lock()

    def _is_enabled(self) -> bool:
        """Return True if drafts should be sent, respecting cooldown."""
        if self._enabled:
            return True
        # Re-enable only if a cooldown was explicitly scheduled and has expired
        if self._disabled_until > 0 and time.time() >= self._disabled_until:
            self._enabled = True
            logger.debug(
                "Draft streamer re-enabled after cooldown", chat_id=self.chat_id
            )
            return True
        return False

    async def append_tool(self, line: str) -> None:
        """Append a tool activity line and send a draft if throttled."""
        if not line:
            return
        if not self._is_enabled() and self.durability is None:
            return
        self._tool_lines.append(line)
        if not self._is_enabled():
            await self._queue_current_draft()
            return
        now = time.time()
        if (now - self._last_send_time) >= self.throttle_interval:
            await self._send_draft()

    async def append_text(self, text: str) -> None:
        """Append streamed text and send a draft if throttle interval elapsed."""
        if not text:
            return
        if not self._is_enabled() and self.durability is None:
            return
        self._accumulated_text += text
        if not self._is_enabled():
            await self._queue_current_draft()
            return
        now = time.time()
        if (now - self._last_send_time) >= self.throttle_interval:
            await self._send_draft()

    async def flush(self) -> None:
        """Force-send the current accumulated text as a draft."""
        if not self._is_enabled():
            await self._queue_current_draft()
            return
        await self._flush_due_drafts()
        if not self._accumulated_text and not self._tool_lines:
            return
        await self._send_draft()

    def _compose_draft(self) -> str:
        """Combine tool header and response body into a single draft."""
        parts: List[str] = []

        if self._tool_lines:
            visible = self._tool_lines[-_MAX_TOOL_LINES:]
            overflow = len(self._tool_lines) - _MAX_TOOL_LINES
            if overflow >= 3:
                parts.append(f"... +{overflow} more")
            parts.extend(visible)

        if self._accumulated_text:
            if parts:
                parts.append("")  # blank separator line
            parts.append(self._accumulated_text)

        return "\n".join(parts)

    async def _send_draft(self) -> None:
        """Send the composed draft (tools + text) as a message draft."""
        async with self._in_flight_lock:
            draft_text = self._compose_draft()
            if not draft_text.strip():
                return

            chunks = self._split_draft_text(draft_text)

            delivered_all = True
            for chunk_index, chunk in enumerate(chunks):
                kwargs = {
                    "chat_id": self.chat_id,
                    "text": chunk,
                    "draft_id": self.draft_id + chunk_index,
                }
                if self.message_thread_id is not None:
                    kwargs["message_thread_id"] = self.message_thread_id

                result = await resilient_telegram_call(
                    lambda: self.bot.send_message_draft(**kwargs),
                    operation="draft.send_message_draft",
                    chat_id=self.chat_id,
                    attempts=self.retry_attempts,
                    fail_silently=True,
                )
                if result is None:
                    delivered_all = False
                    break

            if delivered_all:
                self._last_send_time = time.time()
            else:
                # JAR-155: cooldown instead of permanent disable. A single
                # flood-control blip was killing progress updates for the entire
                # session. After _DRAFT_FAILURE_COOLDOWN seconds the streamer
                # re-enables automatically via _is_enabled().
                self._enabled = False
                self._disabled_until = time.time() + _DRAFT_FAILURE_COOLDOWN
                await self._queue_current_draft()
                logger.debug(
                    "Draft send failed, entering cooldown",
                    chat_id=self.chat_id,
                    cooldown_seconds=_DRAFT_FAILURE_COOLDOWN,
                )

    async def _queue_current_draft(self) -> None:
        """Persist current draft while sends are cooling down."""
        if self.durability is None:
            return
        draft_text = self._compose_draft()
        if not draft_text.strip():
            return
        available_at = self._disabled_until if self._disabled_until > 0 else time.time()
        digest = hashlib.sha256(draft_text.encode("utf-8")).hexdigest()
        await self.durability.enqueue_draft(
            session_id=self.session_id,
            chat_id=self.chat_id,
            message_thread_id=self.message_thread_id,
            draft_id=self.draft_id,
            payload_text=draft_text,
            payload_hash=digest,
            available_at=available_at,
        )

    async def _flush_due_drafts(self) -> None:
        """Send persisted cooldown drafts in FIFO order before current text."""
        if self.durability is None:
            return
        due_drafts = await self.durability.list_due_drafts(
            now=time.time(),
            chat_id=self.chat_id,
            draft_id=self.draft_id,
        )
        for queued in due_drafts:
            kwargs = {
                "chat_id": queued.chat_id,
                "text": queued.payload_text,
                "draft_id": queued.draft_id,
            }
            if queued.message_thread_id is not None:
                kwargs["message_thread_id"] = queued.message_thread_id
            result = await resilient_telegram_call(
                lambda: self.bot.send_message_draft(**kwargs),
                operation="draft.replay_send_message_draft",
                chat_id=queued.chat_id,
                attempts=self.retry_attempts,
                fail_silently=True,
            )
            if result is None:
                break
            await self.durability.mark_draft_delivered(queued.id)

    def _split_draft_text(self, draft_text: str) -> List[str]:
        """Split oversized drafts without silently dropping the leading text."""
        if len(draft_text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return [draft_text]

        chunks = [
            draft_text[i : i + TELEGRAM_MAX_MESSAGE_LENGTH]
            for i in range(0, len(draft_text), TELEGRAM_MAX_MESSAGE_LENGTH)
        ]
        chunk_count_if_split = len(chunks)
        if len(chunks) > _MAX_DRAFT_SPLIT_CHUNKS:
            chunks = chunks[:_MAX_DRAFT_SPLIT_CHUNKS]
            notice_start = TELEGRAM_MAX_MESSAGE_LENGTH - len(_DRAFT_SPLIT_LOSS_NOTICE)
            chunks[-1] = chunks[-1][:notice_start] + _DRAFT_SPLIT_LOSS_NOTICE
            logger.error(
                "draft_truncation_loss",
                original_length=len(draft_text),
                truncated_to=sum(len(chunk) for chunk in chunks),
                chunk_count_if_split=chunk_count_if_split,
                delivered_chunk_count=len(chunks),
                chat_id=self.chat_id,
                session_id=self.session_id,
            )
        logger.warning(
            "draft_truncated",
            original_length=len(draft_text),
            truncated_to=sum(len(chunk) for chunk in chunks),
            chunk_count_if_split=chunk_count_if_split,
            chat_id=self.chat_id,
            session_id=self.session_id,
        )
        return chunks
