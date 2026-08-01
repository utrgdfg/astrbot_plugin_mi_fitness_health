"""Serialized asynchronous synchronization into thread-backed SQLite storage."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

from ..adapters import (
    MiFitnessAuthenticationError,
    MiFitnessCloudAdapter,
    MiFitnessRateLimitError,
)
from ..storage import Database
from ..utils.privacy import redact_error

SYNC_TIMEOUT_SECONDS = 300
DIAGNOSTIC_TIMEOUT_SECONDS = 100
MAX_RECORDS_PER_DATASET = 100_000
KNOWN_REGIONS = frozenset({"cn", "ru", "de", "i2", "sg", "us"})


class SyncService:
    """Coordinate manual, startup, and periodic syncs with one async lock."""

    DATA_TYPES = {
        "daily_activity",
        "heart_rate",
        "body_measurements",
        "sleep",
        "spo2",
        "stress",
    }

    def __init__(
        self,
        adapter: MiFitnessCloudAdapter,
        database: Database,
        user_id: str,
        retention_days: int = 0,
        owner_platform_id: str = "",
    ):
        """Create a sync service.

        Args:
            adapter: Authenticated Xiaomi cloud adapter.
            database: Local persistent store.
            user_id: Single supported Xiaomi account identifier.
            retention_days: Optional local history retention; zero preserves all data.
            owner_platform_id: AstrBot owner whose reminder audit rows may be pruned.
        """
        self.adapter = adapter
        self.database = database
        self.user_id = user_id
        self.retention_days = max(0, int(retention_days))
        self.owner_platform_id = owner_platform_id
        self.lock = asyncio.Lock()
        self._closed = False
        self._region_was_configured = bool(str(getattr(adapter, "region", "")).strip())

    async def initialize(self) -> None:
        """Initialize schema outside AstrBot's event loop."""
        self._ensure_open()
        await self._await_database(self.database.initialize)
        await self._await_database(
            self.database.prune_user_data,
            self.user_id,
            self.retention_days,
            getattr(self.adapter, "user_timezone", UTC),
            self.owner_platform_id,
        )
        if not self._region_was_configured and not getattr(self.adapter, "region", ""):
            cached_region = await self._await_database(
                self.database.get_metadata, self._region_metadata_key()
            )
            if cached_region in KNOWN_REGIONS:
                self.adapter.region = cached_region

    async def connect(self, *, force: bool = False) -> bool:
        """Serialize explicit connection checks with all cloud operations."""
        async with self.lock:
            self._ensure_open()
            if force and self.adapter.is_connected():
                await self._await_completion(self.adapter.close())
            return await self._ensure_connected()

    async def probe_data_keys(self, start: datetime, end: datetime) -> dict[str, str]:
        """Serialize and bound diagnostics so they cannot monopolize the client."""
        async with self.lock:
            self._ensure_open()
            deadline = asyncio.get_running_loop().time() + DIAGNOSTIC_TIMEOUT_SECONDS
            try:
                if not await self._ensure_connected(deadline):
                    reason = self.adapter.last_error or "小米健康云连接失败"
                    if getattr(self.adapter, "authentication_failed", False):
                        raise MiFitnessAuthenticationError(reason)
                    raise RuntimeError(reason)
                return await self._await_cloud(
                    self.adapter.probe_data_keys(start, end), deadline
                )
            except TimeoutError as error:
                await self._await_completion(self.adapter.close())
                raise RuntimeError(
                    "小米健康云数据诊断超过 100 秒安全时限，已停止并关闭本次连接"
                ) from error
            except asyncio.CancelledError:
                await self._await_completion(self.adapter.close())
                raise

    async def purge_local_data(self, owner_platform_id: str) -> int:
        """Serialize an explicit owner purge with cloud/database operations."""
        async with self.lock:
            self._ensure_open()
            return await self._await_database(
                self.database.purge_user_data, self.user_id, owner_platform_id
            )

    async def close(self) -> None:
        """Wait for active operations, then close the shared cloud client exactly once."""

        async def close_locked() -> None:
            async with self.lock:
                if self._closed:
                    return
                self._closed = True
                await self.adapter.close()

        await self._await_completion(close_locked())

    async def sync(
        self, days: int, data_types: set[str] | None = None
    ) -> dict[str, object]:
        """Download an overlap window and return exact insert/update counters.

        Args:
            days: Requested historical range; always bounded for reliability.
            data_types: Optional subset needed by a natural-language question.

        Returns:
            Summary suitable for a private command reply.
        """
        days = max(1, min(int(days), 90))
        allowed_types = (
            set(self.DATA_TYPES)
            if data_types is None
            else self.DATA_TYPES.intersection(data_types)
        )
        if not allowed_types:
            raise ValueError("没有可同步的健康数据类型")
        async with self.lock:
            self._ensure_open()
            deadline = asyncio.get_running_loop().time() + SYNC_TIMEOUT_SECONDS
            try:
                return await self._sync_locked(days, allowed_types, deadline)
            except TimeoutError as error:
                raise RuntimeError(
                    "小米健康云读取超过 5 分钟安全时限，已停止继续下载；"
                    "已经开始的本地数据库事务均已等待完成"
                ) from error

    async def _sync_locked(
        self, days: int, allowed_types: set[str], deadline: float
    ) -> dict[str, object]:
        """Run one bounded sync while the caller owns the operation lock."""
        if not await self._ensure_connected(deadline):
            reason = self.adapter.last_error or "小米健康云连接失败"
            if getattr(self.adapter, "authentication_failed", False):
                raise MiFitnessAuthenticationError(reason)
            raise RuntimeError(reason)
        end = datetime.now(UTC)
        start = end - timedelta(days=days + 2)  # delayed uploads and corrections
        counters = {"added": 0, "updated": 0, "errors": 0}
        details: dict[str, dict[str, object]] = {}
        first_error = ""
        for data_type, iterator in (
            ("daily_activity", self.adapter.iter_daily_activity(start, end)),
            ("heart_rate", self.adapter.iter_heart_rate(start, end)),
            ("body_measurements", self.adapter.iter_body_measurements(start, end)),
            ("sleep", self.adapter.iter_sleep(start, end)),
            ("spo2", self.adapter.iter_spo2(start, end)),
            ("stress", self.adapter.iter_stress(start, end)),
        ):
            if data_type not in allowed_types:
                continue
            try:
                records = await self._await_cloud(
                    self._collect_records(iterator, data_type), deadline
                )
                if data_type == "daily_activity":
                    outcome = await self._await_database(
                        self.database.replace_activity_records,
                        self.user_id,
                        records,
                        getattr(self.adapter, "user_timezone", UTC),
                    )
                else:
                    outcome = await self._await_database(
                        self.database.upsert_many,
                        self.user_id,
                        data_type,
                        records,
                    )
                latest = max(
                    (
                        getattr(record, "timestamp", None)
                        or getattr(record, "collected_at", None)
                        or getattr(record, "end_at", None)
                        for record in records
                    ),
                    default=None,
                )
                await self._await_database(
                    self.database.update_sync_state,
                    self.user_id,
                    data_type,
                    latest,
                )
                counters["added"] += outcome["added"]
                counters["updated"] += outcome["updated"]
                details[data_type] = {"fetched": len(records), **outcome}
            except MiFitnessRateLimitError as error:
                reason = redact_error(error)
                await self._await_database(
                    self.database.update_sync_failure,
                    self.user_id,
                    data_type,
                    reason,
                )
                raise
            except MiFitnessAuthenticationError:
                await self._await_database(
                    self.database.update_sync_failure,
                    self.user_id,
                    data_type,
                    "小米健康云授权已失效",
                )
                raise
            except TimeoutError:
                await self._await_database(
                    self.database.update_sync_failure,
                    self.user_id,
                    data_type,
                    "小米健康云读取超时",
                )
                raise
            except Exception as error:
                counters["errors"] += 1
                reason = redact_error(error)
                first_error = first_error or reason
                details[data_type] = {"error": reason}
                await self._await_database(
                    self.database.update_sync_failure,
                    self.user_id,
                    data_type,
                    reason,
                )
        if counters["errors"] == len(details):
            raise RuntimeError(
                f"所有健康数据集同步失败：{first_error or '未知云端错误'}"
            )
        pruned = await self._await_database(
            self.database.prune_user_data,
            self.user_id,
            self.retention_days,
            getattr(self.adapter, "user_timezone", UTC),
            self.owner_platform_id,
        )
        return {
            **counters,
            "types": len(details),
            "days": days,
            "details": details,
            "pruned": pruned,
        }

    @staticmethod
    async def _collect_records(iterator, data_type: str) -> list[object]:
        """Collect one cloud dataset before starting its local transaction."""
        records = []
        async for record in iterator:
            records.append(record)
            if len(records) > MAX_RECORDS_PER_DATASET:
                raise RuntimeError(f"{data_type} 超过单次同步记录安全上限")
        return records

    @staticmethod
    async def _await_cloud(awaitable, deadline: float):
        """Bound cancellable cloud work without cancelling SQLite worker threads."""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError
        return await asyncio.wait_for(awaitable, timeout=remaining)

    @staticmethod
    async def _await_completion(awaitable):
        """Finish owned cleanup/work before propagating an outer cancellation."""
        task = asyncio.ensure_future(awaitable)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _await_database(self, operation, *args):
        """Never abandon a running SQLite thread or release the operation lock early."""
        return await self._await_completion(asyncio.to_thread(operation, *args))

    def _region_metadata_key(self) -> str:
        """Namespace cached discovery without storing the Xiaomi userId in the key."""
        digest = hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:24]
        return f"xiaomi_region:{digest}"

    async def _persist_region(self) -> None:
        """Persist only a validated region after a successful connection."""
        region = str(getattr(self.adapter, "region", "")).strip().lower()
        if region not in KNOWN_REGIONS:
            return
        await self._await_database(
            self.database.set_metadata, self._region_metadata_key(), region
        )

    async def _ensure_connected(self, deadline: float | None = None) -> bool:
        """Share connection and region-cache behavior across all cloud entrypoints."""
        if self.adapter.is_connected():
            await self._persist_region()
            return True
        connected = (
            await self.adapter.connect()
            if deadline is None
            else await self._await_cloud(self.adapter.connect(), deadline)
        )
        if connected:
            await self._persist_region()
        return connected

    def _ensure_open(self) -> None:
        """Reject new operations after plugin shutdown has claimed the client."""
        if self._closed:
            raise RuntimeError("小米健康同步服务已关闭")
