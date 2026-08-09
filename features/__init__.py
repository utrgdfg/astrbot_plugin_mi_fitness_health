"""Conversation features used by the AstrBot plugin entrypoint."""

from .conversation_routing import (
    DEFAULT_CONTEXT_DECISION_PROMPT,
    ConversationRoutingMixin,
)
from .health_commands import HealthCommandsMixin
from .main_model_tooling import MainModelToolingMixin
from .private_health_tool import (
    PRIVATE_HEALTH_CONTEXT_TAG,
    PRIVATE_HEALTH_TOOL_NAME,
    PrivateHealthContextTool,
    add_private_health_tool,
    scrub_private_health_tool_messages,
)
from .proactive_care import (
    DEFAULT_PROACTIVE_CONTEXT_PROMPT,
    DEFAULT_PROACTIVE_DECISION_PROMPT,
    SAFE_CROSS_PROVIDER_STYLE_PROMPT,
    ProactiveCareMixin,
)

__all__ = [
    "ConversationRoutingMixin",
    "DEFAULT_CONTEXT_DECISION_PROMPT",
    "DEFAULT_PROACTIVE_CONTEXT_PROMPT",
    "DEFAULT_PROACTIVE_DECISION_PROMPT",
    "HealthCommandsMixin",
    "MainModelToolingMixin",
    "ProactiveCareMixin",
    "PRIVATE_HEALTH_CONTEXT_TAG",
    "PRIVATE_HEALTH_TOOL_NAME",
    "PrivateHealthContextTool",
    "SAFE_CROSS_PROVIDER_STYLE_PROMPT",
    "add_private_health_tool",
    "scrub_private_health_tool_messages",
]
