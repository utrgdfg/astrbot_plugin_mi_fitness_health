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

    provider_module.ProviderRequest = ProviderRequest

    core_module = ModuleType("astrbot.core")
    agent_module = ModuleType("astrbot.core.agent")
    message_module = ModuleType("astrbot.core.agent.message")

    class TextPart:
        def __init__(self, text):
            self.text = text

        def mark_as_temp(self):
            return self

    message_module.TextPart = TextPart

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


install_logger_stub()
