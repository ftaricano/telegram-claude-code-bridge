"""Manages session-scoped /goal state with Stop hook evaluation loop."""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Tuple, cast

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)
from claude_agent_sdk.types import HookCallback

from ..storage.models import Goal
from ..storage.repositories import GoalRepository

logger = structlog.get_logger()


class GoalManager:
    """Coordinates topic-scoped /goal state and Stop hook evaluation."""

    def __init__(
        self,
        repo: GoalRepository,
        *,
        evaluator_model: str = "claude-haiku-4-5",
        evaluator_timeout_seconds: float = 30.0,
    ):
        self.repo = repo
        self.evaluator_model = evaluator_model
        self.evaluator_timeout_seconds = evaluator_timeout_seconds

    async def set_goal(self, chat_id: int, thread_id: int, condition: str) -> Goal:
        """Set a new active goal for a topic."""
        condition = condition.strip()
        if not condition:
            raise ValueError("Goal condition is required")
        if len(condition) > 4000:
            raise ValueError("Goal condition too long (max 4000 chars)")
        return await self.repo.set_active(chat_id, thread_id, condition)

    async def get_status(self, chat_id: int, thread_id: int) -> Goal | None:
        """Return the active goal for a topic."""
        return await self.repo.get_active(chat_id, thread_id)

    async def clear(self, chat_id: int, thread_id: int) -> Goal | None:
        """Clear an active goal without marking it achieved."""
        return await self.repo.clear_active(chat_id, thread_id)

    def build_stop_hook(self, chat_id: int, thread_id: int) -> HookCallback:
        """Return the Stop hook callback used by claude-agent-sdk."""

        async def hook(
            hook_input: Any,
            _tool_use_id: str | None = None,
            _context: Any = None,
        ) -> dict[str, Any]:
            goal = await self.repo.get_active(chat_id, thread_id)
            if goal is None:
                return {}

            transcript_path = ""
            if isinstance(hook_input, dict):
                transcript_path = str(hook_input.get("transcript_path") or "")
            transcript_tail = await self._read_transcript_tail(transcript_path)

            try:
                yes, reason = await asyncio.wait_for(
                    self._evaluate_goal(goal, transcript_tail),
                    timeout=self.evaluator_timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "Goal evaluator failed",
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    error=str(exc),
                )
                yes = False
                reason = "evaluator error; continue working toward the goal"

            await self.repo.increment_turn(
                chat_id, thread_id, tokens=0, last_reason=reason
            )

            if yes:
                await self.repo.mark_achieved(chat_id, thread_id)
                return {}

            return {"decision": "block", "reason": reason}

        return cast(HookCallback, hook)

    async def _read_transcript_tail(self, transcript_path: str) -> str:
        """Read the latest transcript characters for evaluation."""
        if not transcript_path:
            return ""
        path = Path(transcript_path)
        if not path.exists() or not path.is_file():
            return ""
        try:
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "Failed to read goal transcript", path=str(path), error=str(exc)
            )
            return ""
        return text[-4000:]

    async def _evaluate_goal(
        self, goal: Goal, transcript_tail: str
    ) -> Tuple[bool, str]:
        """Evaluate the active goal using a small Claude model via OAuth."""
        os.environ.pop("ANTHROPIC_API_KEY", None)
        system_prompt = (
            "You are evaluating whether a goal has been met based on the "
            "conversation transcript.\n"
            f"Goal: {goal.condition}\n"
            'Read the recent conversation and return JSON: {"yes": true/false, '
            '"reason": "<one short sentence>"}\n'
            "Be strict: only return yes if there's clear evidence the condition "
            "is satisfied in the transcript."
        )
        options = ClaudeAgentOptions(
            model=self.evaluator_model,
            max_turns=1,
            allowed_tools=[],
            disallowed_tools=[],
            system_prompt=system_prompt,
            setting_sources=["project", "user"],
        )

        messages: list[Any] = []
        client = ClaudeSDKClient(options)
        try:
            await client.connect()
            await client.query(transcript_tail or "(empty transcript)")
            if client._query is None:
                return False, "evaluator failed to start"
            async for message in client._query.receive_messages():
                messages.append(message)
                if isinstance(message, ResultMessage):
                    break
        finally:
            await client.disconnect()

        text = self._extract_evaluator_text(messages)
        return self.parse_evaluator_response(text)

    @staticmethod
    def _extract_evaluator_text(messages: list[Any]) -> str:
        """Extract evaluator output text from SDK messages."""
        for message in reversed(messages):
            if isinstance(message, ResultMessage) and getattr(message, "result", None):
                return str(message.result)
        parts: list[str] = []
        for message in messages:
            if not isinstance(message, AssistantMessage):
                continue
            content = getattr(message, "content", []) or []
            for block in content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                elif hasattr(block, "text"):
                    parts.append(str(block.text))
        return "\n".join(parts)

    @staticmethod
    def parse_evaluator_response(text: str) -> Tuple[bool, str]:
        """Parse evaluator JSON, tolerating fenced markdown output."""
        cleaned = text.strip()
        fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, flags=re.DOTALL)
        if fence:
            cleaned = fence.group(1).strip()
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
            if not match:
                return False, "evaluator returned unparseable output"
            data = json.loads(match.group(0))

        yes = bool(data.get("yes"))
        reason = str(data.get("reason") or "").strip()
        if not reason:
            reason = "goal met" if yes else "goal not met yet"
        return yes, reason[:500]
