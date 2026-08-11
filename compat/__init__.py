"""Narrow compatibility adapters for supported AstrBot runtime versions."""

from .runner_privacy_guard import (
    install_private_context_guard,
    isolate_private_context_request,
)

__all__ = [
    "install_private_context_guard",
    "isolate_private_context_request",
]
