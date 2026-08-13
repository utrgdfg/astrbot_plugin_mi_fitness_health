"""Offline cloud-adapter tests using fully synthetic, redacted fixture data."""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import astrbot_test_stub  # noqa: F401
import httpx
from astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud import (
    LOGIN_PREFIX,
    MiFitnessAuthenticationError,
    MiFitnessBudgetError,
    MiFitnessCloudAdapter,
    MiFitnessRateLimitError,
    MiFitnessResponseError,
    _OperationBudget,
    _rc4_crypt,
)
from astrbot_plugin_mi_fitness_health.services import QueryService, SyncService
from astrbot_plugin_mi_fitness_health.storage import Database


def _http_response(
    status_code: int,
    *,
    content: bytes = b"",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a fully bound response suitable for raise_for_status and streaming."""
    return httpx.Response(
        status_code,
        content=content,
        headers=headers,
        request=httpx.Request("GET", "https://example.invalid"),
    )


def _range_around(timestamp: int, seconds: int = 120) -> tuple[datetime, datetime]:
    center = datetime.fromtimestamp(timestamp, UTC)
    return center - timedelta(seconds=seconds), center + timedelta(seconds=seconds)


class _StreamContext:
    """Minimal async context manager returned by AsyncClient.stream."""

    def __init__(self, response: httpx.Response):
        self.response = response

    async def __aenter__(self) -> httpx.Response:
        return self.response

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.response.aclose()


class _StreamingClient:
    """Small deterministic stream-only HTTP client used by adapter unit tests."""

    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses)
        self.calls = 0
        self.closed = False

    def stream(self, *args, **kwargs) -> _StreamContext:
        self.calls += 1
        return _StreamContext(self.responses.pop(0))

    async def aclose(self) -> None:
        self.closed = True


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

    def test_extreme_cloud_timestamps_are_safely_rejected(self) -> None:
        unreasonable_future = int((datetime.now(UTC) + timedelta(days=365)).timestamp())
        for value in (-1, 10**100, unreasonable_future, "not-a-timestamp"):
            with self.subTest(value=value):
                self.assertIsNone(
                    MiFitnessCloudAdapter._record_time(
                        {"time": value, "zone_offset": 0}
                    )
                )

    def test_malformed_sleep_timestamps_skip_only_bad_records(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                now = int(datetime.now(UTC).timestamp())
                return [
                    {
                        "time": now,
                        "value": {"bedtime": -1, "wake_up_time": now},
                    },
                    {
                        "time": now,
                        "value": {"bedtime": 10**100, "wake_up_time": now},
                    },
                    {
                        "time": now,
                        "value": {
                            "bedtime": now - 8 * 60 * 60,
                            "wake_up_time": now,
                            "score": 88,
                        },
                    },
                ]

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            now = datetime.now(UTC)
            return [
                row async for row in adapter.iter_sleep(now - timedelta(days=1), now)
            ]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].score, 88)

    def test_resting_heart_rate_fallback_is_queryable(self) -> None:
        """Resting-heart-rate data remains available when the sampled key is empty."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
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
            start, end = _range_around(1784692800)
            return [record async for record in adapter.iter_heart_rate(start, end)]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].bpm, 68)
        self.assertEqual(records[0].sample_type, "passive")

    def test_resting_heart_rate_survives_standard_key_error(self) -> None:
        """One account-specific key failure does not hide a working fallback."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "heart_rate":
                    raise MiFitnessResponseError("unsupported key")
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
            start, end = _range_around(1784692800)
            return [record async for record in adapter.iter_heart_rate(start, end)]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].bpm, 68)

    def test_heart_rate_identity_uses_source_and_timestamp_not_bpm(self) -> None:
        timestamp = 1784692800

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "heart_rate":
                    return [
                        {"time": timestamp, "value": {"bpm": 70}},
                        {"time": timestamp, "value": {"bpm": 72}},
                    ]
                if key == "resting_heart_rate":
                    return [{"time": timestamp, "value": {"bpm": 60}}]
                return []

        async def collect():
            start, end = _range_around(timestamp)
            return [
                row
                async for row in FixtureAdapter("user", "token", "cn").iter_heart_rate(
                    start, end
                )
            ]

        records = asyncio.run(collect())
        self.assertEqual([record.bpm for record in records], [72, 60])
        self.assertEqual(
            [record.record_id for record in records],
            [
                f"mi_fitness_hr_{timestamp}",
                f"mi_fitness_resting_hr_{timestamp}",
            ],
        )

    def test_all_heart_rate_alias_response_errors_fail_the_dataset(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                raise MiFitnessResponseError(f"unsupported {key}")

        async def collect():
            now = datetime.now(UTC)
            return [
                row
                async for row in FixtureAdapter("user", "token", "cn").iter_heart_rate(
                    now - timedelta(minutes=1), now
                )
            ]

        with self.assertRaises(MiFitnessResponseError):
            asyncio.run(collect())

    def test_heart_rate_boolish_fields_do_not_use_python_truthiness(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        samples = adapter._parse_heart_rate_records(
            [
                {
                    "time": 1784692800,
                    "value": {
                        "bpm": 70,
                        "type": False,
                        "workout_id": "0",
                        "is_workout": "false",
                    },
                },
                {
                    "time": 1784692860,
                    "value": {
                        "bpm": 71,
                        "type": "false",
                        "workout_id": 0,
                        "is_workout": False,
                    },
                },
                {
                    "time": 1784692920,
                    "value": {
                        "bpm": 72,
                        "type": "1",
                        "workout_id": "0",
                        "is_workout": "true",
                    },
                },
                {
                    "time": 1784692980,
                    "value": {
                        "bpm": 73,
                        "type": "0",
                        "workout_id": "550e8400-e29b-41d4-a716-446655440000",
                        "is_workout": "false",
                    },
                },
                {
                    "time": 1784693040,
                    "value": {
                        "bpm": 74,
                        "type": "0",
                        "is_workout": "opaque-but-not-boolish",
                    },
                },
            ],
            is_resting=False,
        )

        self.assertEqual(
            [(row.sample_type, row.is_workout) for row in samples],
            [
                ("passive", False),
                ("passive", False),
                ("active", True),
                ("passive", True),
                ("passive", False),
            ],
        )

    def test_nested_measurement_times_take_priority_and_invalid_values_fallback(
        self,
    ) -> None:
        timestamp = 1784692800
        outside = timestamp + 3600

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "resting_heart_rate":
                    return [
                        {
                            "time": outside,
                            "value": {"bpm": 60, "date_time": timestamp},
                        },
                        {
                            "time": timestamp + 10,
                            "value": {"bpm": 61, "date_time": "invalid"},
                        },
                    ]
                if key == "spo2":
                    return [
                        {"time": outside, "value": {"spo2": 97, "time": timestamp}},
                        {
                            "time": timestamp + 10,
                            "value": {"spo2": 98, "time": "invalid"},
                        },
                    ]
                if key == "stress":
                    return [
                        {"time": outside, "value": {"stress": 20, "time": timestamp}},
                        {
                            "time": timestamp + 10,
                            "value": {"stress": 21, "time": "invalid"},
                        },
                    ]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            start, end = _range_around(timestamp, 30)
            heart = [row async for row in adapter.iter_heart_rate(start, end)]
            spo2 = [row async for row in adapter.iter_spo2(start, end)]
            stress = [row async for row in adapter.iter_stress(start, end)]
            return heart, spo2, stress

        groups = asyncio.run(collect())
        for rows in groups:
            self.assertEqual(
                [int(row.timestamp.timestamp()) for row in rows],
                [timestamp, timestamp + 10],
            )

    def test_non_sleep_iterators_reject_records_outside_requested_range(self) -> None:
        timestamp = 1784692800
        record_times = (timestamp - 60, timestamp, timestamp + 60)

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                values = {
                    "steps": lambda: {"steps": 10},
                    "heart_rate": lambda: {"bpm": 70},
                    "weight": lambda: {"weight": 65},
                    "spo2": lambda: {"spo2": 97},
                    "stress": lambda: {"stress": 20},
                }
                if key not in values:
                    return []
                return [
                    {"time": record_time, "value": values[key]()}
                    for record_time in record_times
                ]

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            start, end = _range_around(timestamp, 10)
            return (
                [row async for row in adapter.iter_daily_activity(start, end)],
                [row async for row in adapter.iter_heart_rate(start, end)],
                [row async for row in adapter.iter_body_measurements(start, end)],
                [row async for row in adapter.iter_spo2(start, end)],
                [row async for row in adapter.iter_stress(start, end)],
            )

        groups = asyncio.run(collect())
        self.assertTrue(all(len(rows) == 1 for rows in groups))
        for rows in groups[1:]:
            self.assertEqual(int(rows[0].timestamp.timestamp()), timestamp)

    def test_non_sleep_future_record_is_rejected_even_inside_requested_range(
        self,
    ) -> None:
        future = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
        future_timestamp = int(future.timestamp())

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "heart_rate":
                    return [{"time": future_timestamp, "value": {"bpm": 70}}]
                return []

        async def collect():
            start, end = _range_around(future_timestamp, 10)
            return [
                row
                async for row in FixtureAdapter("user", "token", "cn").iter_heart_rate(
                    start, end
                )
            ]

        self.assertEqual(asyncio.run(collect()), [])

    def test_daily_activity_uses_dedicated_calorie_total(self) -> None:
        """Dedicated calorie records replace, rather than duplicate, step calories."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
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
            start, end = _range_around(1743467400)
            return [record async for record in adapter.iter_daily_activity(start, end)]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].steps, 30)
        self.assertEqual(records[0].distance_m, 24)
        self.assertEqual(records[0].active_kcal, 12)

    def test_daily_activity_survives_optional_calorie_key_error(self) -> None:
        """A working steps key remains usable when the calorie key fails."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
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
            start, end = _range_around(1743467400)
            return [record async for record in adapter.iter_daily_activity(start, end)]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].active_kcal, 3)

    def test_daily_activity_does_not_replace_steps_when_step_key_fails(self) -> None:
        """A failed required steps request must not produce a zero-step update."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
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

    def test_daily_activity_ignores_calorie_only_day_when_steps_are_empty(
        self,
    ) -> None:
        """An optional calorie row cannot create a destructive zero-step day."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "steps":
                    return []
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

        self.assertEqual(asyncio.run(collect()), [])

    def test_repeated_pagination_cursor_rejects_partial_sleep_records(self) -> None:
        """A malformed cloud cursor cannot return a partial non-activity dataset."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            calls = 0

            async def _request(self, host, path, payload, *, budget=None):
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
        with self.assertRaisesRegex(RuntimeError, "分页游标重复"):
            asyncio.run(
                adapter._fetch_key("sleep", datetime.now(UTC), datetime.now(UTC), "cn")
            )

    def test_fetch_key_tolerates_null_duplicate_and_out_of_order_rows(self) -> None:
        early = 1784692800
        late = early + 60

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_page(
                self, key, start, end, region, cursor=None, *, budget=None
            ):
                del key, start, end, region, cursor, budget
                return {
                    "data_list": [
                        None,
                        "not-a-record",
                        {},
                        {"time": late, "value": {"bpm": 72}},
                        {"time": early, "value": None},
                        {"time": late, "value": {"bpm": 72}},
                        {"time": early, "value": {"bpm": 68}},
                    ],
                    "has_more": False,
                }

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            start, end = _range_around(early, 120)
            records = await adapter._fetch_key("heart_rate", start, end, "cn")
            samples = adapter._parse_heart_rate_records(
                records,
                is_resting=False,
                start=start,
                end=end,
            )
            return records, samples

        records, samples = asyncio.run(collect())
        self.assertEqual(len(records), 4)
        self.assertEqual({sample.bpm for sample in samples}, {68, 72})
        self.assertEqual(len({sample.record_id for sample in samples}), 2)

    def test_successful_pagination_forwards_cursor_and_deduplicates_pages(self) -> None:
        first = {"time": 1784692800, "value": {"bpm": 68}}
        duplicate = {"time": 1784692860, "value": {"bpm": 70}}
        last = {"time": 1784692920, "value": {"bpm": 72}}

        class FixtureAdapter(MiFitnessCloudAdapter):
            def __init__(self):
                super().__init__("user", "token", "cn")
                self.cursors = []

            async def _fetch_page(
                self, key, start, end, region, cursor=None, *, budget=None
            ):
                del key, start, end, region, budget
                self.cursors.append(cursor)
                if cursor is None:
                    return {
                        "data_list": [first, duplicate],
                        "has_more": True,
                        "next_key": "second-page",
                    }
                return {
                    "data_list": [duplicate, last],
                    "has_more": False,
                }

        adapter = FixtureAdapter()
        records = asyncio.run(
            adapter._fetch_key(
                "heart_rate",
                datetime.now(UTC),
                datetime.now(UTC),
                "cn",
            )
        )

        self.assertEqual(adapter.cursors, [None, "second-page"])
        self.assertEqual(records, [first, duplicate, last])

    def test_non_activity_pagination_page_limit_rejects_partial_records(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            calls = 0

            async def _fetch_page(
                self, key, start, end, region, cursor=None, *, budget=None
            ):
                self.calls += 1
                return {
                    "data_list": [
                        {"time": 1784692800 + self.calls, "value": {"bpm": 70}}
                    ],
                    "has_more": True,
                    "next_key": f"cursor-{self.calls}",
                }

        adapter = FixtureAdapter("user", "token", "cn")
        with self.assertRaisesRegex(RuntimeError, "分页超过安全上限"):
            asyncio.run(
                adapter._fetch_key(
                    "heart_rate", datetime.now(UTC), datetime.now(UTC), "cn"
                )
            )
        self.assertEqual(adapter.calls, 100)

    def test_has_more_without_valid_cursor_rejects_partial_records(self) -> None:
        """A declared next page must never be silently treated as complete."""

        for key in ("steps", "sleep"):
            for next_key in (None, "", 123):
                with self.subTest(key=key, next_key=next_key):

                    class FixtureAdapter(MiFitnessCloudAdapter):
                        async def _fetch_page(
                            self,
                            data_key,
                            start,
                            end,
                            region,
                            cursor=None,
                            *,
                            budget=None,
                        ):
                            return {
                                "data_list": [
                                    {"time": 1784692800, "value": {"steps": 12}}
                                ],
                                "has_more": True,
                                "next_key": next_key,
                            }

                    adapter = FixtureAdapter("user", "token", "cn")
                    with self.assertRaisesRegex(RuntimeError, "缺少有效游标"):
                        asyncio.run(
                            adapter._fetch_key(
                                key,
                                datetime.now(UTC),
                                datetime.now(UTC),
                                "cn",
                            )
                        )

    def test_zero_sleep_and_stress_scores_are_preserved(self) -> None:
        """Valid zero scores must not be treated as missing by truthiness fallbacks."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
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
            now = datetime.now(UTC).replace(microsecond=0)
            sleeps = [row async for row in adapter.iter_sleep(now, now)]
            stress = [row async for row in adapter.iter_stress(now, now)]
            return sleeps, stress

        sleeps, stress = asyncio.run(collect())
        self.assertEqual(sleeps[0].score, 0)
        self.assertEqual(stress[0].score, 0)

    def test_repeated_steps_cursor_rejects_partial_daily_total(self) -> None:
        """A partial steps page must never overwrite a complete cached daily total."""

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _request(self, host, path, payload, *, budget=None):
                return {
                    "data_list": [{"time": 1784692800, "value": '{"steps":12}'}],
                    "has_more": True,
                    "next_key": "same",
                }

        adapter = FixtureAdapter("user", "token", "cn")
        with self.assertRaisesRegex(RuntimeError, "不完整的同步结果"):
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

                async def _fetch_key(self, key, start, end, region, *, budget=None):
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
            self.assertIn("睡眠时长 430 分钟", snapshot)
            self.assertIn("评分 82", snapshot)
            self.assertIn(wake_local.date().isoformat(), snapshot)
            self.assertIn(
                f"入睡 {wake_local.replace(hour=0, minute=0).strftime('%Y-%m-%d %H:%M')}",
                snapshot,
            )
            self.assertIn(f"起床 {wake_local.strftime('%Y-%m-%d %H:%M')}", snapshot)

    def test_sleep_fetches_later_daily_index_but_rejects_future_session(self) -> None:
        """Today's completed sleep may be indexed later without admitting future data."""
        requested_end = datetime(2026, 8, 1, 7, 0, tzinfo=UTC)
        requested_start = requested_end - timedelta(days=3)
        completed_wake = requested_end - timedelta(minutes=15)
        future_wake = requested_end + timedelta(hours=2)
        stale_wake = requested_start - timedelta(minutes=1)
        observed_end: list[datetime] = []

        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                observed_end.append(end)
                return [
                    {
                        "time": int((requested_end + timedelta(hours=12)).timestamp()),
                        "value": {
                            "bedtime": int(
                                (completed_wake - timedelta(hours=7)).timestamp()
                            ),
                            "wake_up_time": int(completed_wake.timestamp()),
                        },
                    },
                    {
                        "time": int((requested_end + timedelta(hours=14)).timestamp()),
                        "value": {
                            "bedtime": int(
                                (future_wake - timedelta(hours=7)).timestamp()
                            ),
                            "wake_up_time": int(future_wake.timestamp()),
                        },
                    },
                    {
                        "time": int(stale_wake.timestamp()),
                        "value": {
                            "bedtime": int(
                                (stale_wake - timedelta(hours=7)).timestamp()
                            ),
                            "wake_up_time": int(stale_wake.timestamp()),
                        },
                    },
                ]

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [
                record
                async for record in adapter.iter_sleep(requested_start, requested_end)
            ]

        records = asyncio.run(collect())
        self.assertEqual(observed_end, [requested_end + timedelta(days=1)])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].end_at, completed_wake)

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
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(_http_response(401))
        with self.assertRaises(MiFitnessAuthenticationError):
            asyncio.run(adapter._login_with_token())

    def test_login_http_403_is_classified_as_authentication_failure(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(_http_response(403))
        with self.assertRaises(MiFitnessAuthenticationError):
            asyncio.run(adapter._login_with_token())

    def test_login_rejects_malformed_prefixed_json_as_authentication_failure(
        self,
    ) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(200, content=LOGIN_PREFIX + b"{not-json")
        )
        with self.assertRaises(MiFitnessAuthenticationError):
            asyncio.run(adapter._login_with_token())

    def test_login_rate_limit_sets_cooldown_and_is_not_swallowed(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(429, headers={"Retry-After": "120"})
        )
        with self.assertRaises(MiFitnessRateLimitError) as raised:
            asyncio.run(adapter._login_with_token())
        self.assertGreaterEqual(raised.exception.retry_after_seconds, 119)
        self.assertEqual(adapter._client.calls, 1)

    def test_login_rejects_a_different_returned_account(self) -> None:
        login_payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://hlth.io.mi.com/session",
            "userId": "different-user",
            "passToken": "replacement-token",
        }
        adapter = MiFitnessCloudAdapter("configured-user", "original-token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(login_payload).encode(),
            )
        )

        with self.assertRaisesRegex(MiFitnessAuthenticationError, "账号与配置"):
            asyncio.run(adapter._login_with_token())

        self.assertEqual(adapter.user_id, "configured-user")
        self.assertEqual(adapter.pass_token, "original-token")

    def test_login_rejects_untrusted_redirect_variants(self) -> None:
        for location in (
            "http://hlth.io.mi.com/session",
            "https://hlth.io.mi.com:444/session",
            "https://user@hlth.io.mi.com/session",
            "https://hlth.io.mi.com.example.invalid/session",
        ):
            with self.subTest(location=location):
                payload = {
                    "ssecurity": base64.b64encode(b"s" * 16).decode(),
                    "location": location,
                }
                adapter = MiFitnessCloudAdapter("user", "token", "cn")
                adapter._client = _StreamingClient(
                    _http_response(
                        200,
                        content=LOGIN_PREFIX + json.dumps(payload).encode(),
                    )
                )

                with self.assertRaisesRegex(
                    MiFitnessAuthenticationError, "HTTPS.*受信任域"
                ):
                    asyncio.run(adapter._login_with_token())
                self.assertEqual(adapter._client.calls, 1)

    def test_login_keeps_returned_token_uncommitted_until_session_validation(
        self,
    ) -> None:
        payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://api.io.mi.com/session",
            "userId": "user",
            "passToken": "replacement-token",
        }
        adapter = MiFitnessCloudAdapter("user", "original-token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(payload).encode(),
            ),
            _http_response(
                200,
                headers={"Set-Cookie": "serviceToken=synthetic; Secure; HttpOnly"},
            ),
        )

        candidate = asyncio.run(adapter._login_with_token())

        self.assertEqual(candidate, ("user", "replacement-token"))
        self.assertEqual(adapter.pass_token, "original-token")
        self.assertEqual(adapter._client.calls, 2)

    def test_login_forwards_only_whitelisted_session_cookies(self) -> None:
        payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://api.io.mi.com/session",
            "userId": "user",
        }
        session_headers = httpx.Headers(
            [
                ("Set-Cookie", "unknownCookie=ignored; Secure"),
                ("Set-Cookie", "serviceToken=service; Secure; HttpOnly"),
                ("Set-Cookie", "cUserId=cloud-user; Secure"),
                ("Set-Cookie", "userId=user; Secure"),
                ("Set-Cookie", "yetAnotherServiceToken=alternate; Secure"),
            ]
        )
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(payload).encode(),
            ),
            _http_response(200, headers=session_headers),
        )

        asyncio.run(adapter._login_with_token())

        self.assertEqual(
            adapter._cookies,
            "serviceToken=service; cUserId=cloud-user; userId=user; "
            "yetAnotherServiceToken=alternate",
        )
        self.assertNotIn("unknownCookie", adapter._cookies)

    def test_login_accepts_matching_session_user_when_payload_omits_user_id(
        self,
    ) -> None:
        payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://api.io.mi.com/session",
        }
        session_headers = httpx.Headers(
            [
                ("Set-Cookie", "serviceToken=service; Secure; HttpOnly"),
                ("Set-Cookie", "userId=user; Secure"),
            ]
        )
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(payload).encode(),
            ),
            _http_response(200, headers=session_headers),
        )

        candidate = asyncio.run(adapter._login_with_token())

        self.assertEqual(candidate, ("user", "token"))

    def test_login_rejects_unbound_account_when_payload_and_cookie_omit_user_id(
        self,
    ) -> None:
        payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://api.io.mi.com/session",
        }
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(payload).encode(),
            ),
            _http_response(
                200,
                headers={"Set-Cookie": "serviceToken=service; Secure; HttpOnly"},
            ),
        )

        with self.assertRaisesRegex(MiFitnessAuthenticationError, "确认配置账号"):
            asyncio.run(adapter._login_with_token())

    def test_login_rejects_conflicting_session_user_id(self) -> None:
        payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://api.io.mi.com/session",
            "userId": "user",
        }
        session_headers = httpx.Headers(
            [
                ("Set-Cookie", "serviceToken=service; Secure; HttpOnly"),
                ("Set-Cookie", "userId=different-user; Secure"),
            ]
        )
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(payload).encode(),
            ),
            _http_response(200, headers=session_headers),
        )

        with self.assertRaisesRegex(MiFitnessAuthenticationError, "账号与配置"):
            asyncio.run(adapter._login_with_token())

    def test_login_requires_a_service_token_cookie(self) -> None:
        payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://api.io.mi.com/session",
            "userId": "user",
        }
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(payload).encode(),
            ),
            _http_response(200, headers={"Set-Cookie": "userId=user; Secure"}),
        )

        with self.assertRaisesRegex(MiFitnessAuthenticationError, "服务令牌"):
            asyncio.run(adapter._login_with_token())

    def test_login_rejects_session_with_only_unknown_cookies(self) -> None:
        payload = {
            "ssecurity": base64.b64encode(b"s" * 16).decode(),
            "location": "https://api.io.mi.com/session",
        }
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=LOGIN_PREFIX + json.dumps(payload).encode(),
            ),
            _http_response(200, headers={"Set-Cookie": "unknownCookie=ignored"}),
        )

        with self.assertRaises(MiFitnessAuthenticationError):
            asyncio.run(adapter._login_with_token())
        self.assertEqual(adapter._cookies, "")

    def test_connect_commits_login_credentials_only_after_health_api_probe(
        self,
    ) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _login_with_token(self):
                self._ssecurity = b"synthetic-security"
                self._cookies = "serviceToken=synthetic"
                return self.user_id, "replacement-token"

            async def _discover_data_types(self, budget=None):
                self.asserted_original_token = self.pass_token
                return ["sleep"]

        adapter = FixtureAdapter("user", "original-token", "cn")

        self.assertTrue(asyncio.run(adapter.connect()))
        self.assertEqual(adapter.asserted_original_token, "original-token")
        self.assertEqual(adapter.pass_token, "replacement-token")

    def test_failed_health_api_validation_keeps_configured_credentials(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _login_with_token(self):
                self._ssecurity = b"synthetic-security"
                self._cookies = "serviceToken=synthetic"
                return self.user_id, "replacement-token"

            async def _discover_data_types(self, budget=None):
                raise RuntimeError("synthetic probe failure")

        adapter = FixtureAdapter("user", "original-token", "cn")

        self.assertFalse(asyncio.run(adapter.connect()))
        self.assertEqual(adapter.user_id, "user")
        self.assertEqual(adapter.pass_token, "original-token")

    def test_connect_propagates_rate_limit_and_closes_session(self) -> None:
        class RateLimitedAdapter(MiFitnessCloudAdapter):
            async def _establish_session(self) -> None:
                self._set_rate_limit(60)
                raise MiFitnessRateLimitError(60)

        adapter = RateLimitedAdapter("user", "token", "cn")
        with self.assertRaises(MiFitnessRateLimitError):
            asyncio.run(adapter.connect())
        self.assertIsNone(adapter._client)
        self.assertFalse(adapter.is_connected())

    def test_non_transient_http_error_is_not_retried(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(_http_response(400))
        adapter._ssecurity = b"synthetic-security"
        with self.assertRaisesRegex(MiFitnessResponseError, "HTTP 400"):
            asyncio.run(adapter._request("https://example.invalid", "/path", {}))
        self.assertEqual(adapter._client.calls, 1)

    def test_health_api_401_invalidates_the_active_session_without_retry(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(_http_response(401))
        adapter._ssecurity = b"synthetic-security"
        adapter._connected = True

        with self.assertRaises(MiFitnessAuthenticationError):
            asyncio.run(adapter._request("https://example.invalid", "/path", {}))

        self.assertEqual(adapter._client.calls, 1)
        self.assertFalse(adapter.is_connected())
        self.assertTrue(adapter.authentication_failed)
        self.assertEqual(adapter._cookies, "")
        self.assertEqual(adapter._ssecurity, b"")

    def test_health_api_business_403_invalidates_session_material(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=base64.b64encode(
                    json.dumps({"code": 403, "message": "permission denied"}).encode()
                ),
            )
        )
        adapter._ssecurity = b"s" * 16
        adapter._cookies = "serviceToken=synthetic"
        adapter._connected = True

        with patch(
            "astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud._rc4_crypt",
            side_effect=lambda _key, payload: payload,
        ):
            with self.assertRaises(MiFitnessAuthenticationError):
                asyncio.run(adapter._request("https://example.invalid", "/path", {}))

        self.assertFalse(adapter.is_connected())
        self.assertTrue(adapter.authentication_failed)
        self.assertEqual(adapter._cookies, "")
        self.assertEqual(adapter._ssecurity, b"")

    def test_unknown_business_error_does_not_invalidate_valid_session(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(
                200,
                content=base64.b64encode(
                    json.dumps(
                        {"code": 70001, "message": "synthetic data error"}
                    ).encode()
                ),
            )
        )
        adapter._ssecurity = b"s" * 16
        adapter._cookies = "serviceToken=synthetic"
        adapter._connected = True

        with patch(
            "astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud._rc4_crypt",
            side_effect=lambda _key, payload: payload,
        ):
            with self.assertRaises(MiFitnessResponseError):
                asyncio.run(adapter._request("https://example.invalid", "/path", {}))

        self.assertTrue(adapter.is_connected())
        self.assertFalse(adapter.authentication_failed)
        self.assertEqual(adapter._cookies, "serviceToken=synthetic")

    def test_transient_5xx_errors_retry_only_within_the_fixed_budget(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(500),
            _http_response(502),
            _http_response(503),
        )
        adapter._ssecurity = b"synthetic-security"

        with patch(
            "astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "请求失败"):
                asyncio.run(adapter._request("https://example.invalid", "/path", {}))

        self.assertEqual(adapter._client.calls, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.5, 1.0])

    def test_missing_and_null_encrypted_response_fields_fail_closed(self) -> None:
        malformed_bodies = (
            {},
            {"code": None},
            {"code": False},
            {"code": 0, "result": None},
        )
        for body in malformed_bodies:
            with self.subTest(body=body):
                adapter = MiFitnessCloudAdapter("user", "token", "cn")
                adapter._client = _StreamingClient(
                    _http_response(
                        200,
                        content=base64.b64encode(json.dumps(body).encode()),
                    )
                )
                adapter._ssecurity = b"synthetic-security"
                with patch(
                    "astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud._rc4_crypt",
                    side_effect=lambda _key, payload: payload,
                ):
                    with self.assertRaises(MiFitnessResponseError):
                        asyncio.run(
                            adapter._request(
                                "https://example.invalid",
                                "/path",
                                {},
                            )
                        )

    def test_operation_budget_counts_the_entire_encrypted_response(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(_http_response(200, content=b"x" * 9))
        adapter._ssecurity = b"synthetic-security"
        budget = _OperationBudget(max_bytes=8, max_records=10)

        with self.assertRaises(MiFitnessBudgetError):
            asyncio.run(
                adapter._request(
                    "https://example.invalid",
                    "/path",
                    {},
                    budget=budget,
                )
            )
        self.assertEqual(adapter._client.calls, 1)

    def test_heart_aliases_share_budget_and_yield_before_next_alias(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            def __init__(self):
                super().__init__("user", "token", "cn")
                self.calls = []
                self.budgets = []

            async def _fetch_key(self, key, start, end, region, *, budget=None):
                self.calls.append(key)
                self.budgets.append(budget)
                if key == "heart_rate":
                    return [{"time": 1784692800, "value": {"bpm": 72}}]
                return []

        async def collect():
            adapter = FixtureAdapter()
            start, end = _range_around(1784692800)
            iterator = adapter.iter_heart_rate(start, end)
            first = await anext(iterator)
            calls_after_first = list(adapter.calls)
            remaining = [record async for record in iterator]
            return adapter, first, calls_after_first, remaining

        adapter, first, calls_after_first, remaining = asyncio.run(collect())
        self.assertEqual(first.bpm, 72)
        self.assertEqual(calls_after_first, ["heart_rate"])
        self.assertEqual(remaining, [])
        self.assertEqual(len({id(budget) for budget in adapter.budgets}), 1)

    def test_rate_limit_retry_after_is_bounded_and_respected(self) -> None:
        adapter = MiFitnessCloudAdapter("user", "token", "cn")
        adapter._client = _StreamingClient(
            _http_response(429, headers={"Retry-After": "120"})
        )
        adapter._ssecurity = b"synthetic-security"
        with patch(
            "astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud.asyncio.sleep",
            new_callable=AsyncMock,
        ) as sleep:
            with self.assertRaises(MiFitnessRateLimitError) as first:
                asyncio.run(adapter._request("https://example.invalid", "/path", {}))
            self.assertGreaterEqual(first.exception.retry_after_seconds, 119)
            self.assertEqual(adapter._client.calls, 1)
            sleep.assert_not_awaited()

            with self.assertRaises(MiFitnessRateLimitError) as second:
                asyncio.run(adapter._request("https://example.invalid", "/path", {}))
            self.assertGreater(second.exception.retry_after_seconds, 0)
            self.assertEqual(adapter._client.calls, 1)
            sleep.assert_not_awaited()

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
            async def _probe_key(self, key, start, end, region, *, budget=None):
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

    def test_discovery_requires_at_least_one_successful_health_api_probe(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _probe_key(self, key, start, end, region, *, budget=None):
                raise RuntimeError("synthetic probe failure")

        adapter = FixtureAdapter("user", "token", "cn")

        with self.assertRaisesRegex(RuntimeError, "会话未能通过"):
            asyncio.run(adapter._discover_data_types())

    def test_primary_heart_rate_alias_is_used_by_normal_sync(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "heartrate":
                    return [{"time": 1784692800, "value": {"heart_rate": 71}}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            start, end = _range_around(1784692800)
            return [row async for row in adapter.iter_heart_rate(start, end)]

        records = asyncio.run(collect())
        self.assertEqual([record.bpm for record in records], [71])

    def test_invalid_primary_heart_rate_rows_do_not_hide_valid_alias(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "heart_rate":
                    return [{"time": 1784692800, "value": {"unexpected": 71}}]
                if key == "heartrate":
                    return [{"time": 1784692860, "value": {"heart_rate": 73}}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            start, end = _range_around(1784692800)
            return [row async for row in adapter.iter_heart_rate(start, end)]

        records = asyncio.run(collect())
        self.assertEqual([record.bpm for record in records], [73])

    def test_candidate_key_error_is_not_reported_as_empty_success(self) -> None:
        class HeartAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "heart_rate":
                    raise RuntimeError("synthetic heart endpoint failure")
                return []

        class SpO2Adapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
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

    def test_heart_network_error_is_raised_even_when_resting_fallback_has_data(
        self,
    ) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "heart_rate":
                    raise RuntimeError("synthetic heart network failure")
                if key == "resting_heart_rate":
                    return [{"time": 1784692800, "value": {"bpm": 60}}]
                return []

        async def collect():
            start, end = _range_around(1784692800)
            return [
                row
                async for row in FixtureAdapter("user", "token", "cn").iter_heart_rate(
                    start, end
                )
            ]

        with self.assertRaisesRegex(RuntimeError, "heart network failure"):
            asyncio.run(collect())

    def test_spo2_network_error_is_raised_even_when_alias_fallback_has_data(
        self,
    ) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "spo2":
                    raise RuntimeError("synthetic spo2 network failure")
                if key == "blood_oxygen":
                    return [{"time": 1784692800, "value": {"blood_oxygen": 97}}]
                return []

        async def collect():
            start, end = _range_around(1784692800)
            return [
                row
                async for row in FixtureAdapter("user", "token", "cn").iter_spo2(
                    start, end
                )
            ]

        with self.assertRaisesRegex(RuntimeError, "spo2 network failure"):
            asyncio.run(collect())

    def test_blood_oxygen_alias_is_used_by_normal_sync(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region, *, budget=None):
                if key == "spo2":
                    raise MiFitnessResponseError("unsupported key")
                if key == "blood_oxygen":
                    return [{"time": 1784692800, "value": {"blood_oxygen": 97}}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            start, end = _range_around(1784692800)
            return [row async for row in adapter.iter_spo2(start, end)]

        records = asyncio.run(collect())
        self.assertEqual([record.percent for record in records], [97])

    def test_discovery_reports_alias_only_heart_rate_and_spo2(self) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _probe_key(self, key, start, end, region, *, budget=None):
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
            async def _probe_key(self, key, start, end, region, *, budget=None):
                if region == "sg" and key == "sleep":
                    return [{"time": 1784692800, "value": {}}]
                return []

        adapter = FixtureAdapter("user", "token")
        self.assertEqual(asyncio.run(adapter._discover_region()), "sg")

    def test_region_discovery_requires_manual_region_when_all_data_is_empty(
        self,
    ) -> None:
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _probe_key(self, key, start, end, region, *, budget=None):
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
            async def _fetch_key(self, key, start, end, region, *, budget=None):
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
            start, end = _range_around(base)
            return [row async for row in adapter.iter_daily_activity(start, end)]

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
