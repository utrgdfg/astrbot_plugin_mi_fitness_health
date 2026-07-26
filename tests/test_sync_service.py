"""Synchronization selection and operation-lock tests without network access."""

from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from datetime import UTC
from pathlib import Path
from unittest.mock import patch

import astrbot_test_stub  # noqa: F401

from astrbot_plugin_mi_fitness_health.services import SyncService
from astrbot_plugin_mi_fitness_health.storage import Database


class _RecordingAdapter:
    def __init__(self):
        self.connected = True
        self.last_error = None
        self.authentication_failed = False
        self.user_timezone = UTC
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
