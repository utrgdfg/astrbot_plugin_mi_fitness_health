"""Thread-backed private health queries and concise Chinese formatting data."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..storage import Database


class QueryService:
    """Read cached cloud records without blocking AstrBot's event loop."""

    def __init__(self, database: Database, user_id: str, timezone_name: str):
        """Create a query service using a user-local timezone.

        Args:
            database: Local persistent store.
            user_id: Configured account identifier.
            timezone_name: IANA timezone, falling back safely to Asia/Shanghai.
        """
        self.database = database
        self.user_id = user_id
        try:
            self.timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
        except Exception:
            self.timezone = ZoneInfo("Asia/Shanghai")

    def today(self) -> str:
        """Return user's local date."""
        return datetime.now(self.timezone).date().isoformat()

    async def today_summary(self) -> tuple[dict | None, list[dict], dict | None]:
        """Fetch activity, last-day heart rate, and latest measurement."""
        cutoff = (datetime.now(self.timezone) - timedelta(hours=24)).isoformat()
        return (
            await asyncio.to_thread(self.database.today_activity, self.user_id, self.today()),
            await asyncio.to_thread(self.database.heart_rates_since, self.user_id, cutoff),
            await asyncio.to_thread(self.database.latest_measurement, self.user_id),
        )

    async def heart_rates(self, hours: int) -> list[dict]:
        """Return bounded recent records."""
        cutoff = (datetime.now(self.timezone) - timedelta(hours=max(1, min(hours, 168)))).isoformat()
        return await asyncio.to_thread(self.database.heart_rates_since, self.user_id, cutoff)

    async def body(self) -> dict | None:
        """Return newest body measurement."""
        return await asyncio.to_thread(self.database.latest_measurement, self.user_id)

    async def trend(self, days: int) -> list[dict]:
        """Return bounded daily trend rows."""
        days = max(1, min(days, 90))
        end = datetime.now(self.timezone).date()
        return await asyncio.to_thread(self.database.trend, self.user_id, (end - timedelta(days=days - 1)).isoformat(), end.isoformat())

    async def latest_sync_at(self) -> str | None:
        """Return latest synchronization marker."""
        return await asyncio.to_thread(self.database.latest_sync_at)

    async def care_snapshot(self) -> str:
        """Return a minimal cached summary suitable for one owner-only LLM turn."""
        sleep, spo2, stress = await asyncio.gather(
            asyncio.to_thread(self.database.latest_sleep, self.user_id),
            asyncio.to_thread(self.database.latest_metric, "spo2_samples", self.user_id),
            asyncio.to_thread(self.database.latest_metric, "stress_samples", self.user_id),
        )
        parts = []
        if sleep: parts.append(f"最近睡眠：{sleep['asleep_minutes']} 分钟（结束 {sleep['end_at']}，评分 {sleep['score'] if sleep['score'] is not None else '未提供'}）")
        if spo2: parts.append(f"最近血氧：{spo2['percent']}%（采集 {spo2['timestamp']}）")
        if stress: parts.append(f"最近压力分数：{stress['score']}（采集 {stress['timestamp']}）")
        return "；".join(parts) or "暂无睡眠、血氧或压力的已同步云端数据"
