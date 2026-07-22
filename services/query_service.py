"""Thread-backed private health queries and concise Chinese formatting data."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
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
            # Windows/Python builds may not bundle the IANA tz database.  A
            # fixed +08:00 fallback keeps the documented default usable; DST
            # aware zones remain available whenever ZoneInfo can load them.
            self.timezone = timezone(timedelta(hours=8), name="Asia/Shanghai")

    def today(self) -> str:
        """Return user's local date."""
        return datetime.now(self.timezone).date().isoformat()

    async def today_summary(self) -> tuple[dict | None, list[dict], dict | None]:
        """Fetch activity, last-day heart rate, and latest measurement."""
        # Stored cloud timestamps are UTC ISO strings; compare like with like.
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        return (
            await asyncio.to_thread(
                self.database.today_activity, self.user_id, self.today()
            ),
            await asyncio.to_thread(
                self.database.heart_rates_since, self.user_id, cutoff
            ),
            await asyncio.to_thread(self.database.latest_measurement, self.user_id),
        )

    async def heart_rates(self, hours: int) -> list[dict]:
        """Return bounded recent records."""
        cutoff = (
            datetime.now(UTC) - timedelta(hours=max(1, min(hours, 168)))
        ).isoformat()
        return await asyncio.to_thread(
            self.database.heart_rates_since, self.user_id, cutoff
        )

    async def body(self) -> dict | None:
        """Return newest body measurement."""
        return await asyncio.to_thread(self.database.latest_measurement, self.user_id)

    async def trend(self, days: int) -> list[dict]:
        """Return bounded daily trend rows."""
        days = max(1, min(days, 90))
        end = datetime.now(self.timezone).date()
        return await asyncio.to_thread(
            self.database.trend,
            self.user_id,
            (end - timedelta(days=days - 1)).isoformat(),
            end.isoformat(),
        )

    async def latest_sync_at(self) -> str | None:
        """Return latest synchronization marker."""
        return await asyncio.to_thread(self.database.latest_sync_at)

    async def care_snapshot(self, focus: str = "") -> str:
        """Return only health categories relevant to an owner conversation."""
        compact = focus.lower().replace(" ", "")
        requested = {
            "activity": any(
                word in compact
                for word in ("步", "走", "运动", "活动", "距离", "热量", "卡路里")
            ),
            "heart": any(word in compact for word in ("心率", "心跳", "bpm")),
            "body": any(
                word in compact
                for word in (
                    "体重",
                    "体脂",
                    "bmi",
                    "肌肉",
                    "水分",
                    "骨量",
                    "代谢",
                    "身体年龄",
                )
            ),
            "sleep": any(word in compact for word in ("睡", "失眠", "入睡", "醒")),
            "spo2": any(word in compact for word in ("血氧", "spo2")),
            "stress": any(word in compact for word in ("压力", "焦虑", "stress")),
        }
        if not any(requested.values()):
            requested = {key: True for key in requested}
        activities, rates, measurement, sleeps, spo2, stress = await asyncio.gather(
            asyncio.to_thread(
                self.database.recent_activity, self.user_id, self.today()
            ),
            asyncio.to_thread(
                self.database.heart_rates_since,
                self.user_id,
                (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
                100,
            ),
            asyncio.to_thread(self.database.latest_measurement, self.user_id),
            asyncio.to_thread(self.database.recent_sleep, self.user_id),
            asyncio.to_thread(
                self.database.latest_metric, "spo2_samples", self.user_id
            ),
            asyncio.to_thread(
                self.database.latest_metric, "stress_samples", self.user_id
            ),
        )
        parts = []
        if requested["activity"]:
            for activity in activities:
                parts.append(
                    f"{activity['date']} 活动：{activity['steps']} 步，{activity['distance_m']:.0f} m，活动消耗 {activity['active_kcal']:.0f} kcal"
                )
        if requested["heart"] and rates:
            values = [row["bpm"] for row in rates]
            parts.append(
                f"最近 48 小时心率：最新 {rates[0]['bpm']} bpm（采集 {rates[0]['timestamp']}），平均 {sum(values) / len(values):.0f}，最高 {max(values)}，最低 {min(values)}"
            )
        if requested["body"] and measurement:
            parts.append(
                f"最近体重：{measurement['weight_kg']} kg（采集 {measurement['timestamp']}）"
            )
        if requested["sleep"] and sleeps:
            values = []
            for sleep in sleeps:
                ended = datetime.fromisoformat(sleep["end_at"]).astimezone(
                    self.timezone
                )
                score = sleep["score"] if sleep["score"] is not None else "未提供"
                values.append(
                    f"{ended.date()} 睡眠 {sleep['asleep_minutes']} 分钟（结束 {ended.strftime('%H:%M')}，评分 {score}）"
                )
            parts.append("；".join(values))
        if requested["spo2"] and spo2:
            parts.append(f"最近血氧：{spo2['percent']}%（采集 {spo2['timestamp']}）")
        if requested["stress"] and stress:
            parts.append(
                f"最近压力分数：{stress['score']}（采集 {stress['timestamp']}）"
            )
        return "；".join(parts) or "暂无所查询项目的已同步云端数据"
