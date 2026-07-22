"""Offline adapter and alert tests using fully synthetic, redacted fixture data."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from astrbot_plugin_mi_fitness_health.adapters.mi_fitness_cloud import MiFitnessCloudAdapter, _rc4_crypt
from astrbot_plugin_mi_fitness_health.models import HeartRateSample
from astrbot_plugin_mi_fitness_health.services import AlertService
from astrbot_plugin_mi_fitness_health.storage import Database


class AdapterAndAlertTest(unittest.TestCase):
    """Verify protocol primitives and alert safety without external HTTP."""

    def test_rc4_round_trip_and_fixture_parse(self) -> None:
        """RC4 round-trips and accepts a redacted fixture payload."""
        key, value = b"test-key", b"fixture payload"
        self.assertEqual(_rc4_crypt(key, _rc4_crypt(key, value)), value)
        item = json.loads((Path(__file__).parent / "fixtures" / "heart_rate.json").read_text(encoding="utf-8"))[0]
        self.assertEqual(MiFitnessCloudAdapter._value(item)["bpm"], 72)
        self.assertIsNotNone(MiFitnessCloudAdapter._record_time(item))

    def test_workout_record_does_not_complete_passive_alert(self) -> None:
        """Workout records reset a sequence and cannot cause a passive alert."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            for index, workout in enumerate((False, True, False)):
                database.upsert_heart_rate("user", HeartRateSample(f"hr{index}", now + timedelta(minutes=index), 150, "passive", workout))
            alerts = asyncio.run(AlertService(database, "user", 120, 0, 2, 10).evaluate())
            self.assertEqual(alerts, [])

    def test_resting_heart_rate_fallback_is_queryable(self) -> None:
        """Resting-heart-rate data remains available when the sampled key is empty."""
        class FixtureAdapter(MiFitnessCloudAdapter):
            async def _fetch_key(self, key, start, end, region):
                if key == "resting_heart_rate":
                    return [{"time": 1784692800000, "zone_offset": 28800, "value": '{"heart_rate":68}'}]
                return []

        async def collect():
            adapter = FixtureAdapter("user", "token", "cn")
            return [record async for record in adapter.iter_heart_rate(datetime.now(UTC), datetime.now(UTC))]

        records = asyncio.run(collect())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].bpm, 68)
        self.assertEqual(records[0].sample_type, "passive")
