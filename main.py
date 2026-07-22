"""Native AstrBot plugin for private Xiaomi Mi Fitness cloud health data."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import suppress
from datetime import timedelta
from datetime import UTC, datetime
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.provider import ProviderRequest
from astrbot.core.agent.message import TextPart

from .adapters import MiFitnessCloudAdapter
from .services import AlertService, HealthMonitorService, QueryService, SyncService
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
        self.user_id = str(
            config.get("user_id") or os.getenv("MI_FITNESS_USER_ID", "")
        ).strip()
        self.pass_token = str(
            config.get("pass_token") or os.getenv("MI_FITNESS_PASS_TOKEN", "")
        ).strip()
        self.owner_platform_id = str(config.get("owner_platform_id") or "").strip()
        self.owner_platform_instance_id = str(
            config.get("owner_platform_instance_id") or ""
        ).strip()
        database_path = str(config.get("database_path") or "").strip()
        self.database = Database(
            Path(database_path)
            if database_path
            else self.data_dir / "mi_fitness_health.sqlite3"
        )
        self.adapter = MiFitnessCloudAdapter(
            self.user_id, self.pass_token, str(config.get("region") or "").strip()
        )
        self.sync_service = SyncService(self.adapter, self.database, self.user_id)
        self.query_service = QueryService(
            self.database,
            self.user_id,
            str(config.get("user_timezone") or "Asia/Shanghai"),
        )
        self.alert_service = AlertService(
            self.database,
            self.user_id,
            int(config.get("heart_rate_high") or 0),
            int(config.get("heart_rate_low") or 0),
            int(config.get("alert_consecutive_count") or 3),
            int(config.get("alert_cooldown_minutes") or 120),
            int(config.get("spo2_low") or 0),
            int(config.get("stress_high") or 0),
            int(config.get("sleep_min_minutes") or 0),
            int(config.get("alert_data_max_age_minutes") or 180),
        )
        self.auto_sync_enabled = bool(config.get("enable_auto_sync", True))
        self.health_alerts_enabled = bool(config.get("enable_health_alerts", True))
        self.care_dialogue_enabled = bool(config.get("enable_care_dialogue", True))
        self.proactive_monitor_enabled = bool(
            config.get("enable_proactive_health_monitor", True)
        )
        self.monitor_interval = max(
            5, min(int(config.get("health_check_interval_minutes") or 30), 1440)
        )
        self.natural_query_sync_minutes = max(
            1, min(int(config.get("natural_query_sync_minutes") or 15), 120)
        )
        self.sync_days = max(1, min(int(config.get("default_sync_days") or 7), 90))
        self.sync_interval = max(5, int(config.get("sync_interval_minutes") or 60))
        self.monitor_service = HealthMonitorService(
            self.database,
            self.owner_platform_id,
            self.query_service.timezone,
            bool(config.get("enable_late_night_activity_check", True)),
            str(config.get("late_night_start") or "00:30"),
            str(config.get("late_night_end") or "06:00"),
            int(config.get("late_night_activity_window_minutes") or 45),
            int(config.get("alert_cooldown_minutes") or 120),
            int(config.get("proactive_daily_limit") or 3),
        )
        self._auto_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._auto_sync_paused = False

    async def initialize(self) -> None:
        """Migrate the database and schedule one guarded background loop."""
        await self.sync_service.initialize()
        monitor_ready = (
            self.proactive_monitor_enabled
            and self.owner_platform_id
            and self.owner_platform_instance_id
            and self.user_id
            and self.pass_token
        )
        if monitor_ready and not self._monitor_task:
            self._monitor_task = asyncio.create_task(
                self._health_monitor_loop(), name=f"{self.name}-health-monitor"
            )
        elif (
            self.auto_sync_enabled
            and self.user_id
            and self.pass_token
            and not self._auto_task
        ):
            self._auto_task = asyncio.create_task(
                self._auto_sync_loop(), name=f"{self.name}-auto-sync"
            )

    async def terminate(self) -> None:
        """Cancel the periodic task and close plugin-owned HTTP resources."""
        if self._auto_task:
            self._auto_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._auto_task
            self._auto_task = None
        if self._monitor_task:
            self._monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None
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

    async def _sync(self) -> dict[str, object]:
        """Run one synchronized Xiaomi cloud refresh."""
        return await self.sync_service.sync(self.sync_days)

    async def _send_private_message(self, text: str) -> bool:
        """Send a proactive result only to the last observed owner private chat."""
        state = await asyncio.to_thread(
            self.database.private_owner_session, self.owner_platform_id
        )
        if not state:
            return False
        if self.owner_platform_instance_id and not state["session"].startswith(
            self.owner_platform_instance_id + ":"
        ):
            logger.warning(
                "Mi Fitness proactive target does not match configured platform instance"
            )
            return False
        try:
            return await self.context.send_message(
                state["session"], MessageChain().message(text)
            )
        except Exception as error:
            logger.warning(
                "Mi Fitness proactive message was not delivered: %s",
                redact_error(error),
            )
            return False

    async def _health_monitor_loop(self) -> None:
        """Refresh and evaluate private findings at the configured bounded interval."""
        failures = 0
        while not self._auto_sync_paused:
            try:
                state = await asyncio.to_thread(
                    self.database.private_owner_session, self.owner_platform_id
                )
                if not state:
                    failures = 0
                    await asyncio.sleep(self.monitor_interval * 60)
                    continue
                # Monitoring only needs a short recent range; SyncService adds
                # its normal 48-hour overlap for delayed Xiaomi uploads.
                await self.sync_service.sync(1)
                health_findings = (
                    await self.alert_service.evaluate()
                    if self.health_alerts_enabled
                    else []
                )
                messages = [finding.message for finding in health_findings]
                late_finding = await self.monitor_service.evaluate_late_activity()
                if late_finding:
                    messages.append(late_finding.message)
                if messages and not await self.monitor_service.proactive_cooling_down():
                    body = "我刚检查了一次你的近期状态：\n- " + "\n- ".join(messages)
                    body += "\n小米健康云数据可能延迟，不是实时监护；如果有明显不适，请及时联系医疗专业人员。"
                    sent = await self._send_private_message(body)
                    if sent:
                        for finding in health_findings:
                            await self.alert_service.mark_sent(finding)
                        if late_finding:
                            await self.monitor_service.mark_sent(late_finding)
                        await self.monitor_service.mark_proactive_sent(body)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                reason = redact_error(error)
                auth_markers = (
                    "凭证已失效",
                    "重新获取 Cookie",
                    "重新登录",
                    "需要验证",
                    "风控",
                )
                if any(marker in reason for marker in auth_markers):
                    logger.warning(
                        "Mi Fitness health monitor paused for reauthorization: %s",
                        reason,
                    )
                    self._auto_sync_paused = True
                    break
                retry_seconds = min(
                    self.monitor_interval * 60, 30 * (2 ** min(failures - 1, 5))
                )
                logger.warning(
                    "Mi Fitness health monitor retrying after a temporary error: %s",
                    reason,
                )
                await asyncio.sleep(retry_seconds)
                continue
            await asyncio.sleep(self.monitor_interval * 60)

    def _authorized(self, event: AstrMessageEvent) -> bool:
        """Return whether the sender matches the one configured data owner."""
        sender_matches = (
            bool(self.owner_platform_id)
            and str(event.get_sender_id()) == self.owner_platform_id
        )
        platform_matches = (
            bool(self.owner_platform_instance_id)
            and event.get_platform_id() == self.owner_platform_instance_id
        )
        return sender_matches and platform_matches

    def _is_private_owner_event(self, event: AstrMessageEvent) -> bool:
        """Conversational health data is available only in the owner's private chat."""
        return (
            self._authorized(event)
            and event.session.message_type == MessageType.FRIEND_MESSAGE
        )

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def remember_owner_private_activity(self, event: AstrMessageEvent):
        """Remember private owner activity as the only evidence for being awake."""
        if self._is_private_owner_event(event):
            await asyncio.to_thread(
                self.database.touch_private_owner_session,
                self.owner_platform_id,
                event.unified_msg_origin,
                None,
                bool(self.owner_platform_instance_id),
            )

    @staticmethod
    def _is_health_question(text: str) -> bool:
        """Recognize ordinary Chinese health questions without intercepting replies."""
        compact = text.lower().replace(" ", "")
        keywords = (
            "睡",
            "失眠",
            "心率",
            "心跳",
            "步数",
            "走了",
            "运动",
            "卡路里",
            "热量",
            "体重",
            "体脂",
            "血氧",
            "压力",
            "身体数据",
            "健康",
            "昨天怎么样",
            "今天怎么样",
        )
        return any(word in compact for word in keywords)

    @staticmethod
    def _wants_fresh_cloud_data(text: str) -> bool:
        """Allow natural wording such as 'I just synced' to bypass the brief cache window."""
        compact = text.lower().replace(" ", "")
        return any(
            word in compact
            for word in ("刚同步", "刚上传", "最新", "更新一下", "刷新", "同步一下")
        )

    async def _refresh_for_natural_question(self, text: str) -> bool:
        """Refresh stale cloud cache before an owner asks a health question.

        This does not circumvent Xiaomi's phone-to-cloud upload: it only means
        the owner no longer has to type a separate plugin command after the
        phone app has uploaded the data.
        """
        last_sync = await self.query_service.latest_sync_at()
        if last_sync and not self._wants_fresh_cloud_data(text):
            try:
                parsed = datetime.fromisoformat(last_sync)
                parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                if datetime.now(UTC) - parsed < timedelta(
                    minutes=self.natural_query_sync_minutes
                ):
                    return False
            except ValueError:
                pass
        try:
            await asyncio.wait_for(self._sync(), timeout=60)
            return True
        except TimeoutError:
            logger.warning(
                "Mi Fitness natural-query refresh timed out; using local cache"
            )
            return False
        except Exception as error:
            # A natural chat reply should still use the safe local cache when
            # Xiaomi is temporarily unavailable; never expose credentials.
            logger.warning(
                "Mi Fitness natural-query refresh failed: %s", redact_error(error)
            )
            return False

    @filter.llm_tool(name="query_mi_fitness_health")
    async def query_mi_fitness_health(
        self, event: AstrMessageEvent, focus: str = "综合概况"
    ) -> str:
        """在自然对话中读取当前用户的小米运动健康云数据。

        当用户询问自己的睡眠、步数、运动消耗、心率、体重、体脂、血氧、压力或身体状态时调用。
        数据来自小米健康云，可能延迟，不是实时监护；不要据此作医疗诊断。

        Args:
            focus(string): 用户希望了解的项目或时间范围，例如“昨天睡眠”“今日步数”“最近心率”。
        """
        if not self.care_dialogue_enabled or not self._is_private_owner_event(event):
            return "此工具仅允许已配置的数据所有者在私聊中使用。"
        await self._refresh_for_natural_question(focus)
        snapshot = await self.query_service.care_snapshot(focus)
        last_sync = await self.query_service.latest_sync_at()
        return (
            f"查询重点：{focus}\n{snapshot}\n最近本地同步：{last_sync or '暂无'}\n"
            "以上为小米健康云已上传的历史数据，并非实时监护；请直接回答用户的问题，不作医疗诊断。"
        )

    @filter.on_llm_request()
    async def add_owner_health_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """Provide a fallback context when a clear health question reaches the LLM."""
        # LLM context can influence free-form replies, so it is stricter than
        # command authorization and never carries health data into a group.
        if not self.care_dialogue_enabled or not self._is_private_owner_event(event):
            return
        question = event.get_message_str()
        if not self._is_health_question(question):
            return
        await self._refresh_for_natural_question(question)
        snapshot = await self.query_service.care_snapshot(question)
        last_sync = await self.query_service.latest_sync_at()
        text = (
            "<private_health_context>\n"
            + snapshot
            + f"\n最近本地同步：{last_sync or '暂无'}\n"
            "These are delayed Xiaomi cloud records, not real-time monitoring. Answer the owner's health question directly in Chinese from these records; avoid diagnosis and do not claim medical certainty.\n</private_health_context>"
        )
        part = TextPart(text=text)
        req.extra_user_content_parts.append(
            part.mark_as_temp() if hasattr(part, "mark_as_temp") else part
        )

    async def _guard(self, event: AstrMessageEvent):
        """Require the configured owner and a private chat for all health commands."""
        if self._is_private_owner_event(event):
            return
        if not self.owner_platform_id:
            message = "健康数据所有者尚未配置，请由管理员设置 owner_platform_id。"
        elif not self.owner_platform_instance_id:
            message = "所有者平台实例尚未配置，请由管理员设置 owner_platform_instance_id 后再查询。"
        elif self._authorized(event):
            message = "为避免健康数据公开，健康查询只能在与机器人的私聊中使用。"
        else:
            message = "此健康数据仅允许已配置的所有者查询。"
        yield event.plain_result(message)

    @filter.command("健康帮助")
    async def health_help(self, event: AstrMessageEvent):
        """Show commands and privacy boundaries."""
        yield event.plain_result(
            "小米运动健康（仅所有者可用）\n"
            "健康连接｜健康同步｜健康状态｜今日健康｜心率记录 [小时]｜身体数据｜健康趋势 [天]\n"
            "也可以直接说“我昨天睡得怎么样”或“我今天走了多少步”：机器人会按配置自动刷新云端缓存。\n"
            f"主动健康检查：{'每 ' + str(self.monitor_interval) + ' 分钟' if self.proactive_monitor_enabled else '关闭'}；只在有依据且冷却结束时私聊一次。\n"
            "数据是小米健康云已同步的历史记录，所有展示均含采集时间；不是实时监护，也不构成医疗诊断。"
        )

    @filter.command("健康连接")
    async def health_connection(self, event: AstrMessageEvent):
        """Authenticate and show only a credential-safe connection state."""
        async for result in self._guard(event):
            yield result
            return
        if not self.user_id or not self.pass_token:
            yield event.plain_result(
                "未配置 user_id 或 pass_token。请在插件配置页填写后重新加载插件。"
            )
            return
        if not await self.adapter.connect():
            yield event.plain_result(
                f"健康连接失败：{self.adapter.last_error or '未知错误'}\n遇到验证码、二次验证或风控时，请在浏览器完成验证后更新 Cookie。"
            )
            return
        labels = {
            "daily_activity": "步数/距离/活动消耗",
            "heart_rate": "心率",
            "body_measurements": "体重/身体成分",
            "sleep": "睡眠",
            "spo2": "血氧",
            "stress": "压力",
        }
        types = (
            "、".join(
                labels.get(item, item)
                for item in self.adapter.get_available_data_types()
            )
            or "未发现最近 30 天数据"
        )
        yield event.plain_result(
            f"健康连接成功\n区域：{self.adapter.region}\n可用数据：{types}\n不显示账号、Token、Cookie 或 ssecurity。"
        )

    @filter.command("健康同步")
    async def health_sync(self, event: AstrMessageEvent):
        """Manually synchronize a bounded recent cloud-data window."""
        async for result in self._guard(event):
            yield result
            return
        try:
            result = await self._sync()
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
                    lines.append(f"{label}：本次未同步（已保留其他数据）")
                else:
                    lines.append(
                        f"{label}：读取 {item.get('fetched', 0)}，新增 {item.get('added', 0)}，更新 {item.get('updated', 0)}"
                    )
            yield event.plain_result("\n".join(lines))
        except Exception as error:
            yield event.plain_result(f"健康同步失败：{redact_error(error)}")

    @filter.command("今日健康")
    async def health_today(self, event: AstrMessageEvent):
        """Show cached user-local daily summary."""
        async for result in self._guard(event):
            yield result
            return
        activity, rates, measurement = await self.query_service.today_summary()
        yield event.plain_result(
            today_text(activity, rates, measurement)
            + "\n"
            + await self.query_service.care_snapshot()
        )

    @filter.command("健康详情")
    async def health_details(self, event: AstrMessageEvent):
        """Show latest supported sleep, blood-oxygen, and stress cloud records."""
        async for result in self._guard(event):
            yield result
            return
        yield event.plain_result(
            "健康详情（云端已同步数据，非实时）\n"
            + await self.query_service.care_snapshot()
        )

    @filter.command("健康诊断")
    async def health_diagnose(self, event: AstrMessageEvent):
        """Probe cloud keys safely to diagnose an account-specific missing data type."""
        async for result in self._guard(event):
            yield result
            return
        if not self.adapter.is_connected() and not await self.adapter.connect():
            yield event.plain_result(
                f"健康诊断无法连接：{self.adapter.last_error or '未知错误'}"
            )
            return
        data = await self.adapter.probe_data_keys(
            datetime.now(UTC) - timedelta(days=30), datetime.now(UTC)
        )
        yield event.plain_result(
            "健康云诊断（仅记录数/脱敏错误，不含健康明细或凭证）\n"
            + "\n".join(f"{key}：{value}" for key, value in data.items())
        )

    @filter.command("健康状态")
    async def health_status(self, event: AstrMessageEvent):
        """Show cache and synchronization status without exposing credentials."""
        async for result in self._guard(event):
            yield result
            return
        last_sync = await self.query_service.latest_sync_at()
        private_state = await asyncio.to_thread(
            self.database.private_owner_session, self.owner_platform_id
        )
        if self._auto_sync_paused:
            background_status = "已暂停（请检查授权）"
        elif self.proactive_monitor_enabled:
            background_status = f"主动检查接管（每 {self.monitor_interval} 分钟）"
        else:
            background_status = "开启" if self.auto_sync_enabled else "关闭"
        yield event.plain_result(
            f"健康状态\n连接：{'已连接' if self.adapter.is_connected() else '未连接/待验证'}\n"
            f"区域：{self.adapter.region or '自动探测'}\n最近同步：{last_sync or '暂无'}\n"
            f"后台同步：{background_status}\n"
            f"主动健康检查：{'开启（每 ' + str(self.monitor_interval) + ' 分钟）' if self.proactive_monitor_enabled else '关闭'}\n"
            f"主动私聊目标：{'已记录' if private_state else '待所有者先私聊一次'}\n"
            f"自然语言查询刷新：{self.natural_query_sync_minutes} 分钟"
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
            kind = (
                "运动"
                if row["is_workout"]
                else ("主动" if row["sample_type"] == "active" else "被动")
            )
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
            heart = (
                f"{row['avg_heart_rate']:.0f}"
                if row["avg_heart_rate"] is not None
                else "—"
            )
            lines.append(
                f"{row['date']}｜步数 {row['steps']}｜活动 {row['active_kcal']:.0f} kcal｜平均心率 {heart}"
            )
        yield event.plain_result("\n".join(lines))
