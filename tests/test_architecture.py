"""Architecture boundaries keep the AstrBot entrypoint small and auditable."""

from __future__ import annotations

import unittest
from pathlib import Path

from astrbot_plugin_mi_fitness_health.features import HealthCommandsMixin
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
        self.assertEqual(
            MiFitnessHealthPlugin._connection_worker.__module__,
            "astrbot_plugin_mi_fitness_health.features.runtime_coordination",
        )
        self.assertEqual(
            MiFitnessHealthPlugin._health_monitor_loop.__module__,
            "astrbot_plugin_mi_fitness_health.features.runtime_coordination",
        )

    def test_entrypoint_stays_below_seven_hundred_lines(self) -> None:
        entrypoint = Path(__file__).parents[1] / "main.py"
        self.assertLess(len(entrypoint.read_text(encoding="utf-8").splitlines()), 700)

    def test_astrbot_lifecycle_entrypoints_remain_on_concrete_plugin(self) -> None:
        self.assertIn("initialize", MiFitnessHealthPlugin.__dict__)
        self.assertIn("terminate", MiFitnessHealthPlugin.__dict__)

    def test_private_runner_dependency_stays_in_compatibility_boundary(self) -> None:
        feature_root = Path(__file__).parents[1] / "features"
        for path in feature_root.rglob("*.py"):
            with self.subTest(path=path.name):
                self.assertNotIn(
                    "astrbot.core.agent.runners",
                    path.read_text(encoding="utf-8"),
                )

    def test_decorated_command_entrypoints_remain_in_main_module(self) -> None:
        for name in (
            "health_help",
            "health_connection",
            "health_sync",
            "health_today",
            "health_details",
            "health_diagnose",
            "health_status",
            "clear_local_health_data",
            "heart_rate_records",
            "body_data",
            "health_trend",
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(MiFitnessHealthPlugin, name).__module__,
                    "astrbot_plugin_mi_fitness_health.main",
                )
                self.assertEqual(
                    getattr(HealthCommandsMixin, name).__module__,
                    "astrbot_plugin_mi_fitness_health.features.health_commands",
                )


if __name__ == "__main__":
    unittest.main()
