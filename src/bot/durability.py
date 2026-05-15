"""Durable Telegram delivery helpers for JAR-163."""

import hashlib
from types import SimpleNamespace
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


def build_idempotency_key(
    *,
    session_id: str,
    chat_id: int,
    message_thread_id: Optional[int],
    chunk_idx: int,
    payload_text: str,
) -> str:
    """Build a stable key for one outbound Telegram payload."""
    thread = "none" if message_thread_id is None else str(message_thread_id)
    digest = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    return f"{session_id}:{chat_id}:{thread}:{chunk_idx}:{digest}"


class TelegramDeliveryManager:
    """Write-ahead and idempotent wrapper around Telegram sends."""

    def __init__(self, durability_repo: Any):
        self.durability = durability_repo

    async def send_reply_text(
        self,
        message: Any,
        *,
        session_id: str,
        chat_id: int,
        message_thread_id: Optional[int],
        chunk_idx: int,
        text: str,
        parse_mode: Optional[str] = None,
        reply_markup: Any = None,
        reply_to_message_id: Optional[int] = None,
    ) -> Any:
        """Send reply text with checkpoint + idempotency protection."""
        idempotency_key = build_idempotency_key(
            session_id=session_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            chunk_idx=chunk_idx,
            payload_text=text,
        )
        existing_id = await self.durability.get_telegram_message_id(idempotency_key)
        if existing_id is not None:
            logger.info(
                "telegram_delivery_idempotent_hit",
                idempotency_key=idempotency_key,
                telegram_message_id=existing_id,
            )
            return SimpleNamespace(message_id=existing_id)

        checkpoint = await self.durability.create_message_checkpoint(
            session_id=session_id,
            chat_id=chat_id,
            message_thread_id=message_thread_id,
            chunk_idx=chunk_idx,
            payload_text=text,
            parse_mode=parse_mode,
            idempotency_key=idempotency_key,
        )

        try:
            await self.durability.mark_checkpoint_sent(checkpoint.id)
            kwargs = {
                "reply_markup": reply_markup,
                "reply_to_message_id": reply_to_message_id,
            }
            if parse_mode:
                kwargs["parse_mode"] = parse_mode
            result = await message.reply_text(text, **kwargs)
            telegram_message_id = getattr(result, "message_id", None)
            if telegram_message_id is None:
                telegram_message_id = 0
            await self.durability.mark_checkpoint_delivered(
                checkpoint.id,
                telegram_message_id=telegram_message_id,
                idempotency_key=idempotency_key,
            )
            return result
        except Exception as exc:
            await self.durability.mark_checkpoint_failed(checkpoint.id, str(exc))
            raise

    async def replay_pending(self, bot: Any, *, limit: int = 100) -> int:
        """Replay pending/sent checkpoints after daemon restart."""
        checkpoints = await self.durability.list_replayable_checkpoints(limit=limit)
        replayed = 0
        for checkpoint in checkpoints:
            existing_id = await self.durability.get_telegram_message_id(
                checkpoint.idempotency_key
            )
            if existing_id is not None:
                await self.durability.mark_checkpoint_delivered(
                    checkpoint.id,
                    telegram_message_id=existing_id,
                    idempotency_key=checkpoint.idempotency_key,
                )
                replayed += 1
                continue

            try:
                await self.durability.mark_checkpoint_sent(checkpoint.id)
                kwargs = {
                    "chat_id": checkpoint.chat_id,
                    "text": checkpoint.payload_text,
                }
                if checkpoint.message_thread_id is not None:
                    kwargs["message_thread_id"] = checkpoint.message_thread_id
                if checkpoint.parse_mode:
                    kwargs["parse_mode"] = checkpoint.parse_mode
                result = await bot.send_message(**kwargs)
                telegram_message_id = getattr(result, "message_id", 0)
                await self.durability.mark_checkpoint_delivered(
                    checkpoint.id,
                    telegram_message_id=telegram_message_id,
                    idempotency_key=checkpoint.idempotency_key,
                )
                replayed += 1
            except Exception as exc:
                logger.warning(
                    "telegram_checkpoint_replay_failed",
                    checkpoint_id=checkpoint.id,
                    error=str(exc),
                )
                await self.durability.mark_checkpoint_failed(checkpoint.id, str(exc))
        return replayed
