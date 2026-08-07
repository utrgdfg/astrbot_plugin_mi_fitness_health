"""Offline time-boundary tests for cached cloud data."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import astrbot_test_stub  # noqa: F401
from astrbot_plugin_mi_fitness_health.models import (
    BodyMeasurement,
    DailyActivity,
    HeartRateSample,
    SleepSession,
)
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

    def test_focus_maps_to_only_required_sync_datasets(self) -> None:
        service = QueryService(_RecordingDatabase(), "user", "Asia/Shanghai")
        self.assertEqual(
            service.sync_types_for_focus("昨天睡眠和心率"),
            ("heart_rate", "sleep"),
        )
        self.assertEqual(
            service.sync_types_for_focus("步"),
            ("daily_activity",),
        )
        self.assertEqual(
            set(service.sync_types_for_focus("综合概况")),
            {
                "daily_activity",
                "heart_rate",
                "body_measurements",
                "sleep",
                "spo2",
                "stress",
            },
        )

    def test_llm_focus_fails_closed_for_empty_and_unknown_text(self) -> None:
        service = QueryService(_RecordingDatabase(), "user", "Asia/Shanghai")

        for focus in (
            "",
            "   ",
            "overall health overview",
            "something unknown",
            "我刚同步完",
        ):
            with self.subTest(focus=focus):
                self.assertEqual(service.llm_categories_for_focus(focus), ())
                self.assertEqual(service.normalize_llm_focus(focus), "")
                self.assertEqual(service.llm_sync_types_for_focus(focus), ())
                self.assertEqual(
                    asyncio.run(service.llm_care_snapshot(focus)),
                    "",
                )

    def test_llm_focus_limits_ordinary_requests_to_first_two_categories(self) -> None:
        service = QueryService(_RecordingDatabase(), "user", "Asia/Shanghai")

        self.assertEqual(
            service.llm_categories_for_focus("昨天睡眠、心率和步数"),
            ("sleep", "heart"),
        )
        self.assertEqual(
            service.normalize_llm_focus("昨天睡眠、心率和步数"),
            "昨天 睡眠 心率",
        )
        self.assertEqual(
            service.llm_sync_types_for_focus("昨天睡眠、心率和步数"),
            ("sleep", "heart_rate"),
        )
        self.assertEqual(
            service.llm_categories_for_focus("身体数据"),
            ("body",),
        )

    def test_llm_focus_allows_all_only_for_explicit_chinese_comprehensive_intent(
        self,
    ) -> None:
        service = QueryService(_RecordingDatabase(), "user", "Asia/Shanghai")

        self.assertEqual(
            service.llm_categories_for_focus("请给我今天的综合概况"),
            tuple(service.CATEGORY_SYNC_TYPES),
        )
        self.assertEqual(
            set(service.llm_sync_types_for_focus("请给我今天的综合概况")),
            set(service.CATEGORY_SYNC_TYPES.values()),
        )
        self.assertEqual(
            service.llm_categories_for_focus("health"),
            (),
        )

    def test_existing_care_snapshot_keeps_generic_all_category_behavior(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            now = datetime.now(UTC)
            database.upsert_activity(
                "user", DailyActivity(service.today(), 4321, 3000, 210, now)
            )
            database.upsert_measurement("user", BodyMeasurement("weight", now, 60.0))

            snapshot = asyncio.run(service.care_snapshot("未指定类别"))

            self.assertIn("4321 步", snapshot)
            self.assertIn("体重", snapshot)
            self.assertEqual(
                set(service.sync_types_for_focus("未指定类别")),
                set(service.CATEGORY_SYNC_TYPES.values()),
            )

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

    def test_yesterday_sleep_excludes_sessions_ending_today(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            local_now = datetime.now(service.timezone)
            yesterday = local_now.date() - timedelta(days=1)
            yesterday_end = datetime.combine(
                yesterday,
                datetime.min.time(),
                tzinfo=service.timezone,
            ) + timedelta(hours=8)
            today_end = yesterday_end + timedelta(days=1)
            database.upsert_sleep(
                "user",
                SleepSession(
                    "yesterday",
                    (yesterday_end - timedelta(minutes=450)).astimezone(UTC),
                    yesterday_end.astimezone(UTC),
                    450,
                    420,
                    30,
                    88,
                ),
            )
            database.upsert_sleep(
                "user",
                SleepSession(
                    "today",
                    (today_end - timedelta(minutes=130)).astimezone(UTC),
                    today_end.astimezone(UTC),
                    130,
                    111,
                    19,
                    70,
                ),
            )

            snapshot = asyncio.run(service.care_snapshot("昨天睡眠"))
            self.assertIn("420 分钟", snapshot)
            self.assertNotIn("111 分钟", snapshot)

    def test_today_sleep_does_not_present_yesterday_as_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            local_now = datetime.now(service.timezone)
            yesterday_end = datetime.combine(
                local_now.date() - timedelta(days=1),
                datetime.min.time(),
                tzinfo=service.timezone,
            ) + timedelta(hours=8)
            database.upsert_sleep(
                "user",
                SleepSession(
                    "yesterday-only",
                    (yesterday_end - timedelta(minutes=450)).astimezone(UTC),
                    yesterday_end.astimezone(UTC),
                    450,
                    420,
                    30,
                    88,
                ),
            )

            snapshot = asyncio.run(service.care_snapshot("今天 睡眠 心率"))

            self.assertIn("今日睡眠", snapshot)
            self.assertIn("尚未出现以今天起床时间结束", snapshot)
            self.assertIn(yesterday_end.date().isoformat(), snapshot)
            self.assertIn("仅供历史参考", snapshot)
            self.assertIn("不能作为今天刚醒后的状态", snapshot)

    def test_conversation_snapshot_silently_omits_missing_today_sleep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            local_now = datetime.now(service.timezone)
            yesterday_end = local_now - timedelta(days=1)
            database.upsert_sleep(
                "user",
                SleepSession(
                    "yesterday-only",
                    (yesterday_end - timedelta(minutes=450)).astimezone(UTC),
                    yesterday_end.astimezone(UTC),
                    450,
                    420,
                    30,
                    88,
                ),
            )

            snapshot = asyncio.run(
                service.care_snapshot(
                    "今天 睡眠",
                    include_missing_notice=False,
                )
            )

            self.assertEqual(snapshot, "")

    def test_conversation_snapshot_keeps_today_metrics_while_omitting_old_sleep(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            now = datetime.now(UTC)
            yesterday_end = now - timedelta(days=1)
            database.upsert_sleep(
                "user",
                SleepSession(
                    "yesterday-only",
                    yesterday_end - timedelta(minutes=450),
                    yesterday_end,
                    450,
                    420,
                    30,
                    88,
                ),
            )
            database.upsert_heart_rate(
                "user",
                HeartRateSample("today-heart", now, 72, "passive", False),
            )

            snapshot = asyncio.run(
                service.care_snapshot(
                    "今天 睡眠 心率",
                    include_missing_notice=False,
                )
            )

            self.assertIn("今日心率", snapshot)
            self.assertIn("72 bpm", snapshot)
            self.assertNotIn("睡眠", snapshot)
            self.assertNotIn("历史参考", snapshot)

    def test_today_sleep_uses_record_ending_today_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            local_now = datetime.now(service.timezone)
            today_end = local_now.replace(second=0, microsecond=0)
            database.upsert_sleep(
                "user",
                SleepSession(
                    "today",
                    (today_end - timedelta(minutes=450)).astimezone(UTC),
                    today_end.astimezone(UTC),
                    450,
                    420,
                    30,
                    88,
                ),
            )

            snapshot = asyncio.run(service.care_snapshot("今天 睡眠"))

            self.assertIn(today_end.date().isoformat(), snapshot)
            self.assertIn("420 分钟", snapshot)
            self.assertNotIn("仅供历史参考", snapshot)

    def test_naive_legacy_sleep_timestamp_is_interpreted_as_utc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            end_utc = (datetime.now(UTC) - timedelta(hours=1)).replace(
                second=0, microsecond=0
            )
            start_utc = end_utc - timedelta(hours=7)
            database.upsert_sleep(
                "user",
                SleepSession(
                    "legacy-naive",
                    start_utc.replace(tzinfo=None),
                    end_utc.replace(tzinfo=None),
                    420,
                    390,
                    30,
                    80,
                ),
            )

            snapshot = asyncio.run(service.care_snapshot("睡眠"))
            expected_start = start_utc.astimezone(service.timezone)
            expected = end_utc.astimezone(service.timezone)

            self.assertIn(expected.date().isoformat(), snapshot)
            self.assertIn(f"入睡 {expected_start.strftime('%Y-%m-%d %H:%M')}", snapshot)
            self.assertIn(f"起床 {expected.strftime('%Y-%m-%d %H:%M')}", snapshot)

    def test_sleep_snapshot_keeps_cross_midnight_start_and_wake_dates(self) -> None:
        service = QueryService(_RecordingDatabase(), "user", "Asia/Shanghai")

        formatted = service._format_sleep_row(
            {
                "start_at": "2026-08-06T15:30:00+00:00",
                "end_at": "2026-08-06T23:30:00+00:00",
                "asleep_minutes": 450,
                "score": 88,
            }
        )

        self.assertIsNotNone(formatted)
        self.assertIn("入睡 2026-08-06 23:30", formatted)
        self.assertIn("起床 2026-08-07 07:30", formatted)

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
        self.assertIn(
            "今日心率（本地自然日）：最新 96 bpm（数据采集时间：2026-07-22 22:29:00（UTC+08:00）",
            text,
        )
        self.assertEqual(
            local_timestamp(timestamp, service.timezone),
            "2026-07-22 22:29:00（UTC+08:00）",
        )

    def test_local_day_heart_rate_query_keeps_all_samples_and_boundaries(self) -> None:
        """Today's range is local midnight-to-midnight, never a 100-row window."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            local_day = date(2026, 7, 22)

            # UTC 16:00 is local midnight in Asia/Shanghai.  The first and
            # last records below belong to adjacent local dates and must not
            # affect the 22 July average/range.
            database.upsert_heart_rate(
                "user",
                HeartRateSample(
                    "previous-day",
                    datetime(2026, 7, 21, 15, 59, tzinfo=UTC),
                    20,
                    "passive",
                    False,
                ),
            )
            database.upsert_heart_rate(
                "user",
                HeartRateSample(
                    "next-day",
                    datetime(2026, 7, 22, 16, 0, tzinfo=UTC),
                    200,
                    "passive",
                    False,
                ),
            )
            samples = [51, 135] + [78] * 118
            start = datetime(2026, 7, 21, 16, 0, tzinfo=UTC)
            for index, bpm in enumerate(samples):
                database.upsert_heart_rate(
                    "user",
                    HeartRateSample(
                        f"today-{index}",
                        start + timedelta(minutes=index),
                        bpm,
                        "passive",
                        False,
                    ),
                )

            rows = asyncio.run(service.heart_rates_for_local_day(local_day))
            values = [row["bpm"] for row in rows]
            self.assertEqual(len(rows), 120)
            self.assertEqual((min(values), max(values)), (51, 135))
            self.assertEqual(round(sum(values) / len(values)), 78)

    def test_recent_heart_rate_snapshot_keeps_all_48_hour_samples(self) -> None:
        """The advertised 48-hour range must not be truncated to 100 rows."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = QueryService(database, "user", "Asia/Shanghai")
            start = datetime.now(UTC) - timedelta(hours=2)
            samples = [51, 135] + [78] * 118
            for index, bpm in enumerate(samples):
                database.upsert_heart_rate(
                    "user",
                    HeartRateSample(
                        f"recent-{index}",
                        start + timedelta(seconds=index),
                        bpm,
                        "passive",
                        False,
                    ),
                )

            snapshot = asyncio.run(service.care_snapshot("最近心率"))
            self.assertIn("最近 48 小时心率", snapshot)
            self.assertIn("最高 135", snapshot)
            self.assertIn("最低 51", snapshot)
