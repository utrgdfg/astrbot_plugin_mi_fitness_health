"""Thread-backed private health queries and concise Chinese formatting data."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ..storage import Database
from ..utils import local_timestamp


class QueryService:
    """Read cached cloud records without blocking AstrBot's event loop."""

    CATEGORY_SYNC_TYPES = {
        "activity": "daily_activity",
        "heart": "heart_rate",
        "body": "body_measurements",
        "sleep": "sleep",
        "spo2": "spo2",
        "stress": "stress",
    }
    CATEGORY_FOCUS_KEYWORDS = {
        "activity": (
            "步数",
            "步行",
            "计步",
            "散步",
            "跑步",
            "多少步",
            "几步",
            "走",
            "运动",
            "活动",
            "距离",
            "热量",
            "卡路里",
        ),
        "heart": ("心率", "心跳", "bpm"),
        "body": (
            "身体数据",
            "身体成分",
            "体重",
            "体脂",
            "bmi",
            "肌肉",
            "水分",
            "骨量",
            "代谢",
            "身体年龄",
        ),
        "sleep": ("睡", "失眠", "入睡", "醒", "熬夜", "通宵", "补觉", "午觉"),
        "spo2": ("血氧", "spo2"),
        "stress": ("压力", "焦虑", "stress"),
    }
    CATEGORY_FOCUS_LABELS = {
        "activity": "活动",
        "heart": "心率",
        "body": "身体数据",
        "sleep": "睡眠",
        "spo2": "血氧",
        "stress": "压力",
    }
    COMPREHENSIVE_FOCUS_CUES = (
        "综合概况",
        "综合情况",
        "综合状态",
        "整体概况",
        "整体情况",
        "整体状态",
        "总体概况",
        "总体情况",
        "总体状态",
        "健康概况",
        "健康全貌",
        "全部健康数据",
        "所有健康数据",
        "全部身体数据",
        "所有身体数据",
        "全部数据",
        "所有数据",
    )

    def __init__(self, database: Database, user_id: str, timezone_name: str):
        """Create a query service using a user-local timezone.

        Args:
            database: Local persistent store.
            user_id: Configured account identifier.
            timezone_name: IANA timezone, falling back safely to Asia/Shanghai.
        """
        self.database = database
        self.user_id = user_id
        requested_timezone = str(timezone_name or "Asia/Shanghai").strip()
        requested_timezone = requested_timezone or "Asia/Shanghai"
        self.invalid_timezone_name: str | None = None
        self.timezone_fallback_used = False
        try:
            self.timezone = ZoneInfo(requested_timezone)
        except Exception:
            # Windows/Python builds may not bundle the IANA tz database.  A
            # fixed +08:00 fallback keeps the documented default usable; DST
            # aware zones remain available whenever ZoneInfo can load them.
            self.timezone = timezone(timedelta(hours=8), name="Asia/Shanghai")
            self.timezone_fallback_used = True
            if requested_timezone != "Asia/Shanghai":
                self.invalid_timezone_name = " ".join(requested_timezone.split())[:128]

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

    async def latest_sync_at(
        self, data_types: tuple[str, ...] | None = None
    ) -> str | None:
        """Return global latest sync or freshness shared by required datasets."""
        return await asyncio.to_thread(
            self.database.latest_sync_at, self.user_id, data_types
        )

    async def latest_failure_at(self, data_types: tuple[str, ...]) -> str | None:
        """Return the newest unresolved failure for natural-query backoff."""
        return await asyncio.to_thread(
            self.database.latest_sync_failure_at, self.user_id, data_types
        )

    @staticmethod
    def _compact_focus(focus: object) -> str:
        """Normalize a bounded focus string for deterministic category matching."""
        return "".join(str(focus or "").lower().split())

    @classmethod
    def requested_categories(cls, focus: str) -> tuple[dict[str, bool], bool]:
        """Map natural wording to the smallest required health categories."""
        compact = cls._compact_focus(focus)
        requested = {
            category: any(word in compact for word in keywords)
            for category, keywords in cls.CATEGORY_FOCUS_KEYWORDS.items()
        }
        # Keep the historical command/query shorthand without exposing the
        # ambiguous single character to the stricter LLM focus parser.
        requested["activity"] = requested["activity"] or "步" in compact
        explicitly_requested = any(requested.values())
        if not explicitly_requested:
            requested = {key: True for key in requested}
        return requested, explicitly_requested

    @classmethod
    def llm_categories_for_focus(cls, focus: str) -> tuple[str, ...]:
        """Return a fail-closed, minimal category set for an LLM-generated focus.

        Ordinary model output is limited to the first two explicitly mentioned
        categories.  Only an unambiguous Chinese comprehensive-data cue may
        request every category.  Empty, unknown, or generic English wording
        returns no categories instead of expanding to all sensitive records.
        """
        compact = cls._compact_focus(focus)
        if not compact:
            return ()
        if any(cue in compact for cue in cls.COMPREHENSIVE_FOCUS_CUES):
            return tuple(cls.CATEGORY_SYNC_TYPES)

        matches: list[tuple[int, int, str]] = []
        for declaration_order, (category, keywords) in enumerate(
            cls.CATEGORY_FOCUS_KEYWORDS.items()
        ):
            positions = [
                position for word in keywords if (position := compact.find(word)) >= 0
            ]
            if positions:
                matches.append((min(positions), declaration_order, category))
        matches.sort()
        return tuple(category for _, _, category in matches[:2])

    @classmethod
    def normalize_llm_focus(cls, focus: str) -> str:
        """Convert an LLM focus into a safe focus accepted by existing queries."""
        categories = cls.llm_categories_for_focus(focus)
        if not categories:
            return ""
        compact = cls._compact_focus(focus)
        if "昨天" in compact or "昨日" in compact:
            scope = "昨天"
        elif "最近" in compact or "近" in compact or "这两天" in compact:
            scope = "最近"
        elif "今天" in compact or "今日" in compact:
            scope = "今天"
        else:
            scope = ""
        labels = (cls.CATEGORY_FOCUS_LABELS[category] for category in categories)
        return " ".join(part for part in (scope, *labels) if part)

    def llm_sync_types_for_focus(self, focus: str) -> tuple[str, ...]:
        """Return only cloud datasets authorized by a validated LLM focus."""
        return tuple(
            self.CATEGORY_SYNC_TYPES[category]
            for category in self.llm_categories_for_focus(focus)
        )

    async def llm_care_snapshot(
        self,
        focus: str,
        *,
        include_missing_notice: bool = False,
    ) -> str:
        """Return a minimal LLM snapshot without changing command query behavior."""
        safe_focus = self.normalize_llm_focus(focus)
        if not safe_focus:
            return ""
        return await self.care_snapshot(
            safe_focus,
            include_missing_notice=include_missing_notice,
        )

    def sync_types_for_focus(self, focus: str) -> tuple[str, ...]:
        """Return storage sync keys needed to answer one natural-language focus."""
        requested, _ = self.requested_categories(focus)
        return tuple(
            self.CATEGORY_SYNC_TYPES[key]
            for key, enabled in requested.items()
            if enabled
        )

    async def sync_at_for_focus(self, focus: str) -> str | None:
        """Return the oldest valid success among every dataset required by focus."""
        return await self.latest_sync_at(self.sync_types_for_focus(focus))

    def display_timestamp(self, value: object) -> str:
        """Format one stored timestamp in the configured user timezone."""
        return local_timestamp(value, self.timezone)

    @staticmethod
    def _sleep_clock_label(value: datetime) -> str:
        """Make 24-hour sleep times unambiguous to a conversational model."""
        hour = value.hour
        if hour == 0:
            period = "午夜"
        elif hour < 6:
            period = "凌晨"
        elif hour < 12:
            period = "早上"
        elif hour < 14:
            period = "中午"
        elif hour < 18:
            period = "下午"
        else:
            period = "晚上"
        hour_12 = hour % 12 or 12
        minute_text = f"{value.minute:02d}分" if value.minute else ""
        return (
            f"{value.strftime('%Y-%m-%d %H:%M')}"
            f"（{period}{hour_12}点{minute_text}，24小时制）"
        )

    @staticmethod
    def _sleep_duration_label(minutes: int) -> str:
        """Keep exact minutes while adding an easy-to-read duration."""
        hours, remainder = divmod(minutes, 60)
        readable = ""
        if hours:
            readable += f"{hours} 小时"
        if remainder:
            readable += (" " if readable else "") + f"{remainder} 分钟"
        return f"{minutes} 分钟（{readable or '0 分钟'}，核心参考）"

    def _format_sleep_row(self, sleep: dict) -> str | None:
        """Format explicit local sleep boundaries, skipping malformed history."""
        try:
            started = datetime.fromisoformat(str(sleep["start_at"]))
            ended = datetime.fromisoformat(str(sleep["end_at"]))
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
            if ended.tzinfo is None:
                ended = ended.replace(tzinfo=UTC)
            started = started.astimezone(self.timezone)
            ended = ended.astimezone(self.timezone)
            score = sleep["score"] if sleep["score"] is not None else "未提供"
            asleep_minutes = int(sleep["asleep_minutes"])
            if not 0 <= asleep_minutes <= 24 * 60:
                return None
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            OSError,
        ):
            return None
        crosses_date = "是" if started.date() != ended.date() else "否"
        return (
            f"{ended.date()} 睡眠时长 {self._sleep_duration_label(asleep_minutes)}；"
            f"入睡 {self._sleep_clock_label(started)}；"
            f"起床 {self._sleep_clock_label(ended)}；跨日 {crosses_date}；评分 {score}。"
            "判断睡眠情况时以睡眠时长为主，入睡与起床时刻只作辅助；"
            "凌晨时刻仍按 24 小时制理解"
        )

    async def care_snapshot(
        self,
        focus: str = "",
        *,
        include_missing_notice: bool = True,
    ) -> str:
        """Return relevant records, optionally omitting all missing-data notices."""
        compact = focus.lower().replace(" ", "")
        requested, explicitly_requested = self.requested_categories(focus)
        today = datetime.now(self.timezone).date()
        if "昨天" in compact or "昨日" in compact:
            target_day = today - timedelta(days=1)
            heart_day = target_day
            heart_label = "昨日"
        elif "最近" in compact or "近" in compact or "这两天" in compact:
            target_day = None
            heart_day = None
            heart_label = "最近 48 小时"
        else:
            target_day = today if "今天" in compact or "今日" in compact else None
            # Questions such as “我心率怎么样” generally mean today's
            # reading.  Use the same local-day boundary as the Mi Fitness app.
            heart_day = today
            heart_label = "今日"
        now_utc = datetime.now(UTC)
        rate_query = (
            self.heart_rates_for_local_day(heart_day)
            if heart_day is not None
            else asyncio.to_thread(
                self.database.heart_rates_between,
                self.user_id,
                (now_utc - timedelta(hours=48)).isoformat(),
                now_utc.isoformat(),
            )
        )
        if target_day is not None:
            target_start, target_end = self.local_day_bounds(target_day)
            activity_query = asyncio.to_thread(
                self.database.recent_activity_between,
                self.user_id,
                target_day.isoformat(),
                target_day.isoformat(),
                1,
            )
            measurement_query = asyncio.to_thread(
                self.database.latest_measurement_between,
                self.user_id,
                target_start,
                target_end,
            )
            sleep_query = asyncio.to_thread(
                self.database.sleep_ending_between,
                self.user_id,
                target_start,
                target_end,
            )
            spo2_query = asyncio.to_thread(
                self.database.latest_metric_between,
                "spo2_samples",
                self.user_id,
                target_start,
                target_end,
            )
            stress_query = asyncio.to_thread(
                self.database.latest_metric_between,
                "stress_samples",
                self.user_id,
                target_start,
                target_end,
            )
        elif "最近" in compact or "近" in compact or "这两天" in compact:
            recent_start = (datetime.now(UTC) - timedelta(days=7)).isoformat()
            recent_end = datetime.now(UTC).isoformat()
            activity_query = asyncio.to_thread(
                self.database.recent_activity_between,
                self.user_id,
                (today - timedelta(days=6)).isoformat(),
                today.isoformat(),
                7,
            )
            measurement_query = asyncio.to_thread(
                self.database.latest_measurement_between,
                self.user_id,
                recent_start,
                recent_end,
            )
            sleep_query = asyncio.to_thread(
                self.database.sleep_ending_between,
                self.user_id,
                recent_start,
                recent_end,
            )
            spo2_query = asyncio.to_thread(
                self.database.latest_metric_between,
                "spo2_samples",
                self.user_id,
                recent_start,
                recent_end,
            )
            stress_query = asyncio.to_thread(
                self.database.latest_metric_between,
                "stress_samples",
                self.user_id,
                recent_start,
                recent_end,
            )
        else:
            activity_query = asyncio.to_thread(
                self.database.recent_activity_between,
                self.user_id,
                (today - timedelta(days=6)).isoformat(),
                today.isoformat(),
                2,
            )
            measurement_query = asyncio.to_thread(
                self.database.latest_measurement, self.user_id
            )
            sleep_query = asyncio.to_thread(self.database.recent_sleep, self.user_id)
            spo2_query = asyncio.to_thread(
                self.database.latest_metric, "spo2_samples", self.user_id
            )
            stress_query = asyncio.to_thread(
                self.database.latest_metric, "stress_samples", self.user_id
            )
        activities, rates, measurement, sleeps, spo2, stress = await asyncio.gather(
            activity_query,
            rate_query,
            measurement_query,
            sleep_query,
            spo2_query,
            stress_query,
        )
        parts = []
        day_label = (
            "昨日"
            if target_day == today - timedelta(days=1)
            else "今日"
            if target_day == today
            else "最近"
        )
        if requested["activity"]:
            for activity in activities:
                parts.append(
                    f"{activity['date']} 活动：{activity['steps']} 步，{activity['distance_m']:.0f} m，活动消耗 {activity['active_kcal']:.0f} kcal"
                )
        if requested["heart"] and rates:
            ordinary_rates = [row for row in rates if not bool(row["is_workout"])]
            summarized_rates = ordinary_rates or rates
            values = [row["bpm"] for row in summarized_rates]
            label = heart_label if ordinary_rates else f"{heart_label}运动期间"
            parts.append(
                f"{label}心率：最新 {summarized_rates[0]['bpm']} bpm（数据采集时间 {self.display_timestamp(summarized_rates[0]['timestamp'])}），平均 {sum(values) / len(values):.0f}，最高 {max(values)}，最低 {min(values)}"
            )
        if requested["body"] and measurement:
            parts.append(
                f"{day_label}体重：{measurement['weight_kg']} kg（数据采集时间 {self.display_timestamp(measurement['timestamp'])}）"
            )
        if requested["sleep"]:
            values = [
                value
                for sleep in sleeps
                if (value := self._format_sleep_row(sleep)) is not None
            ]
            if values:
                parts.append("；".join(values))
            elif target_day == today and include_missing_notice:
                recent_sleeps = await asyncio.to_thread(
                    self.database.recent_sleep,
                    self.user_id,
                    1,
                )
                recent_value = (
                    self._format_sleep_row(recent_sleeps[0]) if recent_sleeps else None
                )
                message = "今日睡眠：当前缓存中尚未出现以今天起床时间结束的云端记录"
                if recent_value:
                    message += (
                        f"；最近一条为 {recent_value}，仅供历史参考，"
                        "不能作为今天刚醒后的状态"
                    )
                else:
                    message += "；这可能是小米云仍在生成或上传本次睡眠汇总"
                parts.append(message)
            elif explicitly_requested and include_missing_notice:
                parts.append(
                    "睡眠：本地缓存暂无已同步记录；这不代表设备不支持或手机端无法同步"
                )
        if requested["spo2"] and spo2:
            parts.append(
                f"{day_label}血氧：{spo2['percent']}%（数据采集时间 {self.display_timestamp(spo2['timestamp'])}）"
            )
        if requested["stress"] and stress:
            parts.append(
                f"{day_label}压力分数：{stress['score']}（数据采集时间 {self.display_timestamp(stress['timestamp'])}）"
            )
        if parts:
            return "；".join(parts)
        return "暂无所查询项目的已同步云端数据" if include_missing_notice else ""
