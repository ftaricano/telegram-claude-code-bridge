"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import inspect
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.ask_user_question import consume_ask_user_question_other_reply
from ..claude.context_manager import GENERAL_TOPIC_SENTINEL, ContextManager, topic_key
from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from ..storage.models import TopicSessionModel
from .durability import TelegramDeliveryManager
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)
from .utils.telegram_resilience import resilient_telegram_call

logger = structlog.get_logger()

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

_PENDING_COMPACTED_PROMPT_KEY = "_pending_compacted_prompt"
_PENDING_COMPACTED_CONTEXT_KEY = "_pending_compacted_context_key"
_GOAL_NO_ACTIVE_MSG = (
    "No active goal.\n\n" "Use <code>/goal &lt;condition&gt;</code> to start one."
)
CLEAR_GOAL_ALIASES = {"clear", "stop", "off", "reset", "none", "cancel"}

# Session-control flags must be scoped to each Telegram forum topic, not user-wide.
_THREAD_SCOPED_SESSION_KEYS = (
    "force_new_session",
    "session_started",
    _PENDING_COMPACTED_PROMPT_KEY,
    _PENDING_COMPACTED_CONTEXT_KEY,
)

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[int, ActiveRequest] = {}
        self.context_manager = ContextManager(
            token_threshold=settings.context_token_threshold,
            keep_last=settings.context_compact_keep_last,
            summary_target_tokens=settings.context_summary_target_tokens,
        )
        self._topic_locks: Dict[str, asyncio.Lock] = {}
        self._known_commands: frozenset[str] = frozenset()
        self._media_group_buffer: Dict[str, List[Message]] = {}
        self._media_group_tasks: Dict[str, asyncio.Task] = {}
        self._media_group_progress: Dict[str, Message] = {}
        self._media_group_lock = asyncio.Lock()
        self._goal_resume_reset_keys: set[str] = set()

    @staticmethod
    def _build_zero_delivery_telemetry(
        original_content: Optional[str],
        formatted_messages: List[Any],
        messages_attempted: int,
        send_failures: int,
    ) -> Dict[str, Any]:
        """Build context for the zero-delivery guard without logging content."""
        parse_modes = {
            getattr(message, "parse_mode", None) for message in formatted_messages
        }
        if len(parse_modes) == 1:
            parse_mode_used = next(iter(parse_modes))
        elif not parse_modes:
            parse_mode_used = None
        else:
            parse_mode_used = "mixed"

        has_deliverable_text = any(
            getattr(message, "text", None) and message.text.strip()
            for message in formatted_messages
        )
        if not formatted_messages or not has_deliverable_text:
            failed_hop = "format"
        elif send_failures > 0:
            failed_hop = "send"
        elif messages_attempted == 0:
            failed_hop = "chunk"
        else:
            failed_hop = "ack"

        return {
            "original_content_length": len(original_content or ""),
            "parse_mode_used": parse_mode_used,
            "formatted_messages_count": len(formatted_messages),
            "failed_hop": failed_hop,
        }

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            chat = update.effective_chat
            should_enforce_project_thread = self.settings.enable_project_threads
            should_scope_thread_state = (
                message_thread_id is not None and chat is not None
            )

            if should_enforce_project_thread:
                if self.settings.project_threads_mode == "private":
                    should_enforce_project_thread = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce_project_thread = not is_sync_bypass

            if should_enforce_project_thread:
                allowed = await self._apply_thread_routing_context(
                    update,
                    context,
                    message_thread_id,
                )
                if not allowed:
                    return
            elif should_scope_thread_state:
                self._load_thread_state(
                    context,
                    chat_id=chat.id,
                    message_thread_id=message_thread_id,
                    project_root=self.settings.approved_directory,
                )

            try:
                await handler(update, context)
            finally:
                if should_enforce_project_thread or should_scope_thread_state:
                    self._persist_thread_state(context)

        return wrapped

    async def _apply_thread_routing_context(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message_thread_id: Optional[int],
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        self._load_thread_state(
            context,
            chat_id=chat.id,
            message_thread_id=message_thread_id,
            project_root=project.absolute_path,
            project_slug=project.slug,
            project_name=project.name,
        )
        return True

    def _load_thread_state(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        chat_id: int,
        message_thread_id: int,
        project_root: Path,
        project_slug: Optional[str] = None,
        project_name: Optional[str] = None,
    ) -> None:
        """Load thread-local compatibility keys into user_data."""
        state_key = f"{chat_id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project_root.resolve()
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["claude_session_id"] = state.get("claude_session_id")
        for key in _THREAD_SCOPED_SESSION_KEYS:
            if key in state:
                context.user_data[key] = state[key]
            else:
                context.user_data.pop(key, None)
        context.user_data["_thread_context"] = {
            "chat_id": chat_id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project_slug,
            "project_root": str(project_root),
            "project_name": project_name,
        }

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_state = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "project_slug": thread_context["project_slug"],
        }
        for key in _THREAD_SCOPED_SESSION_KEYS:
            if key in context.user_data:
                thread_state[key] = context.user_data[key]

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = thread_state

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    def _current_topic_key(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> str:
        """Return the runtime context key for the current Telegram topic."""
        thread_context = context.user_data.get("_thread_context")
        if thread_context and thread_context.get("state_key"):
            return str(thread_context["state_key"])

        message = update.effective_message or update.message
        chat_id = message.chat_id
        return topic_key(chat_id, getattr(message, "message_thread_id", None))

    def _current_topic_identity(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> tuple[int, int]:
        """Return the canonical storage key for the current Telegram topic."""
        thread_context = context.user_data.get("_thread_context")
        if thread_context:
            chat_id = thread_context.get("chat_id")
            thread_id = thread_context.get("message_thread_id")
            if isinstance(chat_id, int) and isinstance(thread_id, int):
                return chat_id, thread_id

        message = update.effective_message or update.message
        chat = update.effective_chat or getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None) or getattr(message, "chat_id")
        thread_id = self._extract_message_thread_id(update) or GENERAL_TOPIC_SENTINEL
        return int(chat_id), int(thread_id)

    def _topic_lock(self, key: str) -> asyncio.Lock:
        """Return the lock associated with a topic key, creating it if needed."""
        if key not in self._topic_locks:
            self._topic_locks[key] = asyncio.Lock()
        return self._topic_locks[key]

    @staticmethod
    def _topic_session_repo(context: ContextTypes.DEFAULT_TYPE) -> Any:
        """Return the topic session repository if storage exposes it."""
        storage = context.bot_data.get("storage")
        return getattr(storage, "topic_sessions", None) if storage is not None else None

    async def _load_topic_session(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> tuple[int, int, Any, Optional[TopicSessionModel]]:
        """Load the topic session row for the current update."""
        chat_id, thread_id = self._current_topic_identity(update, context)
        repo = self._topic_session_repo(context)
        if repo is None:
            return chat_id, thread_id, None, None
        return chat_id, thread_id, repo, await repo.get(chat_id, thread_id)

    @staticmethod
    async def _upsert_topic_session(
        repo: Any,
        *,
        chat_id: int,
        thread_id: int,
        user_id: int,
        session_id: str,
        project_path: Path,
    ) -> None:
        """Persist the session id associated with a Telegram topic."""
        now = datetime.now(UTC)
        await repo.upsert(
            TopicSessionModel(
                chat_id=chat_id,
                message_thread_id=thread_id,
                user_id=user_id,
                session_id=session_id,
                project_path=str(project_path),
                is_active=True,
                created_at=now,
                last_used=now,
            )
        )

    @staticmethod
    def _format_session_age(last_used: Optional[datetime]) -> str:
        """Format a compact relative age string."""
        if last_used is None:
            return "unknown"
        now = datetime.now(last_used.tzinfo or UTC)
        seconds = max(0, int((now - last_used).total_seconds()))
        if seconds < 60:
            return "just now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h ago"
        days = hours // 24
        return f"{days}d ago"

    @staticmethod
    def _format_duration(delta: Any) -> str:
        """Format a compact elapsed duration."""
        seconds = max(0, int(delta.total_seconds()))
        if seconds < 60:
            return f"{seconds}s"
        minutes, seconds = divmod(seconds, 60)
        if minutes < 60:
            return f"{minutes}m {seconds}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes}m"

    @staticmethod
    def _clear_pending_compacted_context(context: ContextTypes.DEFAULT_TYPE) -> None:
        """Clear one-shot compacted context carried between /compact and next prompt."""
        context.user_data.pop(_PENDING_COMPACTED_PROMPT_KEY, None)
        context.user_data.pop(_PENDING_COMPACTED_CONTEXT_KEY, None)

    async def _latest_persisted_compacted_prompt(
        self, key: str, context: ContextTypes.DEFAULT_TYPE
    ) -> Optional[str]:
        """Rehydrate the latest persisted summary when in-memory state is empty."""
        state = self.context_manager.get_state(key)
        if state.message_count or state.last_summary_text:
            return None

        storage = context.bot_data.get("storage")
        summary_store = (
            getattr(storage, "conversation_summaries", None)
            if storage is not None
            else None
        )
        get_latest = (
            getattr(summary_store, "get_latest_for_topic", None)
            if summary_store is not None
            else None
        )
        if not inspect.iscoroutinefunction(get_latest):
            return None

        latest = await get_latest(key)
        if latest is None or not getattr(latest, "summary_text", None):
            return None

        state.last_summary_text = latest.summary_text
        state.last_summary_at = getattr(latest, "created_at", None)
        state.tokens_used = getattr(latest, "tokens_after", 0) or 0
        state.compaction_count = max(state.compaction_count, 1)
        return self.context_manager.build_compacted_prompt(key, latest.summary_text)

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("goal", self.agentic_goal),
            ("verbose", self.agentic_verbose),
            ("repo", self.agentic_repo),
            ("context", self.context_status),
            ("compact", self.compact_context),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        forum_status_filter = (
            filters.StatusUpdate.FORUM_TOPIC_CLOSED
            | filters.StatusUpdate.FORUM_TOPIC_EDITED
        )
        forum_topic_deleted_filter = getattr(
            filters.StatusUpdate, "FORUM_TOPIC_DELETED", None
        )
        if forum_topic_deleted_filter is not None:
            forum_status_filter = forum_status_filter | forum_topic_deleted_filter
        else:
            # TODO(JAR-176): wire forum_topic_deleted if the installed PTB exposes it.
            logger.info("PTB does not expose FORUM_TOPIC_DELETED status filter")

        app.add_handler(
            MessageHandler(
                forum_status_filter,
                self._inject_deps(self._handle_forum_topic_status_update),
            ),
            group=5,
        )

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Claude, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        from .handlers import callback

        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(callback.aq_callback_handler),
                pattern=r"^aq:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        logger.info("Agentic handlers registered")

    async def _handle_forum_topic_status_update(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Persist Telegram forum topic closed/deleted status updates."""
        message = update.effective_message
        if message is None:
            return

        chat_id, thread_id = self._current_topic_identity(update, context)
        repo = self._topic_session_repo(context)
        if repo is None:
            return

        # Serialize against in-flight agentic_text/agentic_photo/etc. for the
        # same topic: without this lock, a close event would set_inactive only
        # to be silently overwritten by the in-flight handler's final upsert
        # (which always writes is_active=True).
        #
        # Note: in production, `StopAwareUpdateProcessor` already serializes
        # regular updates per (chat_id, message_thread_id) — see
        # `src/bot/update_processor.py`. This explicit lock guards against
        # future refactors that bypass that processor (direct handler calls,
        # tests, alternate event sources).
        topic_state_key = self._current_topic_key(update, context)
        lock = self._topic_lock(topic_state_key)
        acquired = False
        try:
            async with asyncio.timeout(self.settings.context_lock_timeout_seconds):
                await lock.acquire()
                acquired = True
        except TimeoutError:
            logger.warning(
                "Forum topic status update timed out waiting for topic lock; "
                "skipping persistence.",
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            return

        try:
            if getattr(message, "forum_topic_deleted", None) is not None:
                await repo.delete(chat_id, thread_id)
                logger.info(
                    "Deleted topic session after forum_topic_deleted",
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                )
                return

            if getattr(message, "forum_topic_closed", None) is not None:
                await repo.set_inactive(chat_id, thread_id)
                logger.info(
                    "Marked topic session inactive after forum_topic_closed",
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                )
                return

            edited = getattr(message, "forum_topic_edited", None)
            if getattr(edited, "is_closed", False):
                await repo.set_inactive(chat_id, thread_id)
                logger.info(
                    "Marked topic session inactive after forum_topic_edited",
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                )
        finally:
            if acquired:
                lock.release()

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(callback.aq_callback_handler),
                pattern=r"^aq:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("goal", "Set, check, or clear an autonomous goal"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("context", "Show topic context usage"),
                BotCommand("compact", "Compact topic context now"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Context runtime commands ---

    async def context_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Report tracked context usage for the current Telegram topic."""
        message = update.effective_message or update.message
        key = self._current_topic_key(update, context)
        state = self.context_manager.get_state(key)
        threshold = max(1, self.context_manager.token_threshold)
        pct = min(100.0, (state.tokens_used / threshold) * 100)

        await message.reply_text(
            "*Contexto do tópico*\n"
            f"- Chave: `{key}`\n"
            f"- Mensagens rastreadas: {state.message_count} mensagens\n"
            f"- Tokens estimados: {state.tokens_used}/{threshold} ({pct:.1f}%)\n"
            f"- Compactações: {state.compaction_count}",
            parse_mode="Markdown",
        )

    async def compact_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Force context compaction for the current Telegram topic."""
        message = update.effective_message or update.message
        claude_integration = context.bot_data.get("claude_integration")
        storage = context.bot_data.get("storage")
        summary_store = (
            getattr(storage, "conversation_summaries", None)
            if storage is not None
            else None
        )

        if claude_integration is None or summary_store is None:
            await message.reply_text("Context runtime indisponível neste momento.")
            return

        key = self._current_topic_key(update, context)
        lock = self._topic_lock(key)
        acquired = False
        try:
            async with asyncio.timeout(self.settings.context_lock_timeout_seconds):
                await lock.acquire()
                acquired = True
        except TimeoutError:
            await message.reply_text(
                "⏳ Este tópico ainda está processando uma resposta longa. "
                "Tenta compactar de novo em alguns segundos."
            )
            return

        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        # Resolve the session id. Once the Storage facade exposes
        # topic_sessions, that table is the *only* source of truth for this
        # topic — a missing row means "fresh topic" and must NOT silently
        # fall back to a legacy `claude_session_id` in PTB user_data, which
        # could carry session state from another topic or a pre-JAR-176
        # install. Only when the facade lacks topic_sessions entirely do we
        # honor user_data as the legacy fallback.
        _chat_id, _thread_id, topic_repo, topic_session = (
            await self._load_topic_session(update, context)
        )
        if topic_repo is not None:
            session_id = topic_session.session_id if topic_session else None
        else:
            session_id = context.user_data.get("claude_session_id")
        user = update.effective_user
        user_id = user.id if user is not None else 0

        try:
            compaction = await self.context_manager.compact(
                key=key,
                claude=claude_integration,
                summary_store=summary_store,
                session_id=session_id,
                working_directory=str(current_dir),
                user_id=user_id,
            )

            context.user_data[_PENDING_COMPACTED_PROMPT_KEY] = (
                compaction.compacted_prompt
            )
            context.user_data[_PENDING_COMPACTED_CONTEXT_KEY] = key

            if compaction.force_new_session:
                context.user_data["claude_session_id"] = None
                context.user_data["force_new_session"] = True

            fallback = "sim" if compaction.used_fallback else "não"
            await message.reply_text(
                "*Contexto compactado*\n"
                f"- Chave: `{key}`\n"
                f"- Mensagens incluídas: {compaction.messages_included}\n"
                f"- Tokens antes: {compaction.tokens_before}\n"
                f"- Tokens depois: {compaction.tokens_after}\n"
                f"- Fallback: {fallback}",
                parse_mode="Markdown",
            )
        finally:
            if acquired:
                lock.release()

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        self._clear_pending_compacted_context(context)
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        current_dir = context.user_data.get(
            "current_directory", self.settings.approved_directory
        )
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        chat_id, thread_id, repo, _ = await self._load_topic_session(update, context)
        if repo is not None:
            await repo.delete(chat_id, thread_id)
        context.user_data["claude_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True
        self._clear_pending_compacted_context(context)

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        current_dir = self.settings.approved_directory
        dir_display = str(current_dir)

        _chat_id, thread_id, _repo, topic_session = await self._load_topic_session(
            update, context
        )
        if topic_session is None:
            session_status = "none"
        else:
            active_label = "active" if topic_session.is_active else "inactive"
            age = self._format_session_age(topic_session.last_used)
            session_status = f"{active_label} ({age})"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        await update.message.reply_text(
            f"📌 topic={thread_id} · 📂 {dir_display} · "
            f"Session: {session_status}{cost_str}"
        )

    async def agentic_goal(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Native /goal: set/status/clear with Stop hook evaluator loop."""
        message = update.effective_message or update.message
        text = (getattr(message, "text", None) or "").strip()
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        chat_id, thread_id = self._current_topic_identity(update, context)
        integration = context.bot_data.get("claude_integration")
        goal_manager = getattr(integration, "goal_manager", None)
        if goal_manager is None:
            await message.reply_text("Goal manager unavailable.")
            return

        audit_logger = context.bot_data.get("audit_logger")
        success = True

        if not arg:
            goal = await goal_manager.get_status(chat_id, thread_id)
            if goal is None:
                await message.reply_text(_GOAL_NO_ACTIVE_MSG, parse_mode="HTML")
            else:
                elapsed = datetime.now(UTC) - goal.started_at
                await message.reply_text(
                    "🎯 <b>Goal active</b>\n\n"
                    f"<b>Condition:</b> {escape_html(goal.condition)}\n"
                    f"<b>Duration:</b> {self._format_duration(elapsed)}\n"
                    f"<b>Turns:</b> {goal.turn_count}\n"
                    f"<b>Tokens:</b> {goal.token_spend:,}\n"
                    "<b>Last reason:</b> "
                    f"{escape_html(goal.last_reason or '(awaiting first eval)')}",
                    parse_mode="HTML",
                )
            if audit_logger:
                await audit_logger.log_command(
                    user_id=update.effective_user.id,
                    command="goal",
                    args=["status"],
                    success=success,
                )
            return

        if arg.lower() in CLEAR_GOAL_ALIASES:
            cleared = await goal_manager.clear(chat_id, thread_id)
            if cleared:
                await message.reply_text(
                    "🧹 Goal cleared: "
                    f"<i>{escape_html(cleared.condition[:100])}</i>",
                    parse_mode="HTML",
                )
            else:
                await message.reply_text("No active goal to clear.")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=update.effective_user.id,
                    command="goal",
                    args=["clear"],
                    success=success,
                )
            return

        if len(arg) > 4000:
            success = False
            await message.reply_text("Goal condition too long (max 4000 chars).")
            if audit_logger:
                await audit_logger.log_command(
                    user_id=update.effective_user.id,
                    command="goal",
                    args=["set-too-long"],
                    success=success,
                )
            return

        goal = await goal_manager.set_goal(chat_id, thread_id, arg)
        await message.reply_text(
            f"🎯 <b>Goal set</b>\n<i>{escape_html(goal.condition[:200])}</i>\n\n"
            "Starting...",
            parse_mode="HTML",
        )
        if audit_logger:
            await audit_logger.log_command(
                user_id=update.effective_user.id,
                command="goal",
                args=["set"],
                success=success,
            )

        await self.agentic_text(
            update,
            context,
            prompt_override=goal.condition,
            topic_key=self._current_topic_key(update, context),
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    @staticmethod
    async def _should_force_new_claude_session(
        repo: Any, chat_id: int, thread_id: int
    ) -> bool:
        """Return whether Claude must start fresh for this topic."""
        topic_session = await repo.get(chat_id, thread_id)
        return topic_session is None or not topic_session.session_id

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    async def _send_typing_action(
        chat: Any,
        *,
        message_thread_id: Optional[int] = None,
        attempts: int = 1,
    ) -> None:
        """Send a resilient Telegram typing action for the current chat/topic."""
        kwargs: Dict[str, Any] = {}
        if message_thread_id is not None:
            kwargs["message_thread_id"] = message_thread_id

        await resilient_telegram_call(
            lambda: chat.send_action("typing", **kwargs),
            operation="send_action.typing",
            chat_id=getattr(chat, "id", None),
            attempts=attempts,
            fail_silently=True,
        )

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
        attempts: int = 1,
        message_thread_id: Optional[int] = None,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing immediately and then every *interval* seconds,
        independently of stream events. Telegram clients stop showing the
        typing indicator after a short timeout, so the default interval stays
        below JAR-138's <=3s visible-gap budget even during long tool chains
        and SDK reentries that produce no stream events.

        In forum topics, Telegram only displays the action in the visible topic
        when ``message_thread_id`` is passed through to ``sendChatAction``.
        Telegram failures are intentionally swallowed so a bad typing action
        never cancels the Claude request.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await MessageOrchestrator._send_typing_action(
                        chat,
                        message_thread_id=message_thread_id,
                        attempts=attempts,
                    )
                    await asyncio.sleep(interval)
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = mcp_images is not None and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure
        progress_failures = [0]
        progress_disabled = [False]
        progress_interval = self.settings.telegram_progress_edit_interval
        progress_max_failures = self.settings.telegram_progress_max_failures
        telegram_attempts = self.settings.telegram_api_retry_attempts

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if tc_name == "send_image_to_user" or tc_name.endswith(
                        "__send_image_to_user"
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    if name == "AskUserQuestion":
                        continue
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits.
            # If Telegram starts rejecting progress edits, disable progress UI for
            # this request and let Claude continue to final delivery.
            if not draft_streamer and verbose_level >= 1 and not progress_disabled[0]:
                now = time.time()
                if (now - last_edit_time[0]) >= progress_interval and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    result = await resilient_telegram_call(
                        lambda: progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        ),
                        operation="progress.edit_text",
                        chat_id=getattr(
                            getattr(progress_msg, "chat", None), "id", None
                        ),
                        attempts=telegram_attempts,
                        fail_silently=True,
                    )
                    if result is None:
                        progress_failures[0] += 1
                        if progress_failures[0] >= progress_max_failures:
                            progress_disabled[0] = True
                            logger.warning(
                                "Disabling Telegram progress edits for request",
                                failures=progress_failures[0],
                            )

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def agentic_text(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        prompt_override: Optional[str] = None,
        topic_key: Optional[str] = None,
    ) -> None:
        """Direct Claude passthrough serialized per Telegram topic."""
        if await consume_ask_user_question_other_reply(update, context):
            return

        message_text = update.message.text
        topic_state_key = topic_key or self._current_topic_key(update, context)
        lock = self._topic_lock(topic_state_key)

        acquired = False
        try:
            async with asyncio.timeout(self.settings.context_lock_timeout_seconds):
                await lock.acquire()
                acquired = True
        except TimeoutError:
            await update.message.reply_text(
                "⏳ Este tópico ainda está processando uma resposta longa. "
                "Tenta de novo em alguns segundos."
            )
            return

        try:
            locked_kwargs = {}
            if prompt_override is not None:
                locked_kwargs["prompt_override"] = prompt_override
            await self._agentic_text_locked(
                update,
                context,
                topic_state_key,
                message_text,
                **locked_kwargs,
            )
        finally:
            if acquired:
                lock.release()

    async def _agentic_text_locked(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        topic_state_key: str,
        message_text: str,
        *,
        prompt_override: Optional[str] = None,
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await self._send_typing_action(
            chat,
            message_thread_id=update.message.message_thread_id,
            attempts=self.settings.telegram_api_retry_attempts,
        )

        verbose_level = self._get_verbose_level(context)

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "Working...", reply_markup=stop_kb
        )

        # Register active request for stop callback
        active_request = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[user_id] = active_request

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(user_id, None)
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return

        current_dir = self.settings.approved_directory
        chat_id, thread_id, topic_repo, topic_session = await self._load_topic_session(
            update, context
        )
        if topic_repo is None:
            session_id = context.user_data.get("claude_session_id")
            force_new = bool(
                context.user_data.get("force_new_session")
                or (context.user_data.get("_thread_context") and not session_id)
            )
        else:
            if topic_session and not topic_session.is_active:
                await topic_repo.reactivate(chat_id, thread_id)
                topic_session = await topic_repo.get(chat_id, thread_id)
            session_id = topic_session.session_id if topic_session else None
            force_new = await self._should_force_new_claude_session(
                topic_repo, chat_id, thread_id
            )

        goal_manager = getattr(claude_integration, "goal_manager", None)
        if goal_manager is not None and topic_session and session_id:
            resume_key = f"{chat_id}:{thread_id}:{session_id}"
            if resume_key not in self._goal_resume_reset_keys:
                await goal_manager.repo.reset_on_resume(chat_id, thread_id)
                self._goal_resume_reset_keys.add(resume_key)

        thread_context = context.user_data.get("_thread_context")
        if thread_context:
            logger.info(
                "Thread-scoped Claude routing",
                chat_id=thread_context.get("chat_id"),
                message_thread_id=thread_context.get("message_thread_id"),
                state_key=thread_context.get("state_key"),
                session_id=session_id,
                force_new=force_new,
            )

        storage = context.bot_data.get("storage")
        durability = getattr(storage, "durability", None) if storage else None
        delivery = TelegramDeliveryManager(durability) if durability else None
        delivery_session_id = session_id or f"telegram:{chat_id}:{thread_id}"

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
                retry_attempts=self.settings.telegram_api_retry_attempts,
                session_id=session_id,
                durability=durability,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            approved_directory=self.settings.approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
        )

        prompt_for_claude = prompt_override or message_text
        run_session_id = session_id
        run_force_new = force_new
        pending_compacted_prompt = context.user_data.get(_PENDING_COMPACTED_PROMPT_KEY)
        pending_compacted_key = context.user_data.get(_PENDING_COMPACTED_CONTEXT_KEY)
        use_pending_compacted_prompt = (
            bool(pending_compacted_prompt) and pending_compacted_key == topic_state_key
        )
        using_compacted_prompt = False
        if use_pending_compacted_prompt:
            prompt_for_claude = (
                f"{pending_compacted_prompt}\n\n"
                f"New user message:\n{prompt_override or message_text}"
            )
            run_session_id = None
            run_force_new = True
            self._clear_pending_compacted_context(context)
            using_compacted_prompt = True
        elif self.settings.context_runtime_enabled:
            compacted_prompt = await self._latest_persisted_compacted_prompt(
                topic_state_key, context
            )
            if compacted_prompt:
                prompt_for_claude = (
                    f"{compacted_prompt}\n\n"
                    f"New user message:\n{prompt_override or message_text}"
                )
                run_session_id = None
                run_force_new = True
                using_compacted_prompt = True

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(
            chat,
            message_thread_id=update.message.message_thread_id,
            attempts=self.settings.telegram_api_retry_attempts,
        )

        success = True
        response_content: Optional[str] = None
        try:
            if (
                self.settings.context_runtime_enabled
                and not using_compacted_prompt
                and self.context_manager.would_exceed_limit(
                    topic_state_key, message_text
                )
            ):
                storage = context.bot_data.get("storage")
                summary_store = (
                    getattr(storage, "conversation_summaries", None)
                    if storage is not None
                    else None
                )
                try:
                    await progress_msg.edit_text(
                        "Compacting long topic context before continuing...",
                        reply_markup=stop_kb,
                    )
                except Exception:
                    logger.debug("Failed to update progress message for compaction")

                compaction = await self.context_manager.compact(
                    key=topic_state_key,
                    claude=claude_integration,
                    summary_store=summary_store,
                    session_id=session_id,
                    working_directory=str(current_dir),
                    user_id=user_id,
                )
                prompt_for_claude = (
                    f"{compaction.compacted_prompt}\n\n"
                    f"New user message:\n{prompt_override or message_text}"
                )
                run_session_id = None
                run_force_new = True

            claude_response = await claude_integration.run_command(
                prompt=prompt_for_claude,
                working_directory=current_dir,
                user_id=user_id,
                session_id=run_session_id,
                on_stream=on_stream,
                force_new=run_force_new,
                interrupt_event=interrupt_event,
                chat_id=chat_id,
                message_thread_id=thread_id,
                ask_user_question_bot=context.bot,
                ask_user_question_chat_id=chat_id,
                ask_user_question_thread_id=thread_id,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            delivery_session_id = claude_response.session_id

            if topic_repo is not None:
                await self._upsert_topic_session(
                    topic_repo,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    user_id=user_id,
                    session_id=claude_response.session_id,
                    project_path=self.settings.approved_directory,
                )
            else:
                # Legacy fallback parity with agentic_photo/document/voice:
                # without topic_sessions storage, persist the latest session
                # id back into PTB user_data so the next turn can resume.
                context.user_data["claude_session_id"] = claude_response.session_id

            if self.settings.context_runtime_enabled:
                self.context_manager.record_turn(
                    topic_state_key,
                    user_text=message_text,
                    assistant_text=claude_response.content or "",
                    session_id=claude_response.session_id,
                )

            if goal_manager is not None:
                await goal_manager.repo.add_token_spend(
                    chat_id,
                    thread_id,
                    int(getattr(claude_response, "token_count", 0) or 0),
                )

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            # Store interaction
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"
            elif (
                self.settings.claude_max_turns is not None
                and claude_response.num_turns >= self.settings.claude_max_turns
            ):
                response_content = (response_content or "") + (
                    f"\n\n_⚠️ Limite de {self.settings.claude_max_turns} turnos atingido "
                    f"— a resposta pode estar incompleta. Envie uma mensagem de continuação "
                    f"para prosseguir._"
                )

            formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            messages_sent = 0
            messages_attempted = 0
            send_failures = 0
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    messages_attempted += 1
                    if delivery:
                        await delivery.send_reply_text(
                            update.message,
                            session_id=delivery_session_id,
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                            chunk_idx=i,
                            text=message.text,
                            parse_mode=message.parse_mode,
                            reply_markup=None,  # No keyboards in agentic mode
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    else:
                        await update.message.reply_text(
                            message.text,
                            parse_mode=message.parse_mode,
                            reply_markup=None,  # No keyboards in agentic mode
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    messages_sent += 1
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        send_failures += 1
                        if delivery:
                            await delivery.send_reply_text(
                                update.message,
                                session_id=delivery_session_id,
                                chat_id=chat_id,
                                message_thread_id=thread_id,
                                chunk_idx=i,
                                text=message.text,
                                reply_markup=None,
                                reply_to_message_id=(
                                    update.message.message_id if i == 0 else None
                                ),
                            )
                        else:
                            await update.message.reply_text(
                                message.text,
                                reply_markup=None,
                                reply_to_message_id=(
                                    update.message.message_id if i == 0 else None
                                ),
                            )
                        messages_sent += 1
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                        messages_sent += 1

            # JAR-155: guard against silent no-delivery — if Claude produced
            # a response but nothing was actually sent (all messages were empty
            # after strip or all sends failed), notify the user explicitly.
            if messages_sent == 0 and not caption_sent:
                zero_delivery_telemetry = self._build_zero_delivery_telemetry(
                    response_content,
                    formatted_messages,
                    messages_attempted,
                    send_failures,
                )
                logger.error(
                    "agentic_send_loop_delivered_nothing",
                    formatted_count=len(formatted_messages),
                    user_id=user_id,
                    **zero_delivery_telemetry,
                )
                try:
                    await update.message.reply_text(
                        "⚠️ A resposta foi gerada mas não pôde ser entregue. "
                        "Tente novamente.",
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception:
                    pass

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = self.settings.approved_directory
        chat_id, thread_id, topic_repo, topic_session = await self._load_topic_session(
            update, context
        )
        if topic_repo is None:
            session_id = context.user_data.get("claude_session_id")
            force_new = bool(
                context.user_data.get("force_new_session")
                or (context.user_data.get("_thread_context") and not session_id)
            )
        else:
            if topic_session and not topic_session.is_active:
                await topic_repo.reactivate(chat_id, thread_id)
                topic_session = await topic_repo.get(chat_id, thread_id)
            session_id = topic_session.session_id if topic_session else None
            force_new = await self._should_force_new_claude_session(
                topic_repo, chat_id, thread_id
            )

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                chat_id=chat_id,
                message_thread_id=thread_id,
                ask_user_question_bot=context.bot,
                ask_user_question_chat_id=chat_id,
                ask_user_question_thread_id=thread_id,
            )

            if force_new:
                context.user_data["force_new_session"] = False

            if topic_repo is None:
                context.user_data["claude_session_id"] = claude_response.session_id
            else:
                await self._upsert_topic_session(
                    topic_repo,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    user_id=user_id,
                    session_id=claude_response.session_id,
                    project_path=self.settings.approved_directory,
                )

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        media_group_id = update.message.media_group_id
        if media_group_id is not None:
            chat = update.message.chat
            await chat.send_action("typing")

            async with self._media_group_lock:
                self._media_group_buffer.setdefault(media_group_id, []).append(
                    update.message
                )
                if media_group_id not in self._media_group_progress:
                    self._media_group_progress[media_group_id] = (
                        await update.message.reply_text("Working...")
                    )

                task = self._media_group_tasks.get(media_group_id)
                if task:
                    task.cancel()
                self._media_group_tasks[media_group_id] = asyncio.create_task(
                    self._flush_media_group(media_group_id, update, context)
                )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = processed_image.metadata.get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def _flush_media_group(
        self,
        media_group_id: str,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Process a completed Telegram media group as one Claude media request."""
        try:
            await asyncio.sleep(self.settings.media_group_window_seconds)
        except asyncio.CancelledError:
            return

        async with self._media_group_lock:
            messages = self._media_group_buffer.pop(media_group_id, [])
            progress_msg = self._media_group_progress.pop(media_group_id, None)
            self._media_group_tasks.pop(media_group_id, None)

        if not messages:
            return

        user_id = update.effective_user.id
        chat = update.message.chat
        prompt = next(
            (
                caption
                for message in messages
                if isinstance((caption := message.caption), str) and caption.strip()
            ),
            "Analyze these images.",
        )

        # Serialize the album's Claude call against any other turn for the
        # same topic. _flush_media_group runs as a background task, so the
        # StopAwareUpdateProcessor cannot help us here — without this lock a
        # plain text/photo arriving during the debounce or the Claude call
        # would race the album for the topic's session state.
        topic_state_key = self._current_topic_key(update, context)
        topic_lock = self._topic_lock(topic_state_key)
        acquired = False
        try:
            async with asyncio.timeout(self.settings.context_lock_timeout_seconds):
                await topic_lock.acquire()
                acquired = True
        except TimeoutError:
            if progress_msg:
                await progress_msg.edit_text(
                    "⏳ Este tópico ainda está processando uma resposta longa. "
                    "Manda as imagens de novo em alguns segundos."
                )
            logger.warning(
                "Media group flush timed out waiting for topic lock",
                user_id=user_id,
                media_group_id=media_group_id,
            )
            return

        try:
            features = context.bot_data.get("features")
            image_handler = features.get_image_handler() if features else None
            if not image_handler:
                if progress_msg:
                    await progress_msg.edit_text("Photo processing is not available.")
                return

            images = []
            for message in messages:
                processed_image = await image_handler.process_image(
                    message.photo[-1], None
                )
                fmt = processed_image.metadata.get("format", "png")
                images.append(
                    {
                        "data": processed_image.base64_data,
                        "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                    }
                )

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            if progress_msg:
                try:
                    await progress_msg.edit_text(
                        _format_error_message(e), parse_mode="HTML"
                    )
                except Exception as edit_error:
                    logger.debug(
                        "Failed to edit progress msg with error",
                        error=str(edit_error),
                        media_group_id=media_group_id,
                    )
            logger.error(
                "Claude media group processing failed",
                error=str(e),
                user_id=user_id,
                media_group_id=media_group_id,
            )
        finally:
            if acquired:
                topic_lock.release()

    def _reply_context_for_prompt(self, message: Any) -> Optional[str]:
        """Return concise text from the Telegram message this update replies to."""
        reply = getattr(message, "reply_to_message", None)
        if reply is None:
            return None

        raw_text = getattr(reply, "text", None) or getattr(reply, "caption", None)
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if text:
            return text

        sender = getattr(getattr(reply, "from_user", None), "full_name", None)
        message_id = getattr(reply, "message_id", None)
        media_type = next(
            (
                name
                for name in (
                    "voice",
                    "photo",
                    "document",
                    "video",
                    "audio",
                    "animation",
                )
                if (media := getattr(reply, name, None)) is not None
                and media.__class__.__module__ != "unittest.mock"
            ),
            "message",
        )
        details = [f"type={media_type}"]
        if isinstance(sender, str) and sender:
            details.append(f"sender={sender}")
        if isinstance(message_id, (int, str)):
            details.append(f"message_id={message_id}")
        if media_type == "message" and len(details) == 1:
            return None
        return (
            "Replied-to Telegram message has no text/caption ("
            + ", ".join(details)
            + ")."
        )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            reply_context = self._reply_context_for_prompt(update.message)
            processed_voice = await voice_handler.process_voice_message(
                voice,
                update.message.caption,
                **({"reply_context": reply_context} if reply_context else {}),
            )

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        current_dir = self.settings.approved_directory
        chat_id, thread_id, topic_repo, topic_session = await self._load_topic_session(
            update, context
        )
        if topic_repo is None:
            session_id = context.user_data.get("claude_session_id")
            force_new = bool(
                context.user_data.get("force_new_session")
                or (context.user_data.get("_thread_context") and not session_id)
            )
        else:
            if topic_session and not topic_session.is_active:
                await topic_repo.reactivate(chat_id, thread_id)
                topic_session = await topic_repo.get(chat_id, thread_id)
            session_id = topic_session.session_id if topic_session else None
            force_new = await self._should_force_new_claude_session(
                topic_repo, chat_id, thread_id
            )

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=self.settings.approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                chat_id=chat_id,
                message_thread_id=thread_id,
                ask_user_question_bot=context.bot,
                ask_user_question_chat_id=chat_id,
                ask_user_question_thread_id=thread_id,
            )
        finally:
            heartbeat.cancel()

        if force_new:
            context.user_data["force_new_session"] = False

        if topic_repo is None:
            context.user_data["claude_session_id"] = claude_response.session_id
        else:
            await self._upsert_topic_session(
                topic_repo,
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                session_id=claude_response.session_id,
                project_path=self.settings.approved_directory,
            )

        from .handlers.message import _update_working_directory_from_claude_response

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "Voice processing is not available. "
                "Ensure whisper.cpp is installed and the model file exists. "
                "Check WHISPER_CPP_BINARY_PATH and WHISPER_CPP_MODEL_PATH settings."
            )
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "telegram-claude-code-bridge[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Deprecated repo switch command."""
        await update.message.reply_text(
            "⚠️ <b>/repo is deprecated</b>\n\n"
            "This bridge now uses a fixed working directory for every topic: "
            f"<code>{escape_html(str(self.settings.approved_directory))}</code>.\n"
            "Create or switch Telegram forum topics to isolate Claude sessions.",
            parse_mode="HTML",
        )

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        active = self._active_requests.get(target_user_id)
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self.settings.approved_directory
        new_path = base / project_name

        if not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(project_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session in non-thread contexts only. Telegram
        # forum topics must not fall back to the global user+directory session.
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration and not context.user_data.get("_thread_context"):
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(project_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[project_name],
                success=True,
            )
