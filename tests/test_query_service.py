"""Offline time-boundary tests for cached cloud data."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from astrbot_plugin_mi_fitness_health.models import BodyMeasurement, DailyActivity
from astrbot_plugin_mi_fitness_health.services.query_service import QueryService
from astrbot_plugin_mi_fitness_health.storage import Database
from astrbot_plugin_mi_fitness_health.utils import local_timestamp, today_text


class _RecordingDatabase:
    def __init__(self):
        self.cutoff = ""

    def heart_rates_since(self, user_id, cutoff, limit=100):
        self.cutoff = cutoff
        return []


class QueryServiceTest(unittest.TestCase):
    def test_heart_rate_cutoff_is_utc(self) -> None:
        """UTC storage must not be lexically compared against +08:00 text."""
        database = _RecordingDatabase()
        service = QueryService(database, "user", "Asia/Shanghai")
        asyncio.run(service.heart_rates(24))
        self.assertTrue(database.cutoff.endswith("+00:00"))

    def test_conversation_snapshot_only_returns_requested_category(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            now = datetime.now(UTC)
            database.upsert_activity(
                "user", DailyActivity(service.today(), 4321, 3000, 210, now)
            )
            database.upsert_measurement("user", BodyMeasurement("weight", now, 60.0))
            snapshot = asyncio.run(service.care_snapshot("我今天走了多少步"))
            self.assertIn("4321 步", snapshot)
            self.assertNotIn("体重", snapshot)

    def test_missing_sleep_does_not_claim_device_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            snapshot = asyncio.run(service.care_snapshot("我昨天睡得怎么样"))
            self.assertIn("暂无已同步记录", snapshot)
            self.assertIn("不代表设备不支持", snapshot)

    def test_display_timestamps_use_configured_user_timezone(self) -> None:
        """UTC storage timestamps must display as local time, not raw +00:00 text."""
        timestamp = "2026-07-22T14:29:00+00:00"
        service = QueryService(_RecordingDatabase(), "user", "Asia/Shanghai")
        self.assertEqual(
            service.display_timestamp(timestamp), "2026-07-22 22:29:00（UTC+08:00）"
        )
        text = today_text(
            {
                "steps": 1,
                "distance_m": 1,
                "active_kcal": 1,
                "collected_at": timestamp,
            },
            [
                {
                    "bpm": 96,
                    "timestamp": timestamp,
                }
            ],
            None,
            service.timezone,
        )
        self.assertIn("活动数据采集时间：2026-07-22 22:29:00（UTC+08:00）", text)
        self.assertIn("心率：最新 96 bpm（数据采集时间：2026-07-22 22:29:00（UTC+08:00）", text)
        self.assertEqual(
            local_timestamp(timestamp, service.timezone),
            "2026-07-22 22:29:00（UTC+08:00）",
        )
