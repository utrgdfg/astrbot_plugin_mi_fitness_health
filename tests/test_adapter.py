"""Offline cloud-adapter tests using fully synthetic, redacted fixture data."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import astrbot_test_stub  # noqa: F401

from astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud import (
    MiFitnessAuthenticationError,
    MiFitnessCloudAdapter,
    MiFitnessResponseError,
    _rc4_crypt,
)
from astrbot_plugin_mi_fitness_health.services import QueryService, SyncService
from astrbot_plugin_mi_fitness_health.storage import Database


class AdapterTest(unittest.TestCase):
    """Verify protocol primitives and cloud parsing without external HTTP."""

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

    def test_non_transient_http_error_is_not_retried(self) -> None:
        class Response:
            status_code = 400
            headers = {}
            text = ""

            def raise_for_status(self):
                raise AssertionError("400 must be classified before raise_for_status")

        class Client:
            calls = 0

            async def post(self, *args, **kwargs):
                self.calls += 1
                return Response()

        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = Client()
        adapter._ssecurity = b"synthetic-security"
        with self.assertRaisesRegex(MiFitnessResponseError, "HTTP 400"):
            asyncio.run(adapter._request("https://example.invalid", "/path", {}))
        self.assertEqual(adapter._client.calls, 1)

    def test_rate_limit_retry_after_is_bounded_and_respected(self) -> None:
        class Response:
            def __init__(self, status_code, retry_after=""):
                self.status_code = status_code
                self.headers = {"Retry-After": retry_after}
                self.text = ""

            def raise_for_status(self):
                raise AssertionError(
                    "the synthetic final 400 must be classified directly"
                )

        class Client:
            def __init__(self):
                self.responses = [Response(429, "2"), Response(400)]

            async def post(self, *args, **kwargs):
                return self.responses.pop(0)

        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = Client()
        adapter._ssecurity = b"synthetic-security"
        sleep = AsyncMock()
        with patch(
            "astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud.asyncio.sleep",
            sleep,
        ):
            with self.assertRaises(MiFitnessResponseError):
                asyncio.run(adapter._request("https://example.invalid", "/path", {}))
        sleep.assert_awaited_once_with(2.0)

    def test_connection_timeout_closes_client_and_session_material(self) -> None:
        class SlowAdapter(MiFitnessCloudAdapter):
            async def _establish_session(self):
                self._cookies = "serviceToken=synthetic"
                self._ssecurity = b"synthetic-security"
                await asyncio.sleep(1)

        adapter = SlowAdapter("user", "token", "cn")
        with patch(
            "astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud.CONNECT_TIMEOUT_SECONDS",
            0.001,
        ):
            self.assertFalse(asyncio.run(adapter.connect()))
        self.assertIsNone(adapter._client)
        self.assertEqual(adapter._cookies, "")
        self.assertEqual(adapter._ssecurity, b"")

    def test_discovery_reports_every_supported_wellness_type(self) -> None:
        """Connection status includes sleep, SpO2, and stress when present."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "heart_rate":
                    return [{"time": 1784692800, "value": {"bpm": 72}}]
                if key == "spo2":
                    return [{"time": 1784692800, "value": {"spo2": 97}}]
                if key in {"steps", "weight", "sleep", "stress"}:
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

    def test_primary_heart_rate_alias_is_used_by_normal_sync(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "heartrate":
                    return [{"time": 1784692800, "value": {"heart_rate": 71}}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                row
                async for row in adapter.iter_heart_rate(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual([record.bpm for record in records], [71])

    def test_invalid_primary_heart_rate_rows_do_not_hide_valid_alias(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "heart_rate":
                    return [{"time": 1784692800, "value": {"unexpected": 71}}]
                if key == "heartrate":
                    return [{"time": 1784692860, "value": {"heart_rate": 73}}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                row
                async for row in adapter.iter_heart_rate(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual([record.bpm for record in records], [73])

    def test_candidate_key_error_is_not_reported_as_empty_success(self) -> None:
        class HeartAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "heart_rate":
                    raise RuntimeError("synthetic heart endpoint failure")
                return []

        class SpO2Adapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "spo2":
                    raise RuntimeError("synthetic spo2 endpoint failure")
                return []

        async def collect_heart():
            return [
                row
                async for row in HeartAdapter("user", "token", "cn").iter_heart_rate(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        async def collect_spo2():
            return [
                row
                async for row in SpO2Adapter("user", "token", "cn").iter_spo2(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        with self.assertRaisesRegex(RuntimeError, "heart endpoint failure"):
            asyncio.run(collect_heart())
        with self.assertRaisesRegex(RuntimeError, "spo2 endpoint failure"):
            asyncio.run(collect_spo2())

    def test_blood_oxygen_alias_is_used_by_normal_sync(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "spo2":
                    raise RuntimeError("unsupported key")
                if key == "blood_oxygen":
                    return [{"time": 1784692800, "value": {"blood_oxygen": 97}}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                row
                async for row in adapter.iter_spo2(datetime.now(UTC), datetime.now(UTC))
            ]

        records = asyncio.run(collect())
        self.assertEqual([record.percent for record in records], [97])

    def test_discovery_reports_alias_only_heart_rate_and_spo2(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "heartrate":
                    return [{"time": 1784692800, "value": {"heart_rate": 72}}]
                if key == "blood_oxygen":
                    return [{"time": 1784692800, "value": {"blood_oxygen": 97}}]
                return []

        available = asyncio.run(
            FixtureAdapter("user", "token", "cn")._discover_data_types()
        )
        self.assertIn("heart_rate", available)
        self.assertIn("spo2", available)

    def test_region_discovery_uses_non_step_data(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if region == "sg" and key == "sleep":
                    return [{"time": 1784692800, "value": {}}]
                return []

        adapter = FixtureAdapter("user", "token")
        self.assertEqual(asyncio.run(adapter._discover_region()), "sg")

    def test_region_discovery_requires_manual_region_when_all_data_is_empty(
        self,
    ) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                return []

        adapter = FixtureAdapter("user", "token")
        with self.assertRaisesRegex(RuntimeError, "手动选择 region"):
            asyncio.run(adapter._discover_region())

    def test_http_date_retry_after_is_supported_and_bounded(self) -> None:
        retry_at = format_datetime(datetime.now(UTC) + timedelta(seconds=5))
        delay = MiFitnessCloudAdapter._retry_after_delay(retry_at, 0)
        self.assertGreaterEqual(delay, 3.0)
        self.assertLessEqual(delay, 6.0)

    def test_activity_deduplicates_same_minute_and_uses_user_timezone(self) -> None:
        base = int(datetime(2026, 1, 1, 16, 30, tzinfo=UTC).timestamp())

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "steps":
                    return [
                        {
                            "time": base,
                            "zone_offset": 0,
                            "value": {"steps": 10, "distance": 8, "calories": 1},
                        },
                        {
                            "time": base + 20,
                            "zone_offset": 0,
                            "value": {"steps": 12, "distance": 9, "calories": 2},
                        },
                        {
                            "time": base + 60,
                            "zone_offset": 0,
                            "value": {"steps": 8, "distance": 6, "calories": 1},
                        },
                    ]
                return [
                    {"time": base, "value": {"calories": 3}},
                    {"time": base + 20, "value": {"calories": 5}},
                    {"time": base + 60, "value": {"calories": 2}},
                ]

        async def collect():
            adapter = FixtureAdapter(
                "user", "token", "cn", timezone(timedelta(hours=8))
            )
            return [
                row
                async for row in adapter.iter_daily_activity(
                    datetime.now(UTC), datetime.now(UTC)
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual(records[0].date, "2026-01-02")
        self.assertEqual(records[0].steps, 20)
        self.assertEqual(records[0].distance_m, 15)
        self.assertEqual(records[0].active_kcal, 7)

    def test_aware_fetch_boundary_is_converted_instead_of_relabelled(self) -> None:
        value = datetime(2026, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(
            MiFitnessCloudAdapter._utc_timestamp(value),
            int(datetime(2026, 1, 1, 0, 0, tzinfo=UTC).timestamp()),
        )
