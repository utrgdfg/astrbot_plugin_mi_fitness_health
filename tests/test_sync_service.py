"""Synchronization selection and operation-lock tests without network access."""

from __future__ import annotations

import asyncio
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import astrbot_test_stub  # noqa: F401
from astrbot_plugin_mi_fitness_health.adapters import MiFitnessRateLimitError
from astrbot_plugin_mi_fitness_health.models import SleepSession
from astrbot_plugin_mi_fitness_health.services import SyncService
from astrbot_plugin_mi_fitness_health.storage import Database


class _RecordingAdapter:
    def __init__(self):
        self.connected = True
        self.last_error = None
        self.authentication_failed = False
        self.user_timezone = UTC
        self.region = ""
        self.calls = []

    def is_connected(self):
        return self.connected

    async def connect(self):
        self.connected = True
        return True

    async def _empty(self, name):
        self.calls.append(name)
        if False:
            yield None

    def iter_daily_activity(self, start, end):
        return self._empty("daily_activity")

    def iter_heart_rate(self, start, end):
        return self._empty("heart_rate")

    def iter_body_measurements(self, start, end):
        return self._empty("body_measurements")

    def iter_sleep(self, start, end):
        return self._empty("sleep")

    def iter_spo2(self, start, end):
        return self._empty("spo2")

    def iter_stress(self, start, end):
        return self._empty("stress")


class SyncServiceTest(unittest.TestCase):
    def test_empty_or_unknown_selection_is_rejected_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            adapter = _RecordingAdapter()
            service = SyncService(adapter, database, "user")
            with self.assertRaisesRegex(ValueError, "没有可同步"):
                asyncio.run(service.sync(1, set()))
            with self.assertRaisesRegex(ValueError, "没有可同步"):
                asyncio.run(service.sync(1, {"unknown"}))
            self.assertEqual(adapter.calls, [])

    def test_natural_query_can_sync_only_required_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            adapter = _RecordingAdapter()
            service = SyncService(adapter, database, "user")
            result = asyncio.run(service.sync(1, {"sleep"}))
            self.assertEqual(adapter.calls, ["sleep"])
            self.assertEqual(set(result["details"]), {"sleep"})
            self.assertIsNotNone(database.latest_sync_at("user", ("sleep",)))
            self.assertIsNone(database.latest_sync_at("user", ("heart_rate",)))

    def test_one_dataset_timeout_does_not_abort_other_selected_datasets(self) -> None:
        class PartiallySlowAdapter(_RecordingAdapter):
            async def _empty(self, name):
                self.calls.append(name)
                if name == "sleep":
                    await asyncio.sleep(1)
                if False:
                    yield None

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            adapter = PartiallySlowAdapter()
            service = SyncService(adapter, database, "user")
            with (
                patch(
                    "astrbot_plugin_mi_fitness_health.services.sync_service.DATASET_TIMEOUT_SECONDS",
                    0.01,
                ),
                patch(
                    "astrbot_plugin_mi_fitness_health.services.sync_service.SYNC_TIMEOUT_SECONDS",
                    1,
                ),
            ):
                result = asyncio.run(service.sync(1, {"sleep", "stress"}))

            self.assertEqual(adapter.calls, ["sleep", "stress"])
            self.assertEqual(result["errors"], 1)
            self.assertIn("error", result["details"]["sleep"])
            self.assertEqual(result["details"]["stress"]["fetched"], 0)

    def test_successful_sync_reuses_last_record_with_two_day_overlap(self) -> None:
        class WindowRecordingAdapter(_RecordingAdapter):
            def __init__(self, record):
                super().__init__()
                self.record = record
                self.starts = []

            def iter_sleep(self, start, end):
                self.starts.append(start)

                async def records():
                    if len(self.starts) == 1:
                        yield self.record

                return records()

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            end_at = datetime.now(UTC) - timedelta(hours=1)
            record = SleepSession(
                "sleep-incremental",
                end_at - timedelta(hours=8),
                end_at,
                480,
                450,
                30,
                80,
            )
            adapter = WindowRecordingAdapter(record)
            service = SyncService(adapter, database, "user")

            asyncio.run(service.sync(7, {"sleep"}))
            asyncio.run(service.sync(7, {"sleep"}))

            self.assertEqual(len(adapter.starts), 2)
            expected = end_at - timedelta(days=2)
            self.assertLess(abs((adapter.starts[1] - expected).total_seconds()), 2)
            self.assertIsNotNone(database.sync_record_at("user", "sleep"))

    def test_connection_time_does_not_consume_download_deadline(self) -> None:
        class SlowConnectAdapter(_RecordingAdapter):
            def __init__(self):
                super().__init__()
                self.connected = False

            async def connect(self):
                await asyncio.sleep(0.15)
                self.connected = True
                return True

        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = SyncService(SlowConnectAdapter(), database, "user")
            with patch(
                "astrbot_plugin_mi_fitness_health.services.sync_service.SYNC_TIMEOUT_SECONDS",
                0.1,
            ):
                result = asyncio.run(service.sync(1, {"sleep"}))

            self.assertEqual(result["details"]["sleep"]["fetched"], 0)

    def test_region_cache_loads_only_when_region_was_not_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            adapter = _RecordingAdapter()
            service = SyncService(adapter, database, "user")
            database.set_metadata(service._region_metadata_key(), "cn")

            asyncio.run(service.initialize())
            self.assertEqual(adapter.region, "cn")

            database.set_metadata(service._region_metadata_key(), "CN")
            invalid_adapter = _RecordingAdapter()
            invalid_service = SyncService(invalid_adapter, database, "user")
            asyncio.run(invalid_service.initialize())
            self.assertEqual(invalid_adapter.region, "")

            explicit_adapter = _RecordingAdapter()
            explicit_adapter.region = "us"
            explicit_service = SyncService(explicit_adapter, database, "user")
            asyncio.run(explicit_service.initialize())
            self.assertEqual(explicit_adapter.region, "us")
            asyncio.run(explicit_service.connect())
            self.assertEqual(
                database.get_metadata(explicit_service._region_metadata_key()), "us"
            )

    def test_every_connection_path_persists_a_discovered_region(self) -> None:
        class DiscoveringAdapter(_RecordingAdapter):
            def __init__(self, region):
                super().__init__()
                self.connected = False
                self.discovered_region = region

            async def connect(self):
                self.connected = True
                self.region = self.discovered_region
                return True

            async def probe_data_keys(self, start, end):
                return {}

        for path_name, expected_region in (
            ("connect", "de"),
            ("probe", "i2"),
            ("sync", "sg"),
        ):
            with self.subTest(path_name=path_name):
                with tempfile.TemporaryDirectory() as directory:
                    database = Database(Path(directory) / "health.sqlite3")
                    adapter = DiscoveringAdapter(expected_region)
                    service = SyncService(adapter, database, "user")
                    asyncio.run(service.initialize())
                    if path_name == "connect":
                        self.assertTrue(asyncio.run(service.connect()))
                    elif path_name == "probe":
                        asyncio.run(
                            service.probe_data_keys(
                                datetime.now(UTC), datetime.now(UTC)
                            )
                        )
                    else:
                        asyncio.run(service.sync(1, {"sleep"}))
                    self.assertEqual(
                        database.get_metadata(service._region_metadata_key()),
                        expected_region,
                    )

    def test_rate_limit_stops_the_batch_after_recording_current_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()

            class RateLimitedAdapter(_RecordingAdapter):
                async def _empty(self, name):
                    self.calls.append(name)
                    if name == "sleep":
                        raise MiFitnessRateLimitError(60)
                    if False:
                        yield None

            adapter = RateLimitedAdapter()
            service = SyncService(adapter, database, "user")
            with self.assertRaises(MiFitnessRateLimitError):
                asyncio.run(service.sync(1, {"sleep", "stress"}))
            self.assertEqual(adapter.calls, ["sleep"])
            self.assertIsNotNone(database.latest_sync_failure_at("user", ("sleep",)))

    def test_connection_and_sync_share_one_operation_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()

            class BlockingAdapter(_RecordingAdapter):
                def __init__(self):
                    super().__init__()
                    self.connected = False
                    self.active = 0
                    self.max_active = 0
                    self.connect_started = asyncio.Event()
                    self.release_connect = asyncio.Event()

                async def connect(self):
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    self.connect_started.set()
                    await self.release_connect.wait()
                    self.active -= 1
                    self.connected = True
                    return True

                async def _empty(self, name):
                    self.active += 1
                    self.max_active = max(self.max_active, self.active)
                    await asyncio.sleep(0)
                    self.calls.append(name)
                    self.active -= 1
                    if False:
                        yield None

            adapter = BlockingAdapter()
            service = SyncService(adapter, database, "user")

            async def run():
                connect_task = asyncio.create_task(service.connect())
                await adapter.connect_started.wait()
                sync_task = asyncio.create_task(service.sync(1, {"sleep"}))
                await asyncio.sleep(0)
                adapter.release_connect.set()
                await asyncio.gather(connect_task, sync_task)

            asyncio.run(run())
            self.assertEqual(adapter.max_active, 1)

    def test_forced_connection_closes_existing_session_before_reconnecting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()

            class ReconnectingAdapter(_RecordingAdapter):
                def __init__(self):
                    super().__init__()
                    self.closed = 0
                    self.connected_count = 0

                async def close(self):
                    self.closed += 1
                    self.connected = False

                async def connect(self):
                    self.connected_count += 1
                    self.connected = True
                    return True

            adapter = ReconnectingAdapter()
            service = SyncService(adapter, database, "user")
            self.assertTrue(asyncio.run(service.connect(force=True)))
            self.assertEqual(adapter.closed, 1)
            self.assertEqual(adapter.connected_count, 1)

    def test_entire_sync_has_a_bounded_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()

            class SlowAdapter(_RecordingAdapter):
                async def _empty(self, name):
                    await asyncio.sleep(1)
                    if False:
                        yield None

            service = SyncService(SlowAdapter(), database, "user")
            with patch(
                "astrbot_plugin_mi_fitness_health.services.sync_service.SYNC_TIMEOUT_SECONDS",
                0.001,
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(service.sync(1, {"sleep"}))

    def test_database_write_finishes_before_the_sync_lock_is_released(self) -> None:
        """A cloud deadline must not cancel an already-running SQLite thread."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            service = SyncService(_RecordingAdapter(), database, "user")
            original = database.upsert_many

            def slow_write(*args):
                time.sleep(0.05)
                return original(*args)

            with (
                patch.object(database, "upsert_many", side_effect=slow_write),
                patch(
                    "astrbot_plugin_mi_fitness_health.services.sync_service.SYNC_TIMEOUT_SECONDS",
                    0.02,
                ),
            ):
                result = asyncio.run(service.sync(1, {"sleep"}))

            self.assertEqual(result["details"]["sleep"]["fetched"], 0)
            self.assertIsNotNone(database.latest_sync_at("user", ("sleep",)))

    def test_cancelled_write_finishes_before_purge_can_acquire_the_lock(self) -> None:
        """Cancellation cannot let a stale SQLite thread reinsert rows after purge."""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()
            started = threading.Event()
            release = threading.Event()
            original = database.upsert_many

            class OneSleepAdapter(_RecordingAdapter):
                async def _sleep(self):
                    end = datetime.now(UTC)
                    yield SleepSession(
                        "sleep-1",
                        end - timedelta(hours=8),
                        end,
                        480,
                        450,
                        30,
                        80,
                    )

                def iter_sleep(self, start, end):
                    return self._sleep()

            def slow_write(*args):
                started.set()
                release.wait(timeout=2)
                return original(*args)

            service = SyncService(OneSleepAdapter(), database, "user")

            async def run():
                sync_task = asyncio.create_task(service.sync(1, {"sleep"}))
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                sync_task.cancel()
                purge_task = asyncio.create_task(service.purge_local_data("owner"))
                await asyncio.sleep(0.02)
                self.assertFalse(purge_task.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await sync_task
                await purge_task

            with patch.object(database, "upsert_many", side_effect=slow_write):
                asyncio.run(run())

            self.assertEqual(database.recent_sleep("user"), [])
            self.assertIsNone(database.latest_sync_at("user"))

    def test_diagnostic_deadline_closes_the_shared_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "health.sqlite3")
            database.initialize()

            class SlowDiagnosticAdapter(_RecordingAdapter):
                def __init__(self):
                    super().__init__()
                    self.closed = 0

                async def probe_data_keys(self, start, end):
                    await asyncio.sleep(1)
                    return {}

                async def close(self):
                    self.closed += 1
                    self.connected = False

            adapter = SlowDiagnosticAdapter()
            service = SyncService(adapter, database, "user")
            with patch(
                "astrbot_plugin_mi_fitness_health.services.sync_service.DIAGNOSTIC_TIMEOUT_SECONDS",
                0.001,
            ):
                with self.assertRaisesRegex(RuntimeError, "数据诊断超过"):
                    asyncio.run(
                        service.probe_data_keys(datetime.now(UTC), datetime.now(UTC))
                    )
            self.assertEqual(adapter.closed, 1)
