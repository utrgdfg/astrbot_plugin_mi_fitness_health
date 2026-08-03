"""Architecture boundaries keep the AstrBot entrypoint small and auditable."""

from __future__ import annotations

import unittest
from pathlib import Path

from astrbot_plugin_mi_fitness_health.main import MiFitnessHealthPlugin


class ArchitectureTest(unittest.TestCase):
    def test_conversation_features_live_outside_the_entrypoint(self) -> None:
        self.assertEqual(
            MiFitnessHealthPlugin._decide_context_focus.__module__,
            "astrbot_plugin_mi_fitness_health.features.conversation_routing",
        )
        self.assertEqual(
            MiFitnessHealthPlugin._should_send_proactive_care.__module__,
            "astrbot_plugin_mi_fitness_health.features.proactive_care",
        )

    def test_entrypoint_stays_below_one_thousand_lines(self) -> None:
        entrypoint = Path(__file__).parents[1] / "main.py"
        self.assertLess(len(entrypoint.read_text(encoding="utf-8").splitlines()), 1000)


if __name__ == "__main__":
    unittest.main()
