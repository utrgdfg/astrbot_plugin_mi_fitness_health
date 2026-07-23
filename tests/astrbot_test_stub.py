"""Minimal AstrBot API stub for offline modules that only need the logger."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import Mock


def install_logger_stub() -> None:
    """Provide ``astrbot.api.logger`` when tests run outside AstrBot."""
    if "astrbot.api" in sys.modules:
        return
    astrbot_module = ModuleType("astrbot")
    api_module = ModuleType("astrbot.api")
    api_module.logger = Mock()
    astrbot_module.api = api_module
    sys.modules["astrbot"] = astrbot_module
    sys.modules["astrbot.api"] = api_module


install_logger_stub()
