"""Offline proactive-monitor tests; no platform or Xiaomi request is used."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import astrbot_test_stub  # noqa: F401
from astrbot_plugin_mi_fitness_health.services import (
    HealthMonitorService,
    MonitorFinding,
)
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

    def test_future_private_activity_is_not_treated_as_recent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            local_zone = timezone(timedelta(hours=8))
            now = datetime(2026, 7, 23, 2, 0, tzinfo=local_zone)
            database.touch_private_owner_session(
                "owner",
                "qq:FriendMessage:123",
                now.astimezone(UTC) + timedelta(minutes=10),
            )
            service = HealthMonitorService(
                database, "owner", local_zone, True, "00:30", "06:00", 45, 120
            )

            self.assertIsNone(asyncio.run(service.evaluate_late_activity(now)))

    def test_delivery_audit_does_not_store_candidate_or_generated_text(self) -> None:
        database = Mock()
        service = HealthMonitorService(
            database, "owner", UTC, True, "00:30", "06:00", 45, 120
        )
        finding = MonitorFinding(
            "late_night_activity",
            "2026-08-07",
            "所有者在 01:30 仍有私聊活动",
        )
        sent_at = datetime(2026, 8, 7, 1, 35, tzinfo=UTC)

        asyncio.run(service.mark_sent(finding, sent_at, delivery_confirmed=False))
        asyncio.run(
            service.mark_proactive_sent(
                "今晚早点休息吧", sent_at, delivery_confirmed=False
            )
        )
        asyncio.run(service.confirm_sent(finding, sent_at))
        asyncio.run(service.confirm_proactive_sent(sent_at))

        calls = database.add_alert.call_args_list
        self.assertEqual(calls[0].args[2], "深夜活跃关心发送结果待确认")
        self.assertEqual(calls[1].args[2], "主动关心发送结果待确认")
        confirmations = database.confirm_alert_delivery.call_args_list
        self.assertEqual(confirmations[0].args[2], "已发送深夜活跃关心")
        self.assertEqual(confirmations[1].args[2], "已发送主动关心")
        self.assertNotIn("01:30", str(calls))
        self.assertNotIn("早点休息", str(calls))
        self.assertNotIn("01:30", str(confirmations))
        self.assertNotIn("早点休息", str(confirmations))
