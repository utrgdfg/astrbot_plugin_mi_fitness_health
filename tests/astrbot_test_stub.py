"""Minimal AstrBot API stub for offline modules that only need the logger."""

from __future__ import annotations

import sys
import tempfile
from enum import Enum
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock


def install_logger_stub() -> None:
    """Provide the small AstrBot surface imported by offline plugin tests."""
    if "astrbot.api" in sys.modules:
        return
    astrbot_module = ModuleType("astrbot")
    api_module = ModuleType("astrbot.api")
    api_module.logger = Mock()
    api_module.AstrBotConfig = dict

    event_module = ModuleType("astrbot.api.event")

    class AstrMessageEvent:
        pass

    class MessageChain:
        def message(self, text):
            return text

    class _EventMessageType:
        PRIVATE_MESSAGE = "private"

    class _Filter:
        EventMessageType = _EventMessageType

        @staticmethod
        def _decorator(*args, **kwargs):
            def decorate(function):
                return function

            return decorate

        command = _decorator
        llm_tool = _decorator
        on_agent_done = _decorator
        on_llm_request = _decorator
        event_message_type = _decorator

    event_module.AstrMessageEvent = AstrMessageEvent
    event_module.MessageChain = MessageChain
    event_module.filter = _Filter

    platform_module = ModuleType("astrbot.api.platform")

    class MessageType(Enum):
        FRIEND_MESSAGE = "FriendMessage"

    platform_module.MessageType = MessageType

    star_module = ModuleType("astrbot.api.star")

    class Context:
        pass

    class Star:
        def __init__(self, context):
            self.context = context
            self.name = "astrbot_plugin_mi_fitness_health"

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            return Path(tempfile.gettempdir()) / name

    star_module.Context = Context
    star_module.Star = Star
    star_module.StarTools = StarTools

    provider_module = ModuleType("astrbot.api.provider")

    class ProviderRequest:
        def __init__(self):
            self.extra_user_content_parts = []
            self.contexts = []
            self.conversation = None
            self.func_tool = None

    provider_module.ProviderRequest = ProviderRequest

    core_module = ModuleType("astrbot.core")
    agent_module = ModuleType("astrbot.core.agent")
    message_module = ModuleType("astrbot.core.agent.message")
    run_context_module = ModuleType("astrbot.core.agent.run_context")
    runners_module = ModuleType("astrbot.core.agent.runners")
    tool_loop_runner_module = ModuleType(
        "astrbot.core.agent.runners.tool_loop_agent_runner"
    )
    tool_module = ModuleType("astrbot.core.agent.tool")
    astr_agent_context_module = ModuleType("astrbot.core.astr_agent_context")

    class TextPart:
        def __init__(self, text):
            self.text = text
            self._no_save = False

        def mark_as_temp(self):
            self._no_save = True
            return self

    class Message:
        def __init__(self, role, content=None, tool_calls=None, tool_call_id=None):
            self.role = role
            self.content = content
            self.tool_calls = tool_calls
            self.tool_call_id = tool_call_id
            self._no_save = False

    class ContextWrapper:
        def __init__(self, context=None, messages=None):
            self.context = context
            self.messages = list(messages or [])

        @classmethod
        def __class_getitem__(cls, item):
            del item
            return cls

    class FunctionTool:
        def __init__(
            self,
            name,
            description,
            parameters,
            handler=None,
            active=True,
            is_background_task=False,
        ):
            self.name = name
            self.description = description
            self.parameters = parameters
            self.handler = handler
            self.active = active
            self.is_background_task = is_background_task

        @classmethod
        def __class_getitem__(cls, item):
            del item
            return cls

    class ToolSet:
        def __init__(self, tools=None):
            self.tools = list(tools or [])

        def empty(self):
            return not self.tools

        def add_tool(self, tool):
            self.tools = [item for item in self.tools if item.name != tool.name]
            self.tools.append(tool)

        def get_tool(self, name):
            return next((item for item in self.tools if item.name == name), None)

        def get_light_tool_set(self):
            return ToolSet(self.tools)

        def get_param_only_tool_set(self):
            return ToolSet(self.tools)

        def __bool__(self):
            return bool(self.tools)

    class AstrAgentContext:
        pass

    class ToolLoopAgentRunner:
        def _func_tool_for_provider(self):
            if not self.req.func_tool:
                return None
            modalities = self.provider.provider_config.get("modalities", None)
            if isinstance(modalities, list) and "tool_use" not in modalities:
                return None
            return self.req.func_tool

        async def _iter_llm_responses(self, *args, **kwargs):
            del args, kwargs
            for response in getattr(self, "_test_responses", []):
                observed = getattr(self, "_test_seen_func_tools", None)
                if observed is not None:
                    observed.append(self._func_tool_for_provider())
                yield response

    ToolLoopAgentRunner._iter_llm_responses.__module__ = (
        "astrbot.core.agent.runners.tool_loop_agent_runner"
    )
    ToolLoopAgentRunner._func_tool_for_provider.__module__ = (
        "astrbot.core.agent.runners.tool_loop_agent_runner"
    )

    message_module.Message = Message
    message_module.TextPart = TextPart
    run_context_module.ContextWrapper = ContextWrapper
    tool_module.FunctionTool = FunctionTool
    tool_module.ToolExecResult = str
    tool_module.ToolSet = ToolSet
    tool_loop_runner_module.ToolLoopAgentRunner = ToolLoopAgentRunner
    astr_agent_context_module.AstrAgentContext = AstrAgentContext

    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module
    sys.modules["astrbot.api.event"] = event_module
    sys.modules["astrbot.api.platform"] = platform_module
    sys.modules["astrbot.api.star"] = star_module
    sys.modules["astrbot.api.provider"] = provider_module
    sys.modules["astrbot.core"] = core_module
    sys.modules["astrbot.core.agent"] = agent_module
    sys.modules["astrbot.core.agent.message"] = message_module
    sys.modules["astrbot.core.agent.run_context"] = run_context_module
    sys.modules["astrbot.core.agent.runners"] = runners_module
    sys.modules["astrbot.core.agent.runners.tool_loop_agent_runner"] = (
        tool_loop_runner_module
    )
    sys.modules["astrbot.core.agent.tool"] = tool_module
    sys.modules["astrbot.core.astr_agent_context"] = astr_agent_context_module


install_logger_stub()
