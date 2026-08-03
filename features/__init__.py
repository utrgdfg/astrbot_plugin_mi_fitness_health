"""Conversation features used by the AstrBot plugin entrypoint."""

from .conversation_routing import (
    DEFAULT_CONTEXT_DECISION_PROMPT,
    ConversationRoutingMixin,
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
    "ProactiveCareMixin",
    "SAFE_CROSS_PROVIDER_STYLE_PROMPT",
]
