"""Synchronization selection and operation-lock tests without network access."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import UTC
from pathlib import Path

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
            self.assertIsNotNone(database.latest_sync_at(("sleep",)))
            self.assertIsNone(database.latest_sync_at(("heart_rate",)))

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
