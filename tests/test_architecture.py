"""Architecture boundaries keep the AstrBot entrypoint small and auditable."""

from __future__ import annotations

import unittest
from pathlib import Path

from astrbot_plugin_mi_fitness_health.features import HealthCommandsMixin
from astrbot_plugin_mi_fitness_health.main import MiFitnessHealthPlugin

from scripts.select_latest_astrbot_4x import select_latest_stable_v4


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
        repository_root = Path(__file__).parents[1]
        guarded_paths = [repository_root / "main.py"]
        for package in (
            "adapters",
            "features",
            "models",
            "services",
            "storage",
            "utils",
        ):
            guarded_paths.extend((repository_root / package).rglob("*.py"))
        forbidden = (
            "astrbot.core.agent.runners",
            "ToolLoopAgentRunner",
            "_iter_llm_responses",
            "_func_tool_for_provider",
        )
        for path in guarded_paths:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                for marker in forbidden:
                    self.assertNotIn(marker, text)

    def test_compatibility_matrix_tracks_minimum_and_latest_stable_4x(self) -> None:
        workflow = (
            Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
        ).read_text(encoding="utf-8")
        self.assertIn('channel: ["minimum-supported", "latest-stable-4x"]', workflow)
        self.assertIn('ref="v4.24.2"', workflow)
        self.assertIn("repos/AstrBotDevs/AstrBot/releases?per_page=100", workflow)
        self.assertIn("select_latest_astrbot_4x.py", workflow)
        self.assertIn("v4.*", workflow)
        self.assertIn("pip install -e ./astrbot-runtime", workflow)
        self.assertIn("check_astrbot_runtime_smoke.py", workflow)

    def test_latest_stable_4x_selector_ignores_v5_and_prereleases(self) -> None:
        releases = [
            {"tag_name": "v5.0.0", "draft": False, "prerelease": False},
            {"tag_name": "v4.30.0-rc.1", "draft": False, "prerelease": True},
            {"tag_name": "v4.29.2", "draft": False, "prerelease": False},
            {"tag_name": "v4.29.1", "draft": False, "prerelease": False},
        ]
        self.assertEqual(select_latest_stable_v4(releases), "v4.29.2")
        with self.assertRaises(ValueError):
            select_latest_stable_v4(
                [{"tag_name": "v5.0.0", "draft": False, "prerelease": False}]
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
