"""Offline proactive-monitor tests; no platform or Xiaomi request is used."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import astrbot_test_stub  # noqa: F401

from astrbot_plugin_mi_fitness_health.services import HealthMonitorService
from astrbot_plugin_mi_fitness_health.storage import Database


class MonitorServiceTest(unittest.TestCase):
    def test_recent_private_activity_triggers_once_during_late_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            local_zone = timezone(timedelta(hours=8))
            now = datetime(2026, 7, 23, 1, 30, tzinfo=local_zone)
            database.touch_private_owner_session(
                "owner",
                "qq:FriendMessage:123",
                now.astimezone(UTC) - timedelta(minutes=10),
            )
            service = HealthMonitorService(
                database, "owner", local_zone, True, "00:30", "06:00", 45, 120
            )

            finding = asyncio.run(service.evaluate_late_activity(now))
            self.assertIsNotNone(finding)
            self.assertIn("私聊活动", finding.message)
            asyncio.run(service.mark_sent(finding, now))
            asyncio.run(service.mark_proactive_sent(finding.message, now))
            self.assertTrue(
                asyncio.run(service.proactive_cooling_down(now + timedelta(minutes=30)))
            )
            self.assertIsNone(
                asyncio.run(service.evaluate_late_activity(now + timedelta(minutes=30)))
            )

    def test_missing_recent_activity_never_guesses_user_is_awake(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            local_zone = timezone(timedelta(hours=8))
            service = HealthMonitorService(
                database, "owner", local_zone, True, "00:30", "06:00", 45, 120
            )
            now = datetime(2026, 7, 23, 2, 0, tzinfo=local_zone)
            self.assertIsNone(asyncio.run(service.evaluate_late_activity(now)))
