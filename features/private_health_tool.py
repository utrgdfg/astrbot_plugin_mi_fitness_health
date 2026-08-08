"""Ephemeral main-agent tool for owner-only Mi Fitness context."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from astrbot.api import logger
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import Message, TextPart
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext

PRIVATE_HEALTH_TOOL_NAME = "load_owner_mi_fitness_context"
PRIVATE_HEALTH_CONTEXT_TAG = '<private_life_context source="mi_fitness">'

SAFE_TOOL_RESULT_LOADED = (
    "Verified private life context was loaded temporarily for this response. "
    "Use it only when relevant, keep the current persona, and do not mention the tool."
)
SAFE_TOOL_RESULT_UNAVAILABLE = (
    "No verified private life context is currently available. "
    "Continue the conversation naturally without mentioning data, syncing, or the tool."
)

PrivateContextLoader = Callable[
    [ContextWrapper[AstrAgentContext]], Awaitable[str | None]
]


class PrivateHealthContextTool(FunctionTool[AstrAgentContext]):
    """Let the current main model request private context without logging its values."""

    def __init__(self, loader: PrivateContextLoader, routing_prompt: str) -> None:
        bounded_prompt = " ".join(str(routing_prompt or "").split())[:2400]
        description = (
            "Load verified Xiaomi Mi Fitness life data for the configured owner. "
            "Call this zero-argument tool before replying whenever the full conversation "
            "concerns the owner's sleep, schedule, tiredness, energy, exercise, activity, "
            "recovery, heart rate, weight, body composition, blood oxygen, or stress and "
            "the data could verify, clarify, or gently correct the reply. Understand short "
            "answers from context: if you asked whether the owner napped and they answer "
            "that they did, call the tool even without a metric keyword. Also call it for "
            "direct questions about the owner's data. Do not call it for unrelated chat, "
            "third-party situations, writing/code tasks, or medical emergencies. The tool "
            "may return no data; in that case continue naturally and do not mention the "
            "plugin, cache, cloud, synchronization, or missing data. This tool has no "
            "arguments: never invent parameters. If additional guidance mentions a data "
            "category or time scope, simply call this tool and let the plugin derive them "
            "locally from the conversation."
        )
        if bounded_prompt:
            description += f" Additional routing guidance: {bounded_prompt}"
        super().__init__(
            name=PRIVATE_HEALTH_TOOL_NAME,
            description=description,
            parameters={"type": "object", "properties": {}},
        )
        self._loader = loader

    async def call(
        self, context: ContextWrapper[AstrAgentContext], **kwargs: Any
    ) -> ToolExecResult:
        """Load one bounded snapshot into a provider-only temporary message."""
        del kwargs
        if any(_has_private_context_part(message) for message in context.messages):
            return SAFE_TOOL_RESULT_LOADED
        try:
            text = await self._loader(context)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "[小米运动健康] 主模型请求生活数据时暂时无法准备上下文（%s）",
                type(error).__name__,
            )
            return SAFE_TOOL_RESULT_UNAVAILABLE
        if not text:
            return SAFE_TOOL_RESULT_UNAVAILABLE

        part = TextPart(text=text)
        if not hasattr(part, "mark_as_temp"):
            return SAFE_TOOL_RESULT_UNAVAILABLE
        temporary_part = part.mark_as_temp()
        for message in reversed(context.messages):
            if getattr(message, "role", None) != "user":
                continue
            content = getattr(message, "content", None)
            if isinstance(content, str):
                message.content = [TextPart(text=content), temporary_part]
                return SAFE_TOOL_RESULT_LOADED
            if isinstance(content, list):
                content.append(temporary_part)
                return SAFE_TOOL_RESULT_LOADED

        # A normal main-agent turn always contains a user message. Keep a
        # fail-safe temporary message for unusual runners that omit it.
        fallback = Message(role="user", content=[temporary_part])
        fallback._no_save = True
        context.messages.append(fallback)
        return SAFE_TOOL_RESULT_LOADED


def add_private_health_tool(
    req: ProviderRequest,
    loader: PrivateContextLoader,
    routing_prompt: str,
) -> None:
    """Expose the private tool only on the already-authorized LLM request."""
    if req.func_tool is None:
        req.func_tool = ToolSet()
    req.func_tool.add_tool(PrivateHealthContextTool(loader, routing_prompt))


def _tool_call_name(tool_call: object) -> str:
    """Read a tool-call name from AstrBot model objects or serialized dictionaries."""
    if isinstance(tool_call, dict):
        function = tool_call.get("function")
        return str(function.get("name") or "") if isinstance(function, dict) else ""
    function = getattr(tool_call, "function", None)
    return str(getattr(function, "name", "") or "")


def _tool_call_id(tool_call: object) -> str:
    """Read a tool-call ID without assuming one provider representation."""
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "")
    return str(getattr(tool_call, "id", "") or "")


def _has_private_context_part(message: object) -> bool:
    """Identify a temporary context content block created by this plugin."""
    content = getattr(message, "content", None)
    if not isinstance(content, list):
        return False
    return any(
        isinstance(getattr(part, "text", None), str)
        and bool(getattr(part, "_no_save", False))
        and getattr(part, "text").startswith(PRIVATE_HEALTH_CONTEXT_TAG)
        for part in content
    )


def scrub_private_health_tool_messages(messages: list[Message]) -> None:
    """Remove this tool's temporary context and call artifacts before persistence."""
    private_call_ids: set[str] = set()
    cleaned: list[Message] = []

    for message in messages:
        content = getattr(message, "content", None)
        if isinstance(content, list):
            message.content = [
                part
                for part in content
                if not (
                    isinstance(getattr(part, "text", None), str)
                    and bool(getattr(part, "_no_save", False))
                    and getattr(part, "text").startswith(PRIVATE_HEALTH_CONTEXT_TAG)
                )
            ]
            if not message.content and getattr(message, "_no_save", False):
                continue
        tool_calls = getattr(message, "tool_calls", None)
        if getattr(message, "role", None) != "assistant" or not tool_calls:
            cleaned.append(message)
            continue

        remaining_calls = []
        removed_count = 0
        for tool_call in tool_calls:
            if _tool_call_name(tool_call) == PRIVATE_HEALTH_TOOL_NAME:
                removed_count += 1
                if call_id := _tool_call_id(tool_call):
                    private_call_ids.add(call_id)
            else:
                remaining_calls.append(tool_call)

        if removed_count == 0:
            cleaned.append(message)
        elif remaining_calls:
            message.tool_calls = remaining_calls
            cleaned.append(message)
        # A tool-planning assistant message containing only this private tool is
        # deliberately omitted. The final persona reply remains untouched.

    messages[:] = [
        message
        for message in cleaned
        if not (
            getattr(message, "role", None) == "tool"
            and str(getattr(message, "tool_call_id", "") or "") in private_call_ids
        )
    ]
