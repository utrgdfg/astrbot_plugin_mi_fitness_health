"""Offline tests only; these never contact Xiaomi services."""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from astrbot_plugin_mi_fitness_health.models import DailyActivity
from astrbot_plugin_mi_fitness_health.storage import Database


class DatabaseTest(unittest.TestCase):
    """Verify migration and precise insert/update accounting."""

    def test_activity_upsert_and_migration(self) -> None:
        """Database preserves the row and reports added then updated."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            record = DailyActivity("2026-07-22", 1000, 800.0, 100.0, datetime.now(UTC))
            self.assertEqual(database.upsert_activity("user", record), "added")
            self.assertEqual(database.upsert_activity("user", record), "updated")
            self.assertEqual(database.today_activity("user", "2026-07-22")["steps"], 1000)
