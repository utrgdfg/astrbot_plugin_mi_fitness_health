"""Native AstrBot plugin for private Xiaomi Mi Fitness cloud health data."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from .adapters import MiFitnessCloudAdapter
from .services import AlertService, QueryService, SyncService
from .storage import Database
from .utils import measurement_text, today_text
from .utils.privacy import redact_error

logger = logging.getLogger(__name__)


class MiFitnessHealthPlugin(Star):
    """Own cloud lifecycle, local storage, and owner-only health commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        """Configure one Xiaomi account and one AstrBot data owner.

        Args:
            context: AstrBot runtime context.
            config: Values supplied by AstrBot's plugin configuration page.
        """
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(self.name))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.user_id = str(config.get("user_id") or os.getenv("MI_FITNESS_USER_ID", "")).strip()
        self.pass_token = str(config.get("pass_token") or os.getenv("MI_FITNESS_PASS_TOKEN", "")).strip()
        self.owner_platform_id = str(config.get("owner_platform_id") or "").strip()
        database_path = str(config.get("database_path") or "").strip()
        self.database = Database(Path(database_path) if database_path else self.data_dir / "mi_fitness_health.sqlite3")
        self.adapter = MiFitnessCloudAdapter(self.user_id, self.pass_token, str(config.get("region") or "").strip())
        self.sync_service = SyncService(self.adapter, self.database, self.user_id)
        self.query_service = QueryService(self.database, self.user_id, str(config.get("user_timezone") or "Asia/Shanghai"))
        self.alert_service = AlertService(
            self.database, self.user_id, int(config.get("heart_rate_high") or 0), int(config.get("heart_rate_low") or 0),
            int(config.get("alert_consecutive_count") or 3), int(config.get("alert_cooldown_minutes") or 120),
        )
        self.auto_sync_enabled = bool(config.get("enable_auto_sync", True))
        self.health_alerts_enabled = bool(config.get("enable_health_alerts", False))
        self.care_dialogue_enabled = bool(config.get("enable_care_dialogue", True))
        self.sync_days = max(1, min(int(config.get("default_sync_days") or 7), 90))
        self.sync_interval = max(5, int(config.get("sync_interval_minutes") or 60))
        self._auto_task: asyncio.Task[None] | None = None
        self._auto_sync_paused = False

    async def initialize(self) -> None:
        """Migrate the database and schedule one guarded background loop."""
        await self.sync_service.initialize()
        if self.auto_sync_enabled and self.user_id and self.pass_token and not self._auto_task:
            self._auto_task = asyncio.create_task(self._auto_sync_loop(), name=f"{self.name}-auto-sync")

    async def terminate(self) -> None:
        """Cancel the periodic task and close plugin-owned HTTP resources."""
        if self._auto_task:
            self._auto_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._auto_task
            self._auto_task = None
        await self.adapter.close()

    async def _auto_sync_loop(self) -> None:
        """Synchronize periodically without unbounded retries or parallel runs."""
        while not self._auto_sync_paused:
            try:
                await self._sync()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                reason = redact_error(error)
                logger.warning("Mi Fitness automatic sync paused: %s", reason)
                self._auto_sync_paused = True
                break
            await asyncio.sleep(self.sync_interval * 60)

    async def _sync(self) -> dict[str, int | str]:
        """Run one sync and persist eligible non-diagnostic health alerts."""
        summary = await self.sync_service.sync(self.sync_days)
        if self.health_alerts_enabled:
            alerts = await self.alert_service.evaluate()
            if alerts:
                logger.warning("Mi Fitness created %d private health reminder(s)", len(alerts))
        return summary

    def _authorized(self, event: AstrMessageEvent) -> bool:
        """Return whether the sender matches the one configured data owner."""
        return bool(self.owner_platform_id) and str(event.get_sender_id()) == self.owner_platform_id

    @filter.on_llm_request()
    async def add_owner_health_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """Let only the data owner receive gentle, non-diagnostic health-aware dialogue."""
        if not self.care_dialogue_enabled or not self._authorized(event):
            return
        snapshot = await self.query_service.care_snapshot()
        text = ("<private_health_context>\n" + snapshot + "\n"
                "These are delayed Xiaomi cloud records, not real-time monitoring. Only mention them when relevant to the owner's message; be caring, avoid diagnosis and do not claim medical certainty.\n</private_health_context>")
        part = TextPart(text=text)
        req.extra_user_content_parts.append(part.mark_as_temp() if hasattr(part, "mark_as_temp") else part)

    async def _guard(self, event: AstrMessageEvent):
        """Yield a refusal if the request does not belong to the configured owner."""
        if self._authorized(event):
            return
        message = "健康数据所有者尚未配置，请由管理员设置 owner_platform_id。" if not self.owner_platform_id else "此健康数据仅允许已配置的所有者查询。"
        yield event.plain_result(message)

    @filter.command("健康帮助")
    async def health_help(self, event: AstrMessageEvent):
        """Show commands and privacy boundaries."""
        yield event.plain_result(
            "小米运动健康（仅所有者可用）\n"
            "健康连接｜健康同步｜健康状态｜今日健康｜心率记录 [小时]｜身体数据｜健康趋势 [天]\n"
            "数据是小米健康云已同步的历史记录，所有展示均含采集时间；不是实时监护，也不构成医疗诊断。\n"
            "睡眠、运动记录、血氧、压力、设备电量：当前数据源暂未支持。"
        )

    @filter.command("健康连接")
    async def health_connection(self, event: AstrMessageEvent):
        """Authenticate and show only a credential-safe connection state."""
        async for result in self._guard(event):
            yield result
            return
        if not self.user_id or not self.pass_token:
            yield event.plain_result("未配置 user_id 或 pass_token。请在插件配置页填写后重新加载插件。")
            return
        if not await self.adapter.connect():
            yield event.plain_result(f"健康连接失败：{self.adapter.last_error or '未知错误'}\n遇到验证码、二次验证或风控时，请在浏览器完成验证后更新 Cookie。")
            return
        labels = {"daily_activity": "步数/距离/活动消耗", "heart_rate": "心率", "body_measurements": "体重/身体成分"}
        types = "、".join(labels[item] for item in self.adapter.get_available_data_types()) or "未发现最近 30 天数据"
        yield event.plain_result(f"健康连接成功\n区域：{self.adapter.region}\n可用数据：{types}\n不显示账号、Token、Cookie 或 ssecurity。")

    @filter.command("健康同步")
    async def health_sync(self, event: AstrMessageEvent):
        """Manually synchronize a bounded recent cloud-data window."""
        async for result in self._guard(event):
            yield result
            return
        try:
            result = await self._sync()
            yield event.plain_result(f"健康同步完成：{result['days']} 天范围，新增 {result['added']}，更新 {result['updated']}，数据类型 {result['types']}。")
        except Exception as error:
            yield event.plain_result(f"健康同步失败：{redact_error(error)}")

    @filter.command("今日健康")
    async def health_today(self, event: AstrMessageEvent):
        """Show cached user-local daily summary."""
        async for result in self._guard(event):
            yield result
            return
        activity, rates, measurement = await self.query_service.today_summary()
        yield event.plain_result(today_text(activity, rates, measurement) + "\n" + await self.query_service.care_snapshot())

    @filter.command("健康详情")
    async def health_details(self, event: AstrMessageEvent):
        """Show latest supported sleep, blood-oxygen, and stress cloud records."""
        async for result in self._guard(event):
            yield result
            return
        yield event.plain_result("健康详情（云端已同步数据，非实时）\n" + await self.query_service.care_snapshot())

    @filter.command("健康状态")
    async def health_status(self, event: AstrMessageEvent):
        """Show cache and synchronization status without exposing credentials."""
        async for result in self._guard(event):
            yield result
            return
        last_sync = await self.query_service.latest_sync_at()
        yield event.plain_result(
            f"健康状态\n连接：{'已连接' if self.adapter.is_connected() else '未连接/待验证'}\n"
            f"区域：{self.adapter.region or '自动探测'}\n最近同步：{last_sync or '暂无'}\n"
            f"自动同步：{'已暂停（请重新授权后重载）' if self._auto_sync_paused else ('开启' if self.auto_sync_enabled else '关闭')}"
        )

    @filter.command("心率记录")
    async def heart_rate_records(self, event: AstrMessageEvent, hours: int = 24):
        """Show recent cloud heart-rate records, capped to one week."""
        async for result in self._guard(event):
            yield result
            return
        rows = await self.query_service.heart_rates(hours)
        if not rows:
            yield event.plain_result("最近范围内没有缓存心率记录。请先执行 健康同步。")
            return
        lines = [f"最近 {max(1, min(hours, 168))} 小时心率记录（云端采集，非实时）"]
        for row in rows[:20]:
            kind = "运动" if row["is_workout"] else ("主动" if row["sample_type"] == "active" else "被动")
            lines.append(f"{row['timestamp']}｜{row['bpm']} bpm｜{kind}")
        yield event.plain_result("\n".join(lines))

    @filter.command("身体数据")
    async def body_data(self, event: AstrMessageEvent):
        """Show the latest cached smart-scale measurement."""
        async for result in self._guard(event):
            yield result
            return
        yield event.plain_result(measurement_text(await self.query_service.body()))

    @filter.command("健康趋势")
    async def health_trend(self, event: AstrMessageEvent, days: int = 7):
        """Show a concise text trend of cached daily cloud records."""
        async for result in self._guard(event):
            yield result
            return
        rows = await self.query_service.trend(days)
        if not rows:
            yield event.plain_result("暂无趋势数据。请先执行 健康同步。")
            return
        lines = [f"最近 {max(1, min(days, 90))} 天趋势（云端已同步数据）"]
        for row in rows:
            heart = f"{row['avg_heart_rate']:.0f}" if row["avg_heart_rate"] is not None else "—"
            lines.append(f"{row['date']}｜步数 {row['steps']}｜活动 {row['active_kcal']:.0f} kcal｜平均心率 {heart}")
        yield event.plain_result("\n".join(lines))
