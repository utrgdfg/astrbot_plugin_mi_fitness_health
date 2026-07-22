"""Offline tests only; these never contact Xiaomi services."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from astrbot_plugin_mi_fitness_health.models import DailyActivity, HeartRateSample
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

    def test_batch_write(self) -> None:
        """Large sample types use one transaction-oriented API."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            result = database.upsert_many("user", "heart_rate", [
                HeartRateSample("a", now, 70, "passive", False),
                HeartRateSample("b", now, 72, "passive", False),
            ])
            self.assertEqual(result, {"added": 2, "updated": 0})
            self.assertEqual(database.upsert_many("user", "heart_rate", [HeartRateSample("a", now, 71, "passive", False)]), {"added": 0, "updated": 1})
            database.touch_private_owner_session("owner", "qq:FriendMessage:123", now)
            state = database.private_owner_session("owner")
            self.assertEqual(state["session"], "qq:FriendMessage:123")
            self.assertEqual(state["updated_at"], now.isoformat())

    def test_v3_alert_table_migrates_without_deleting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE schema_version(version INTEGER NOT NULL)")
                connection.execute("INSERT INTO schema_version VALUES(3)")
                connection.execute("CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, alert_type TEXT NOT NULL, created_at TEXT NOT NULL, message TEXT NOT NULL)")
                connection.execute("INSERT INTO alerts(alert_type,created_at,message) VALUES('legacy','2026-01-01T00:00:00+00:00','kept')")
                connection.commit()
            database = Database(path)
            database.initialize()
            self.assertEqual(database.last_alert_at("legacy"), "2026-01-01T00:00:00+00:00")
            database.add_alert("new", "message", "event-1")
            self.assertTrue(database.alert_event_sent("new", "event-1"))
