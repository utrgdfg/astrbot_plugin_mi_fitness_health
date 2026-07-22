"""Serialized asynchronous synchronization into thread-backed SQLite storage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ..adapters import MiFitnessCloudAdapter
from ..storage import Database


class SyncService:
    """Coordinate manual, startup, and periodic syncs with one async lock."""

    def __init__(self, adapter: MiFitnessCloudAdapter, database: Database, user_id: str):
        """Create a sync service.

        Args:
            adapter: Authenticated Xiaomi cloud adapter.
            database: Local persistent store.
            user_id: Single supported Xiaomi account identifier.
        """
        self.adapter = adapter
        self.database = database
        self.user_id = user_id
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Initialize schema outside AstrBot's event loop."""
        await asyncio.to_thread(self.database.initialize)

    async def sync(self, days: int) -> dict[str, int | str]:
        """Download an overlap window and return exact insert/update counters.

        Args:
            days: Requested historical range; always bounded for reliability.

        Returns:
            Summary suitable for a private command reply.
        """
        days = max(1, min(int(days), 90))
        async with self.lock:
            if not self.adapter.is_connected() and not await self.adapter.connect():
                raise RuntimeError(self.adapter.last_error or "小米健康云连接失败")
            end = datetime.now(UTC)
            start = end - timedelta(days=days + 2)  # delayed uploads and corrections
            counters = {"added": 0, "updated": 0}
            type_count = 0
            for data_type, iterator, writer in (
                ("daily_activity", self.adapter.iter_daily_activity(start, end), self.database.upsert_activity),
                ("heart_rate", self.adapter.iter_heart_rate(start, end), self.database.upsert_heart_rate),
                ("body_measurements", self.adapter.iter_body_measurements(start, end), self.database.upsert_measurement),
            ):
                latest: datetime | None = None
                async for record in iterator:
                    outcome = await asyncio.to_thread(writer, self.user_id, record)
                    counters[outcome] += 1
                    timestamp = getattr(record, "timestamp", getattr(record, "collected_at", None))
                    latest = max(latest, timestamp) if latest and timestamp else timestamp or latest
                await asyncio.to_thread(self.database.update_sync_state, data_type, latest)
                type_count += 1
            return {**counters, "types": type_count, "days": days}
