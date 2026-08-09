"""Make the bundled tests runnable after cloning or arbitrary ZIP extraction."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

PACKAGE_NAME = "astrbot_plugin_mi_fitness_health"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TESTS_ROOT = REPOSITORY_ROOT / "tests"

sys.path.insert(0, str(TESTS_ROOT))
import astrbot_test_stub  # noqa: E402, F401

if REPOSITORY_ROOT.name == PACKAGE_NAME:
    sys.path.insert(0, str(REPOSITORY_ROOT.parent))
elif PACKAGE_NAME not in sys.modules:
    package = ModuleType(PACKAGE_NAME)
    package.__file__ = str(REPOSITORY_ROOT)
    package.__package__ = PACKAGE_NAME
    package.__path__ = [str(REPOSITORY_ROOT)]
    sys.modules[PACKAGE_NAME] = package
