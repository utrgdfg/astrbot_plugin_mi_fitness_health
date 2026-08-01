"""Offline tests only; these never contact Xiaomi services."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from astrbot_plugin_mi_fitness_health.models import DailyActivity, HeartRateSample
from astrbot_plugin_mi_fitness_health.storage import Database
from astrbot_plugin_mi_fitness_health.storage.database import (
    APPLICATION_ID,
    OWNERSHIP_KEY,
    OWNERSHIP_TABLE,
    OWNERSHIP_VALUE,
)


class DatabaseTest(unittest.TestCase):
    """Verify migration and precise insert/update accounting."""

    def test_database_records_ownership_and_protects_generic_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            database = Database(path)
            database.initialize()
            database.set_metadata("xiaomi_region:user", "cn")
            self.assertEqual(database.get_metadata("xiaomi_region:user"), "cn")
            database.set_metadata("xiaomi_region:user", None)
            self.assertIsNone(database.get_metadata("xiaomi_region:user"))
            with self.assertRaises(ValueError):
                database.set_metadata(OWNERSHIP_KEY, "other")
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA application_id").fetchone()[0],
                    APPLICATION_ID,
                )
                self.assertEqual(
                    connection.execute(
                        f"SELECT value FROM {OWNERSHIP_TABLE} WHERE key=?",
                        (OWNERSHIP_KEY,),
                    ).fetchone()[0],
                    OWNERSHIP_VALUE,
                )

    def test_custom_path_rejects_non_database_and_foreign_sqlite_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            text_path = Path(directory) / "notes.sqlite3"
            text_path.write_text("do not overwrite", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不是 SQLite"):
                Database(text_path, custom_path=True).initialize()
            self.assertEqual(text_path.read_text(encoding="utf-8"), "do not overwrite")

            foreign_path = Path(directory) / "foreign.sqlite3"
            with closing(sqlite3.connect(foreign_path)) as connection:
                connection.execute("CREATE TABLE unrelated(value TEXT)")
                connection.execute("INSERT INTO unrelated VALUES('kept')")
                connection.commit()
            with self.assertRaisesRegex(ValueError, "不是本插件"):
                Database(foreign_path, custom_path=True).initialize()
            with closing(sqlite3.connect(foreign_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM unrelated").fetchone()[0],
                    "kept",
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='schema_version'"
                    ).fetchone()
                )

    def test_custom_path_rejects_table_name_only_legacy_imitation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "imitation.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE schema_version(version INTEGER NOT NULL);
                    INSERT INTO schema_version VALUES(1);
                    CREATE TABLE daily_activity(value TEXT);
                    CREATE TABLE heart_rate_samples(value TEXT);
                    CREATE TABLE body_measurements(value TEXT);
                    CREATE TABLE sync_state(value TEXT);
                    CREATE TABLE alerts(value TEXT);
                    """
                )
                connection.commit()

            with self.assertRaisesRegex(ValueError, "不是本插件"):
                Database(path, custom_path=True).initialize()
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    tuple(
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(daily_activity)"
                        ).fetchall()
                    ),
                    ("value",),
                )

    def test_custom_path_accepts_and_claims_a_legacy_plugin_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            Database(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(f"DROP TABLE {OWNERSHIP_TABLE}")
                connection.execute("PRAGMA application_id=0")
                connection.commit()

            database = Database(path, custom_path=True)
            database.initialize()
            database.set_metadata("xiaomi_region:user", "sg")
            self.assertEqual(database.get_metadata("xiaomi_region:user"), "sg")

    def test_custom_path_revalidates_the_open_handle_before_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            Database(path).initialize()
            database = Database(path, custom_path=True)
            validate = database._validate_custom_path

            def validate_then_replace() -> None:
                validate()
                path.unlink()
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE unrelated(value TEXT)")
                    connection.execute("INSERT INTO unrelated VALUES('kept')")
                    connection.commit()

            with (
                patch.object(
                    database,
                    "_validate_custom_path",
                    side_effect=validate_then_replace,
                ),
                self.assertRaisesRegex(ValueError, "不是本插件"),
            ):
                database.initialize()

            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM unrelated").fetchone()[0],
                    "kept",
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='schema_version'"
                    ).fetchone()
                )

    def test_custom_path_rejects_runtime_database_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            database = Database(path, custom_path=True)
            database.initialize()
            replacement = Path(directory) / "replacement.sqlite3"
            with closing(sqlite3.connect(replacement)) as connection:
                connection.execute("CREATE TABLE unrelated(value TEXT)")
                connection.execute("INSERT INTO unrelated VALUES('kept')")
                connection.commit()
            path.unlink()
            replacement.replace(path)

            with self.assertRaisesRegex(RuntimeError, "归属"):
                database.get_metadata("xiaomi_region:user")
            with self.assertRaisesRegex(RuntimeError, "归属"):
                database.compact()
            with closing(sqlite3.connect(path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM unrelated").fetchone()[0],
                    "kept",
                )

    def test_custom_path_requires_an_absolute_database_filename(self) -> None:
        with self.assertRaisesRegex(ValueError, "绝对路径"):
            Database(Path("relative.sqlite3"), custom_path=True).initialize()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "后缀"):
                Database(Path(directory) / "health.txt", custom_path=True).initialize()

    def test_newer_schema_is_not_opened_by_an_older_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "future.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE TABLE schema_version(version INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO schema_version VALUES(999)")
                connection.commit()
            with self.assertRaisesRegex(RuntimeError, "拒绝降级"):
                Database(path).initialize()

    def test_activity_upsert_and_migration(self) -> None:
        """Database preserves the row and reports added then updated."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            record = DailyActivity("2026-07-22", 1000, 800.0, 100.0, datetime.now(UTC))
            self.assertEqual(database.upsert_activity("user", record), "added")
            self.assertEqual(database.upsert_activity("user", record), "updated")
            self.assertEqual(
                database.today_activity("user", "2026-07-22")["steps"], 1000
            )

    def test_batch_write(self) -> None:
        """Large sample types use one transaction-oriented API."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            result = database.upsert_many(
                "user",
                "heart_rate",
                [
                    HeartRateSample("a", now, 70, "passive", False),
                    HeartRateSample("b", now, 72, "passive", False),
                ],
            )
            self.assertEqual(result, {"added": 2, "updated": 0})
            self.assertEqual(
                database.upsert_many(
                    "user",
                    "heart_rate",
                    [HeartRateSample("a", now, 71, "passive", False)],
                ),
                {"added": 0, "updated": 1},
            )
            database.touch_private_owner_session("owner", "qq:FriendMessage:123", now)
            state = database.private_owner_session("owner")
            self.assertEqual(state["session"], "qq:FriendMessage:123")
            self.assertEqual(state["updated_at"], now.isoformat())

    def test_v3_alert_table_migrates_without_deleting_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "CREATE TABLE schema_version(version INTEGER NOT NULL)"
                )
                connection.execute("INSERT INTO schema_version VALUES(3)")
                connection.execute(
                    "CREATE TABLE alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, alert_type TEXT NOT NULL, created_at TEXT NOT NULL, message TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO alerts(alert_type,created_at,message) VALUES('legacy','2026-01-01T00:00:00+00:00','kept')"
                )
                connection.commit()
            database = Database(path)
            database.initialize()
            self.assertEqual(
                database.last_alert_at("", "legacy"), "2026-01-01T00:00:00+00:00"
            )
            with closing(sqlite3.connect(path)) as connection:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='care_deliveries'"
                    ).fetchone()
                )
            database.add_alert("owner", "new", "message", "event-1")
            self.assertTrue(database.alert_event_sent("owner", "new", "event-1"))

    def test_v5_migration_keeps_health_rows_and_discards_unowned_sync_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE schema_version(version INTEGER NOT NULL);
                    INSERT INTO schema_version VALUES(5);
                    CREATE TABLE daily_activity (
                        user_id TEXT NOT NULL, date TEXT NOT NULL,
                        steps INTEGER NOT NULL, distance_m REAL NOT NULL,
                        active_kcal REAL NOT NULL, collected_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL, PRIMARY KEY(user_id,date)
                    );
                    CREATE TABLE sync_state (
                        data_type TEXT PRIMARY KEY, last_sync_at TEXT NOT NULL,
                        last_record_at TEXT
                    );
                    CREATE TABLE sync_failures (
                        data_type TEXT PRIMARY KEY, last_attempt_at TEXT NOT NULL,
                        last_error TEXT NOT NULL
                    );
                    CREATE TABLE alerts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        alert_type TEXT NOT NULL, created_at TEXT NOT NULL,
                        message TEXT NOT NULL, event_key TEXT
                    );
                    INSERT INTO daily_activity VALUES(
                        'user','2026-07-22',4321,3000,210,
                        '2026-07-22T12:00:00+00:00',
                        '2026-07-22T12:00:00+00:00'
                    );
                    INSERT INTO sync_state VALUES(
                        'daily_activity','2026-07-22T12:00:00+00:00',NULL
                    );
                    INSERT INTO sync_failures VALUES(
                        'sleep','2026-07-22T12:00:00+00:00','legacy failure'
                    );
                    INSERT INTO alerts(alert_type,created_at,message,event_key)
                    VALUES(
                        'legacy','2026-07-22T12:00:00+00:00','kept','legacy-event'
                    );
                    """
                )
                connection.commit()

            database = Database(path)
            database.initialize()

            self.assertEqual(
                database.today_activity("user", "2026-07-22")["steps"], 4321
            )
            self.assertIsNone(database.latest_sync_at("user"))
            self.assertIsNone(database.latest_sync_failure_at("user", ("sleep",)))
            self.assertEqual(
                database.last_alert_at("", "legacy"),
                "2026-07-22T12:00:00+00:00",
            )
            self.assertIsNone(database.last_alert_at("owner", "legacy"))

    def test_required_sync_freshness_tracks_each_dataset_and_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            database.update_sync_state("user", "daily_activity", now)
            database.update_sync_state("user", "sleep", now)
            self.assertIsNotNone(
                database.latest_sync_at("user", ("daily_activity", "sleep"))
            )
            self.assertIsNone(
                database.latest_sync_at("user", ("daily_activity", "heart_rate"))
            )
            database.update_sync_failure("user", "sleep", "synthetic temporary failure")
            self.assertIsNone(
                database.latest_sync_at("user", ("daily_activity", "sleep"))
            )
            database.update_sync_state("user", "sleep", now)
            self.assertIsNotNone(
                database.latest_sync_at("user", ("daily_activity", "sleep"))
            )

    def test_sync_state_is_isolated_between_xiaomi_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            database.update_sync_state("account-a", "sleep", now)
            database.update_sync_state("account-b", "heart_rate", now)

            self.assertIsNotNone(database.latest_sync_at("account-a", ("sleep",)))
            self.assertIsNone(database.latest_sync_at("account-a", ("heart_rate",)))
            self.assertIsNotNone(database.latest_sync_at("account-b", ("heart_rate",)))
            database.update_sync_failure(
                "account-a", "sleep", "synthetic temporary failure"
            )
            self.assertIsNone(database.latest_sync_at("account-a", ("sleep",)))
            self.assertIsNotNone(database.latest_sync_at("account-b", ("heart_rate",)))

    def test_sync_freshness_compares_mixed_iso_offsets_chronologically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.sqlite3"
            database = Database(path)
            database.initialize()
            with closing(sqlite3.connect(path)) as connection:
                connection.executemany(
                    "INSERT INTO sync_state(user_id,data_type,last_sync_at,last_record_at) VALUES(?,?,?,NULL)",
                    (
                        ("user", "daily_activity", "2026-01-01T09:00:00+08:00"),
                        ("user", "sleep", "2026-01-01T02:00:00+00:00"),
                    ),
                )
                connection.commit()
            self.assertEqual(
                database.latest_sync_at("user", ("daily_activity", "sleep")),
                "2026-01-01T01:00:00+00:00",
            )
            self.assertEqual(
                database.latest_sync_at("user"),
                "2026-01-01T02:00:00+00:00",
            )

    def test_activity_replacement_removes_legacy_cloud_zone_date(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            collected = datetime(2026, 1, 1, 16, 30, tzinfo=UTC)
            database.upsert_activity(
                "user", DailyActivity("2026-01-01", 10, 8, 1, collected)
            )
            result = database.replace_activity_records(
                "user",
                [DailyActivity("2026-01-02", 20, 16, 2, collected)],
                timezone(timedelta(hours=8)),
            )
            self.assertEqual(result, {"added": 1, "updated": 0})
            self.assertIsNone(database.today_activity("user", "2026-01-01"))
            self.assertEqual(database.today_activity("user", "2026-01-02")["steps"], 20)

    def test_retention_and_explicit_purge_remove_only_local_plugin_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            old = datetime.now(UTC) - timedelta(days=30)
            database.upsert_activity(
                "user", DailyActivity(old.date().isoformat(), 10, 8, 1, old)
            )
            database.upsert_heart_rate(
                "user", HeartRateSample("old", old, 70, "passive", False)
            )
            self.assertGreaterEqual(database.prune_user_data("user", 7), 2)
            self.assertEqual(database.heart_rates_since("user", old.isoformat()), [])
            database.upsert_activity(
                "user",
                DailyActivity(
                    datetime.now(UTC).date().isoformat(),
                    20,
                    16,
                    2,
                    datetime.now(UTC),
                ),
            )
            database.touch_private_owner_session("owner", "qq:FriendMessage:123")
            database.update_sync_state("user", "daily_activity", datetime.now(UTC))
            self.assertGreaterEqual(database.purge_user_data("user", "owner"), 3)
            self.assertIsNone(database.private_owner_session("owner"))
            self.assertIsNone(database.latest_sync_at("user"))

    def test_retention_prunes_only_the_selected_owners_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            old = datetime.now(UTC) - timedelta(days=30)
            database.add_alert("owner-a", "proactive_message", "a", created_at=old)
            database.add_alert("owner-b", "proactive_message", "b", created_at=old)

            database.prune_user_data("user", 7, UTC, "owner-a")

            self.assertIsNone(database.last_alert_at("owner-a", "proactive_message"))
            self.assertIsNotNone(database.last_alert_at("owner-b", "proactive_message"))

    def test_retention_honors_values_shorter_than_seven_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            now = datetime.now(UTC)
            two_days_old = now - timedelta(days=2)
            database.upsert_heart_rate(
                "user",
                HeartRateSample(
                    "old",
                    two_days_old,
                    70,
                    "passive",
                    False,
                ),
            )
            database.upsert_heart_rate(
                "user",
                HeartRateSample("current", now, 72, "passive", False),
            )

            database.prune_user_data("user", 1)

            rows = database.heart_rates_since(
                "user", (now - timedelta(days=3)).isoformat()
            )
            self.assertEqual([row["record_id"] for row in rows], ["current"])
