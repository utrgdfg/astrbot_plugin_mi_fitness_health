"""Owner-only health command handlers for the plugin entrypoint."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import wraps

from astrbot.api.event import AstrMessageEvent

from ..services.sync_service import SyncServiceBusyError
from ..utils import measurement_text, today_text
from ..utils.privacy import redact_error


def _tracked_foreground_operation(method):
    """Keep mutating command tasks inside the plugin reload barrier."""

    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        task = self._begin_foreground_operation()
        if task is None:
            event = args[0] if args else kwargs.get("event")
            if event is not None:
                event.stop_event()
                yield event.plain_result("插件正在重载，请稍后再试。")
            return
        try:
            async for result in method(self, *args, **kwargs):
                yield result
        finally:
            self._end_foreground_operation(task)

    return wrapped


class HealthCommandsMixin:
    """Provide owner-only commands for health data and maintenance."""

    _CONVERSATION_HEALTH_MODE_LABELS = {
        "main_model": "当前主模型预判",
        "decision_model": "独立判断模型",
        "local_rules": "本地轻量规则",
    }

    def _connection_check_active(self) -> bool:
        """Return whether a background Xiaomi connection still owns cloud work."""
        connection_task = getattr(self, "_connection_task", None)
        cloud_task = getattr(self, "_connection_cloud_task", None)
        return bool(
            (connection_task is not None and not connection_task.done())
            or (cloud_task is not None and not cloud_task.done())
        )

    async def health_help(self, event: AstrMessageEvent):
        """Show commands and privacy boundaries."""
        async for result in self._guard(event):
            yield result
            return
        mode_label = self._CONVERSATION_HEALTH_MODE_LABELS[
            self._effective_conversation_health_mode()
        ]
        yield event.plain_result(
            "小米运动健康（仅所有者可用）\n"
            "健康连接｜健康同步｜健康状态｜今日健康｜心率记录 [小时]｜身体数据｜健康趋势 [天]\n"
            "平时只需正常聊天；出现作息、疲劳、运动或早晚问候等线索时，插件会在后台准备相关生活数据，让机器人按当前人格自然回应。\n"
            f"日常对话数据方式：{mode_label}。\n"
            "直接查询和以上命令主要用于核对数据或排查连接问题。\n"
            f"主动关心检查：{'每 ' + str(self.monitor_interval) + ' 分钟检查本地状态' if self.proactive_monitor_enabled else '关闭'}；只在自然时机且冷却结束时私聊一次。\n"
            f"主动判断读取最近私聊：{'已授权' if self.allow_proactive_chat_context else '未授权（主动关心保持静默）'}。\n"
            f"普通自动同步：{'每 ' + str(self.sync_interval) + ' 分钟读取小米云' if self.auto_sync_enabled else '关闭（使用对话按需同步）'}。\n"
            f"对话生活数据授权：{'已开启' if self.allow_health_data_to_llm else '未开启（仅命令查询）'}。\n"
            "数据用于让日常对话更贴近你；它不是实时监护，也不用于医疗诊断。"
        )

    async def health_connection(self, event: AstrMessageEvent):
        """Start a bounded background connection check and release the pipeline."""
        async for result in self._guard(event):
            yield result
            return
        # A successful background launch intentionally has no immediate chat
        # bubble, but it must still consume the command instead of falling
        # through to the default LLM pipeline.
        event.stop_event()
        if not self.user_id or not self.pass_token:
            yield event.plain_result(
                "未配置 user_id 或 pass_token。请在插件配置页填写后重新加载插件。"
            )
            return
        if self._local_data_clear_in_progress:
            yield event.plain_result("本地健康数据正在清除，请稍后再检查连接。")
            return
        if self._terminating or self._terminated:
            return
        if self._connection_task is not None and not self._connection_task.done():
            return
        cloud_task = getattr(self, "_connection_cloud_task", None)
        if cloud_task is not None and not cloud_task.done():
            yield event.plain_result("上一次健康连接仍在收尾，请稍后再试。")
            return
        if self._maintenance_lock.locked():
            yield event.plain_result("当前有健康数据操作正在进行，请稍后再检查连接。")
            return
        error_text = ""
        async with self._maintenance_lock:
            if self._local_data_clear_in_progress:
                error_text = "本地健康数据正在清除，请稍后再检查连接。"
            elif self._terminating or self._terminated:
                return
            elif self._connection_task is not None and not self._connection_task.done():
                return
            elif (
                getattr(self, "_connection_cloud_task", None) is not None
                and not self._connection_cloud_task.done()
            ):
                error_text = "上一次健康连接仍在收尾，请稍后再试。"
            else:
                session = str(event.unified_msg_origin)
                if self._terminating or self._terminated:
                    return
                self._connection_task = asyncio.create_task(
                    self._connection_worker(session),
                    name=f"{self.name}-connection-check",
                )
        if error_text:
            yield event.plain_result(error_text)

    @_tracked_foreground_operation
    async def health_sync(self, event: AstrMessageEvent):
        """Manually synchronize a bounded recent cloud-data window."""
        async for result in self._guard(event):
            yield result
            return
        if self._local_data_clear_in_progress:
            yield event.plain_result("本地健康数据正在清除，请稍后再同步。")
            return
        if self._connection_check_active():
            yield event.plain_result("健康连接仍在检查或收尾，请稍后再同步。")
            return
        if self._maintenance_lock.locked():
            yield event.plain_result("当前有健康数据操作正在进行，请稍后再同步。")
            return
        reply = ""
        async with self._maintenance_lock:
            if self._local_data_clear_in_progress:
                reply = "本地健康数据正在清除，请稍后再同步。"
            elif self._connection_check_active():
                reply = "健康连接仍在检查或收尾，请稍后再同步。"
            else:
                now = datetime.now(UTC)
                if self._last_manual_sync_at is not None:
                    elapsed = (now - self._last_manual_sync_at).total_seconds()
                    if elapsed < self._manual_sync_min_interval:
                        remaining = max(
                            1,
                            min(
                                self._manual_sync_min_interval,
                                int(self._manual_sync_min_interval - elapsed + 0.999),
                            ),
                        )
                        reply = (
                            f"刚执行过同步，请约 {remaining} 秒后再试；"
                            "云端数据上传本身有延迟，频繁同步不会让数据更新更快。"
                        )
                if not reply:
                    previous_sync_at = self._last_manual_sync_at
                    self._last_manual_sync_at = now
                    try:
                        result = await self._sync()
                        self._auto_sync_paused = False
                        self._ensure_background_task()
                        details = result.get("details", {})
                        labels = {
                            "daily_activity": "活动",
                            "heart_rate": "心率",
                            "body_measurements": "身体数据",
                            "sleep": "睡眠",
                            "spo2": "血氧",
                            "stress": "压力",
                        }
                        lines = [
                            f"健康同步完成：{result['days']} 天范围，新增 {result['added']}，更新 {result['updated']}。"
                        ]
                        for key, label in labels.items():
                            item = details.get(key, {})
                            if "error" in item:
                                lines.append(
                                    f"{label}：本次未同步（{redact_error(item['error'])}；已保留其他数据）"
                                )
                            else:
                                lines.append(
                                    f"{label}：读取 {item.get('fetched', 0)}，新增 {item.get('added', 0)}，更新 {item.get('updated', 0)}"
                                )
                        reply = "\n".join(lines)
                    except Exception as error:
                        self._last_manual_sync_at = previous_sync_at
                        reply = f"健康同步失败：{redact_error(error)}"
        yield event.plain_result(reply)

    async def health_today(self, event: AstrMessageEvent):
        """Show cached user-local daily summary."""
        async for result in self._guard(event):
            yield result
            return
        activity, rates, measurement = await self.query_service.today_summary()
        yield event.plain_result(
            today_text(activity, rates, measurement, self.query_service.timezone)
            + "\n"
            + await self.query_service.care_snapshot("今天 睡眠 血氧 压力")
        )

    async def health_details(self, event: AstrMessageEvent):
        """Show latest supported sleep, blood-oxygen, and stress cloud records."""
        async for result in self._guard(event):
            yield result
            return
        yield event.plain_result(
            "健康详情（云端已同步数据，非实时）\n"
            + await self.query_service.care_snapshot("睡眠 血氧 压力")
        )

    @_tracked_foreground_operation
    async def health_diagnose(self, event: AstrMessageEvent):
        """Probe cloud keys safely to diagnose data availability, not the user."""
        async for result in self._guard(event):
            yield result
            return
        if self._local_data_clear_in_progress:
            yield event.plain_result("本地健康数据正在清除，请稍后再诊断。")
            return
        if self._connection_check_active():
            yield event.plain_result("健康连接仍在检查或收尾，请稍后再诊断。")
            return
        if self._maintenance_lock.locked():
            yield event.plain_result("当前有健康数据操作正在进行，请稍后再诊断。")
            return
        async with self._maintenance_lock:
            if self._local_data_clear_in_progress:
                reply = "本地健康数据正在清除，请稍后再诊断。"
            elif self._connection_check_active():
                reply = "健康连接仍在检查或收尾，请稍后再诊断。"
            else:
                try:
                    data = await self.sync_service.probe_data_keys(
                        datetime.now(UTC) - timedelta(days=30), datetime.now(UTC)
                    )
                except Exception as error:
                    reply = f"数据诊断无法连接：{redact_error(error)}"
                else:
                    reply = (
                        "健康云数据诊断（仅记录数/脱敏错误，不含健康明细或凭证）\n"
                        + "\n".join(f"{key}：{value}" for key, value in data.items())
                    )
        yield event.plain_result(reply)

    async def health_status(self, event: AstrMessageEvent):
        """Show cache and synchronization status without exposing credentials."""
        async for result in self._guard(event):
            yield result
            return
        last_sync = await self.query_service.latest_sync_at()
        private_state = await asyncio.to_thread(
            self.database.private_owner_session, self.owner_platform_id
        )
        monitor_running = bool(self._monitor_task and not self._monitor_task.done())
        auto_running = bool(self._auto_task and not self._auto_task.done())
        if auto_running:
            background_status = f"自动同步（每 {self.sync_interval} 分钟）"
        elif self._auto_sync_paused:
            background_status = "已暂停（请检查授权）"
        elif not self.user_id or not self.pass_token:
            background_status = "未运行（缺少小米凭证）"
        elif not self.auto_sync_enabled:
            background_status = "未开启（按需同步）"
        else:
            background_status = "未运行"
        mode_label = self._CONVERSATION_HEALTH_MODE_LABELS[
            self._effective_conversation_health_mode()
        ]
        timezone_status = str(self.query_service.timezone)
        if getattr(self.query_service, "invalid_timezone_name", None):
            timezone_status += "（配置无效，已回退）"
        elif getattr(self.query_service, "timezone_fallback_used", False):
            timezone_status += "（固定偏移回退）"
        yield event.plain_result(
            f"健康状态\n连接：{'已连接' if self.adapter.is_connected() else '未连接/待验证'}\n"
            f"区域：{self.adapter.region or '自动探测'}\n用户时区：{timezone_status}\n"
            f"最近同步完成时间：{self.query_service.display_timestamp(last_sync) if last_sync else '暂无'}\n"
            f"平台实例校验：{'已启用' if self.owner_platform_instance_id else '未配置（健康功能禁用）'}\n"
            f"后台同步：{background_status}\n"
            f"主动关心检查：{'运行中' if monitor_running else '未运行'}"
            f"（每 {self.monitor_interval} 分钟，仅检查本地数据）\n"
            f"主动私聊目标：{'已记录' if private_state else '待所有者先私聊一次'}\n"
            f"对话触发的数据刷新间隔：{self.natural_query_sync_minutes} 分钟\n"
            f"日常对话数据方式：{mode_label}\n"
            f"判断模型等待上限：{self.context_decision_timeout_seconds} 秒\n"
            f"对话云端刷新等待上限：{self.natural_query_cloud_wait_seconds} 秒\n"
            f"对话生活数据授权：{'开启' if self.allow_health_data_to_llm else '关闭'}\n"
            f"主动判断私聊上下文授权：{'开启' if self.allow_proactive_chat_context else '关闭'}\n"
            f"本地数据保留：{str(self.data_retention_days) + ' 天' if self.data_retention_days else '不自动清理'}"
        )

    @_tracked_foreground_operation
    async def clear_local_health_data(
        self, event: AstrMessageEvent, confirmation: str = ""
    ):
        """Delete local cache only after an exact owner confirmation."""
        async for result in self._guard(event):
            yield result
            return
        if confirmation.strip() != "确认清除":
            yield event.plain_result(
                "此操作会清除插件本地缓存、同步状态和主动关心记录，"
                "但不会删除小米云数据或配置凭证。"
                "如确定，请发送：健康清除本地数据 确认清除"
            )
            return
        if self.auto_sync_enabled or self.proactive_monitor_enabled:
            yield event.plain_result(
                "为避免清除后立即重新产生本地记录，请先在插件配置中关闭"
                "“普通自动同步”和“主动关心检查”，重载插件后再执行清除。"
            )
            return
        if self._maintenance_lock.locked():
            yield event.plain_result("当前有健康数据操作正在进行，请稍后再清除。")
            return
        deleted: int | None = None
        self._local_data_clear_in_progress = True
        try:
            async with self._maintenance_lock:
                for attribute in (
                    "_auto_task",
                    "_monitor_task",
                    "_natural_refresh_task",
                    "_connection_task",
                    "_owner_activity_task",
                ):
                    await self._cancel_owned_task(attribute)
                self._pending_owner_activity = None
                self._pending_refresh_types.clear()
                self._active_refresh_types.clear()
                try:
                    deleted = await self.sync_service.purge_local_data(
                        self.owner_platform_id
                    )
                except SyncServiceBusyError:
                    pass
        finally:
            self._local_data_clear_in_progress = False
        if deleted is None:
            yield event.plain_result("小米云连接或同步仍在收尾，请稍后再清除本地数据。")
            return
        yield event.plain_result(
            f"本地健康缓存已清除，共删除 {deleted} 条本地记录。"
            "小米云端数据和插件配置凭证未被修改。"
        )

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
            kind = (
                "运动"
                if row["is_workout"]
                else ("主动" if row["sample_type"] == "active" else "被动")
            )
            lines.append(
                f"{self.query_service.display_timestamp(row['timestamp'])}｜{row['bpm']} bpm｜{kind}"
            )
        yield event.plain_result("\n".join(lines))

    async def body_data(self, event: AstrMessageEvent):
        """Show the latest cached smart-scale measurement."""
        async for result in self._guard(event):
            yield result
            return
        yield event.plain_result(
            measurement_text(
                await self.query_service.body(), self.query_service.timezone
            )
        )

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
            heart = (
                f"{row['avg_heart_rate']:.0f}"
                if row["avg_heart_rate"] is not None
                else "—"
            )
            lines.append(
                f"{row['date']}｜步数 {row['steps']}｜活动 {row['active_kcal']:.0f} kcal｜平均心率 {heart}"
            )
        yield event.plain_result("\n".join(lines))
