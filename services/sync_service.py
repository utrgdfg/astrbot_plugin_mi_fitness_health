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

    async def sync(self, days: int) -> dict[str, object]:
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
            counters = {"added": 0, "updated": 0, "errors": 0}
            details: dict[str, dict[str, object]] = {}
            for data_type, iterator in (
                ("daily_activity", self.adapter.iter_daily_activity(start, end)),
                ("heart_rate", self.adapter.iter_heart_rate(start, end)),
                ("body_measurements", self.adapter.iter_body_measurements(start, end)),
                ("sleep", self.adapter.iter_sleep(start, end)),
                ("spo2", self.adapter.iter_spo2(start, end)),
                ("stress", self.adapter.iter_stress(start, end)),
            ):
                try:
                    records = [record async for record in iterator]
                    outcome = await asyncio.to_thread(self.database.upsert_many, self.user_id, data_type, records)
                    latest = max((getattr(record, "timestamp", None) or getattr(record, "collected_at", None) or getattr(record, "end_at", None) for record in records), default=None)
                    await asyncio.to_thread(self.database.update_sync_state, data_type, latest)
                    counters["added"] += outcome["added"]
                    counters["updated"] += outcome["updated"]
                    details[data_type] = {"fetched": len(records), **outcome}
                except Exception as error:
                    # A variant key can fail for one account. Keep the other
                    # datasets usable and expose only a short safe status.
                    counters["errors"] += 1
                    details[data_type] = {"error": str(error)[:120]}
            return {**counters, "types": len(details), "days": days, "details": details}
