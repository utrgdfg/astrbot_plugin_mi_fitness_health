"""Offline adapter and alert tests using fully synthetic, redacted fixture data."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import astrbot_test_stub  # noqa: F401

from astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud import (
    MiFitnessAuthenticationError,
    MiFitnessCloudAdapter,
    _rc4_crypt,
)
from astrbot_plugin_mi_fitness_health.models import HeartRateSample, SpO2Sample
from astrbot_plugin_mi_fitness_health.services import (
    AlertService,
    QueryService,
    SyncService,
)
from astrbot_plugin_mi_fitness_health.storage import Database


class AdapterAndAlertTest(unittest.TestCase):
    """Verify protocol primitives and alert safety without external HTTP."""

    def test_rc4_round_trip_and_fixture_parse(self) -> None:
        """RC4 round-trips and accepts a redacted fixture payload."""
        key, value = b"test-key", b"fixture payload"
        self.assertEqual(_rc4_crypt(key, _rc4_crypt(key, value)), value)
        item = json.loads(
            (Path(__file__).parent / "fixtures" / "heart_rate.json").read_text(
                encoding="utf-8"
            )
        )[0]
        self.assertEqual(MiFitnessCloudAdapter._value(item)["bpm"], 72)
        self.assertIsNotNone(MiFitnessCloudAdapter._record_time(item))

    def test_workout_record_does_not_complete_passive_alert(self) -> None:
        """Workout records reset a sequence and cannot cause a passive alert."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            for index, workout in enumerate((False, True, False)):
                database.upsert_heart_rate(
                    "user",
                    HeartRateSample(
                        f"hr{index}",
                        now + timedelta(minutes=index),
                        150,
                        "passive",
                        workout,
                    ),
                )
            alerts = asyncio.run(
                AlertService(database, "user", 120, 0, 2, 10).evaluate()
            )
            self.assertEqual(alerts, [])

    def test_fresh_consecutive_alert_is_marked_only_after_delivery(self) -> None:
        """One exact sequence retries before send and never repeats after mark_sent."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            database.upsert_heart_rate(
                "user",
                HeartRateSample(
                    "hr1", now - timedelta(minutes=2), 130, "passive", False
                ),
            )
            database.upsert_heart_rate(
                "user",
                HeartRateSample(
                    "hr2", now - timedelta(minutes=1), 132, "passive", False
                ),
            )
            service = AlertService(database, "user", 120, 0, 2, 120)

            first = asyncio.run(service.evaluate())
            self.assertEqual(len(first), 1)
            self.assertEqual(len(asyncio.run(service.evaluate())), 1)
            asyncio.run(service.mark_sent(first[0]))
            self.assertEqual(asyncio.run(service.evaluate()), [])

    def test_spo2_requires_consecutive_fresh_samples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            database.upsert_spo2(
                "user", SpO2Sample("o1", now - timedelta(minutes=2), 92)
            )
            database.upsert_spo2(
                "user", SpO2Sample("o2", now - timedelta(minutes=1), 93)
            )
            service = AlertService(database, "user", 0, 0, 2, 120, spo2_low=95)
            findings = asyncio.run(service.evaluate())
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].alert_type, "spo2_low")

    def test_resting_heart_rate_fallback_is_queryable(self) -> None:
        """Resting-heart-rate data remains available when the sampled key is empty."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "resting_heart_rate":
                    return [
                        {
                            "time": 1784692800000,
                            "zone_offset": 28800,
                            "value": '{"heart_rate":68}',
                        }
                    ]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                record
                async for record in adapter.iter_heart_rate(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].bpm, 68)
        self.assertEqual(records[0].sample_type, "passive")

    def test_resting_heart_rate_survives_standard_key_error(self) -> None:
        """One account-specific key failure does not hide a working fallback."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "heart_rate":
                    raise RuntimeError("unsupported key")
                return [
                    {
                        "time": 1784692800000,
                        "zone_offset": 28800,
                        "value": '{"heart_rate":68}',
                    }
                ]

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                record
                async for record in adapter.iter_heart_rate(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].bpm, 68)

    def test_daily_activity_uses_dedicated_calorie_total(self) -> None:
        """Dedicated calorie records replace, rather than duplicate, step calories."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "steps":
                    return [
                        {
                            "time": 1743467400,
                            "zone_offset": 0,
                            "value": '{"steps":10,"distance":8,"calories":1}',
                        },
                        {
                            "time": 1743467460,
                            "zone_offset": 0,
                            "value": '{"steps":20,"distance":16,"calories":2}',
                        },
                    ]
                return [
                    {"time": 1743467400, "zone_offset": 0, "value": '{"calories":5}'},
                    {"time": 1743467460, "zone_offset": 0, "value": '{"calories":7}'},
                ]

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                record
                async for record in adapter.iter_daily_activity(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].steps, 30)
        self.assertEqual(records[0].distance_m, 24)
        self.assertEqual(records[0].active_kcal, 12)

    def test_daily_activity_survives_optional_calorie_key_error(self) -> None:
        """A working steps key remains usable when the calorie key fails."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "calories":
                    raise RuntimeError("unsupported key")
                return [
                    {
                        "time": 1743467400,
                        "zone_offset": 0,
                        "value": '{"steps":10,"distance":8,"calories":3}',
                    }
                ]

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                record
                async for record in adapter.iter_daily_activity(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].active_kcal, 3)

    def test_daily_activity_does_not_replace_steps_when_step_key_fails(self) -> None:
        """A failed required steps request must not produce a zero-step update."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "steps":
                    raise RuntimeError("steps unavailable")
                return [
                    {"time": 1743467400, "zone_offset": 0, "value": '{"calories":7}'}
                ]

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                record
                async for record in adapter.iter_daily_activity(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        with self.assertRaisesRegex(RuntimeError, "steps unavailable"):
            asyncio.run(collect())

    def test_repeated_pagination_cursor_keeps_unique_records(self) -> None:
        """A malformed cloud cursor cannot discard an otherwise usable first page."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            calls = 0

            async def _request(self, host, path, payload):
                self.calls += 1
                value = (
                    '{"bedtime":1784664000,"wake_up_time":1784692800}'
                    if self.calls == 1
                    else {
                        "bedtime": 1784664000,
                        "wake_up_time": 1784692800,
                    }
                )
                return {
                    "data_list": [
                        {
                            "time": 1784692800,
                            "value": value,
                        }
                    ],
                    "has_more": True,
                    "next_key": "same",
                }

        adapter = FixtureAdapter("user", "token", "cn")
        records = asyncio.run(
            adapter._fetch_key("sleep", datetime.now(UTC), datetime.now(UTC), "cn")
        )
        self.assertEqual(len(records), 1)

    def test_zero_sleep_and_stress_scores_are_preserved(self) -> None:
        """Valid zero scores must not be treated as missing by truthiness fallbacks."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                now = int(datetime.now(UTC).timestamp())
                if key == "sleep":
                    return [
                        {
                            "time": now,
                            "value": {
                                "bedtime": now - 8 * 60 * 60,
                                "wake_up_time": now,
                                "score": 0,
                            },
                        }
                    ]
                if key == "stress":
                    return [{"time": now, "value": {"stress": 0}}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            now = datetime.now(UTC)
            sleeps = [row async for row in adapter.iter_sleep(now, now)]
            stress = [row async for row in adapter.iter_stress(now, now)]
            return sleeps, stress

        sleeps, stress = asyncio.run(collect())
        self.assertEqual(sleeps[0].score, 0)
        self.assertEqual(stress[0].score, 0)

    def test_repeated_steps_cursor_rejects_partial_daily_total(self) -> None:
        """A partial steps page must never overwrite a complete cached daily total."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _request(self, host, path, payload):
                return {
                    "data_list": [{"time": 1784692800, "value": '{"steps":12}'}],
                    "has_more": True,
                    "next_key": "same",
                }

        adapter = FixtureAdapter("user", "token", "cn")
        with self.assertRaisesRegex(RuntimeError, "不完整的每日汇总"):
            asyncio.run(
                adapter._fetch_key("steps", datetime.now(UTC), datetime.now(UTC), "cn")
            )

    def test_sleep_flows_from_cloud_parser_to_conversation_snapshot(self) -> None:
        """Sleep survives parsing, synchronization, SQLite, and natural-language query."""

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            query_service = QueryService(database, "user", "Asia/Shanghai")
            wake_local = datetime.now(query_service.timezone).replace(
                hour=7, minute=30, second=0, microsecond=0
            ) - timedelta(days=1)
            wake = int(wake_local.astimezone(UTC).timestamp())
            bedtime = wake - 7 * 60 * 60 - 30 * 60

            class FixtureAdapter(MiFitnessCloudAdapter):
                def is_connected(self):
                    return True

                async def _fetch_key(self, key, start, end, region):
                    if key != "sleep":
                        return []
                    return [
                        {
                            "time": wake,
                            "value": json.dumps(
                                {
                                    "bedtime": bedtime,
                                    "wake_up_time": wake,
                                    "awake_duration": 20,
                                    "sleep_score": 82,
                                }
                            ),
                        }
                    ]

            adapter = FixtureAdapter("user", "token", "cn")
            result = asyncio.run(SyncService(adapter, database, "user").sync(1))
            self.assertEqual(result["details"]["sleep"]["fetched"], 1)
            snapshot = asyncio.run(query_service.care_snapshot("我昨天睡得怎么样"))
            self.assertIn("睡眠 430 分钟", snapshot)
            self.assertIn("评分 82", snapshot)
            self.assertIn(wake_local.date().isoformat(), snapshot)
            self.assertIn("结束 07:30", snapshot)

    def test_sync_propagates_authentication_failure(self) -> None:
        """An expired connected session reaches the monitor pause logic."""

        class FixtureAdapter:
            def is_connected(self):
                return True

            async def connect(self):
                return True

            async def iter_daily_activity(self, start, end):
                raise MiFitnessAuthenticationError("凭证已失效")
                yield

            async def empty(self, start, end):
                if False:
                    yield

            iter_heart_rate = empty
            iter_body_measurements = empty
            iter_sleep = empty
            iter_spo2 = empty
            iter_stress = empty

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = SyncService(FixtureAdapter(), database, "user")
            with self.assertRaises(MiFitnessAuthenticationError):
                asyncio.run(service.sync(1))

    def test_sync_propagates_initial_login_authentication_failure(self) -> None:
        """An initial login rejection also reaches the monitor pause logic."""

        class FixtureAdapter:
            last_error = "凭证已失效"
            authentication_failed = True

            def is_connected(self):
                return False

            async def connect(self):
                return False

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = SyncService(FixtureAdapter(), database, "user")
            with self.assertRaises(MiFitnessAuthenticationError):
                asyncio.run(service.sync(1))

    def test_login_http_401_is_classified_as_authentication_failure(self) -> None:
        """The account endpoint's 401 must not be downgraded to a temporary error."""

        class Response:
            status_code = 401

            def raise_for_status(self):
                raise AssertionError("401 should be classified before raise_for_status")

        class Client:
            async def get(self, *args, **kwargs):
                return Response()

        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = Client()
        with self.assertRaises(MiFitnessAuthenticationError):
            asyncio.run(adapter._login_with_token())

    def test_discovery_reports_every_supported_wellness_type(self) -> None:
        """Connection status includes sleep, SpO2, and stress when present."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key in {"steps", "heart_rate", "weight", "sleep", "spo2", "stress"}:
                    return [{"time": 1784692800000, "value": "{}"}]
                return []

        adapter = FixtureAdapter("user", "token", "cn")
        available = asyncio.run(adapter._discover_data_types())
        self.assertEqual(
            available,
            [
                "daily_activity",
                "heart_rate",
                "body_measurements",
                "sleep",
                "spo2",
                "stress",
            ],
        )

    def test_old_heart_rate_cannot_complete_fresh_sequence(self) -> None:
        """Every record in a consecutive alert sequence must still be fresh."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            database.upsert_heart_rate(
                "user",
                HeartRateSample("old", now - timedelta(hours=4), 132, "passive", False),
            )
            database.upsert_heart_rate(
                "user",
                HeartRateSample(
                    "fresh", now - timedelta(minutes=1), 130, "passive", False
                ),
            )
            findings = asyncio.run(
                AlertService(
                    database, "user", 120, 0, 2, 120, data_max_age_minutes=60
                ).evaluate()
            )
            self.assertEqual(findings, [])

    def test_old_spo2_cannot_complete_fresh_sequence(self) -> None:
        """Metric alerts also reject an older matching sample in the sequence."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            database.upsert_spo2(
                "user", SpO2Sample("old", now - timedelta(hours=4), 92)
            )
            database.upsert_spo2(
                "user", SpO2Sample("fresh", now - timedelta(minutes=1), 93)
            )
            service = AlertService(
                database, "user", 0, 0, 2, 120, spo2_low=95, data_max_age_minutes=60
            )
            self.assertEqual(asyncio.run(service.evaluate()), [])
