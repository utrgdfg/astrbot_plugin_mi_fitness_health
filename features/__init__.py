"""Conversation features used by the AstrBot plugin entrypoint."""

from .conversation_routing import (
    DEFAULT_CONTEXT_DECISION_PROMPT,
    ConversationRoutingMixin,
)
from .health_commands import HealthCommandsMixin
from .main_model_tooling import MainModelToolingMixin
from .proactive_care import (
    DEFAULT_PROACTIVE_CONTEXT_PROMPT,
    DEFAULT_PROACTIVE_DECISION_PROMPT,
    SAFE_CROSS_PROVIDER_STYLE_PROMPT,
    ProactiveCareMixin,
)
from .runtime_coordination import RuntimeCoordinationMixin

__all__ = [
    "ConversationRoutingMixin",
    "DEFAULT_CONTEXT_DECISION_PROMPT",
    "DEFAULT_PROACTIVE_CONTEXT_PROMPT",
    "DEFAULT_PROACTIVE_DECISION_PROMPT",
    "HealthCommandsMixin",
    "MainModelToolingMixin",
    "ProactiveCareMixin",
    "RuntimeCoordinationMixin",
    "SAFE_CROSS_PROVIDER_STYLE_PROMPT",
]
