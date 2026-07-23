"""Thread-backed private health queries and concise Chinese formatting data."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..storage import Database
from ..utils import local_timestamp


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

    def local_day_bounds(self, value: date) -> tuple[str, str]:
        """Return UTC ISO boundaries for one user-local calendar day."""
        start = datetime.combine(value, time.min, tzinfo=self.timezone)
        end = start + timedelta(days=1)
        return start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()

    async def heart_rates_for_local_day(self, value: date) -> list[dict]:
        """Return all samples belonging to one local calendar day."""
        start, end = self.local_day_bounds(value)
        return await asyncio.to_thread(
            self.database.heart_rates_between, self.user_id, start, end
        )

    async def heart_rates_for_range(self, start_day: date, end_day: date) -> list[dict]:
        """Return all samples from local ``start_day`` up to ``end_day``."""
        start, _ = self.local_day_bounds(start_day)
        end, _ = self.local_day_bounds(end_day)
        return await asyncio.to_thread(
            self.database.heart_rates_between, self.user_id, start, end
        )

    async def today_summary(self) -> tuple[dict | None, list[dict], dict | None]:
        """Fetch activity and complete local-day heart-rate statistics."""
        today = datetime.now(self.timezone).date()
        return (
            await asyncio.to_thread(
                self.database.today_activity, self.user_id, today.isoformat()
            ),
            await self.heart_rates_for_local_day(today),
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
        """Return trend rows with heart rates grouped by local calendar day."""
        days = max(1, min(days, 90))
        end = datetime.now(self.timezone).date()
        start = end - timedelta(days=days - 1)
        activities, rates = await asyncio.gather(
            asyncio.to_thread(
                self.database.trend,
                self.user_id,
                start.isoformat(),
                end.isoformat(),
            ),
            self.heart_rates_for_range(start, end + timedelta(days=1)),
        )
        passive_by_day: dict[str, list[int]] = {}
        for row in rates:
            if row["is_workout"]:
                continue
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=UTC)
                local_day = timestamp.astimezone(self.timezone).date().isoformat()
            except (TypeError, ValueError):
                continue
            passive_by_day.setdefault(local_day, []).append(row["bpm"])
        for row in activities:
            values = passive_by_day.get(row["date"], [])
            row["avg_heart_rate"] = sum(values) / len(values) if values else None
        return activities

    async def latest_sync_at(self) -> str | None:
        """Return latest synchronization marker."""
        return await asyncio.to_thread(self.database.latest_sync_at)

    def display_timestamp(self, value: object) -> str:
        """Format one stored timestamp in the configured user timezone."""
        return local_timestamp(value, self.timezone)

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
        explicitly_requested = any(requested.values())
        if not explicitly_requested:
            requested = {key: True for key in requested}
        today = datetime.now(self.timezone).date()
        if "昨天" in compact or "昨日" in compact:
            heart_day = today - timedelta(days=1)
            heart_label = "昨日"
        elif "最近" in compact or "近" in compact or "这两天" in compact:
            heart_day = None
            heart_label = "最近 48 小时"
        else:
            # Questions such as “我心率怎么样” generally mean today's
            # reading.  Use the same local-day boundary as the Mi Fitness app.
            heart_day = today
            heart_label = "今日"
        rate_query = (
            self.heart_rates_for_local_day(heart_day)
            if heart_day is not None
            else asyncio.to_thread(
                self.database.heart_rates_since,
                self.user_id,
                (datetime.now(UTC) - timedelta(hours=48)).isoformat(),
                100,
            )
        )
        activities, rates, measurement, sleeps, spo2, stress = await asyncio.gather(
            asyncio.to_thread(
                self.database.recent_activity, self.user_id, self.today()
            ),
            rate_query,
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
                f"{heart_label}心率：最新 {rates[0]['bpm']} bpm（数据采集时间 {self.display_timestamp(rates[0]['timestamp'])}），平均 {sum(values) / len(values):.0f}，最高 {max(values)}，最低 {min(values)}"
            )
        if requested["body"] and measurement:
            parts.append(
                f"最近体重：{measurement['weight_kg']} kg（数据采集时间 {self.display_timestamp(measurement['timestamp'])}）"
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
        elif requested["sleep"] and explicitly_requested:
            parts.append(
                "睡眠：本地缓存暂无已同步记录；这不代表设备不支持或手机端无法同步"
            )
        if requested["spo2"] and spo2:
            parts.append(
                f"最近血氧：{spo2['percent']}%（数据采集时间 {self.display_timestamp(spo2['timestamp'])}）"
            )
        if requested["stress"] and stress:
            parts.append(
                f"最近压力分数：{stress['score']}（数据采集时间 {self.display_timestamp(stress['timestamp'])}）"
            )
        return "；".join(parts) or "暂无所查询项目的已同步云端数据"
