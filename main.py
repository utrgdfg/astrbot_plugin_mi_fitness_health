"""Native AstrBot plugin for private Xiaomi Mi Fitness cloud health data."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.platform import MessageType
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext

from .adapters import MiFitnessAuthenticationError, MiFitnessCloudAdapter
from .features import (
    DEFAULT_CONTEXT_DECISION_PROMPT,
    DEFAULT_PROACTIVE_CONTEXT_PROMPT,
    DEFAULT_PROACTIVE_DECISION_PROMPT,
    ConversationRoutingMixin,
    HealthCommandsMixin,
    MainModelToolingMixin,
    ProactiveCareMixin,
    add_private_health_tool,
    scrub_private_health_tool_messages,
)
from .services import HealthMonitorService, QueryService, SyncService
from .storage import Database
from .utils.access import (
    normalize_identifier,
    owner_access_denial_reason,
)
from .utils.async_tools import await_with_hard_timeout
from .utils.privacy import redact_error

CONNECTION_COMMAND_TIMEOUT_SECONDS = 120.0
DETACHED_TASK_DRAIN_SECONDS = 1.0
SHUTDOWN_CLOUD_CLOSE_TIMEOUT_SECONDS = 5.0


def _config_bool(
    config: AstrBotConfig, key: str, default: bool, *, fail_closed: bool = False
) -> bool:
    """Parse one bool without treating arbitrary non-empty strings as true."""
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on", "是", "开启"}:
            return True
        if normalized in {"false", "0", "no", "off", "否", "关闭", ""}:
            return False
    fallback = False if fail_closed else default
    logger.warning(
        "[小米运动健康] 配置项 %s 不是有效布尔值，已使用%s默认值",
        key,
        "关闭" if not fallback else "开启",
    )
    return fallback


def _config_int(
    config: AstrBotConfig,
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    """Parse and clamp one integer without allowing malformed config to abort load."""
    value = config.get(key, default)
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, float) and not value.is_integer():
            raise ValueError
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        logger.warning(
            "[小米运动健康] 配置项 %s 不是有效整数，已使用默认值",
            key,
        )
        parsed = default
    return max(minimum, min(parsed, maximum))


class MiFitnessHealthPlugin(
    HealthCommandsMixin,
    ProactiveCareMixin,
    MainModelToolingMixin,
    ConversationRoutingMixin,
    Star,
):
    """Own cloud lifecycle, local storage, and owner-only health commands."""

    _CONTEXT_CATEGORY_LABELS = {
        "activity": "活动",
        "heart": "心率",
        "body": "身体数据",
        "sleep": "睡眠",
        "spo2": "血氧",
        "stress": "压力",
    }
    _CONTEXT_SCOPE_LABELS = {
        "today": "今天",
        "yesterday": "昨天",
        "recent": "最近",
        "none": "",
    }
    _SYNC_TYPE_LOG_LABELS = {
        "daily_activity": "活动",
        "heart_rate": "心率",
        "body_measurements": "身体数据",
        "sleep": "睡眠",
        "spo2": "血氧",
        "stress": "压力",
    }
    _MORNING_WAKE_CUES = (
        "早安",
        "早啊",
        "早呀",
        "早上好",
        "起床",
        "刚醒",
        "醒了",
        "睡醒",
    )

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
        self.owner_platform_id = normalize_identifier(config.get("owner_platform_id"))
        self.owner_platform_instance_id = normalize_identifier(
            config.get("owner_platform_instance_id")
        )
        database_path = str(config.get("database_path") or "").strip()
        selected_database_path = (
            Path(database_path)
            if database_path
            else self.data_dir / "mi_fitness_health.sqlite3"
        )
        self.database = Database(
            selected_database_path,
            custom_path=bool(database_path),
        )
        self.query_service = QueryService(
            self.database,
            self.user_id,
            str(config.get("user_timezone") or "Asia/Shanghai"),
        )
        if self.query_service.invalid_timezone_name:
            logger.warning(
                "[小米运动健康] 配置项 user_timezone 不是有效的 IANA 时区，"
                "已安全回退为 Asia/Shanghai"
            )
        elif self.query_service.timezone_fallback_used:
            logger.warning(
                "[小米运动健康] 当前 Python 环境无法加载 IANA 时区数据，"
                "已使用 Asia/Shanghai 固定偏移回退"
            )
        self.adapter = MiFitnessCloudAdapter(
            self.user_id,
            self.pass_token,
            str(config.get("region") or "").strip(),
            self.query_service.timezone,
        )
        self.data_retention_days = _config_int(
            config, "data_retention_days", 90, 0, 3650
        )
        self.sync_service = SyncService(
            self.adapter,
            self.database,
            self.user_id,
            self.data_retention_days,
            self.owner_platform_id,
        )
        self.auto_sync_enabled = _config_bool(config, "enable_auto_sync", False)
        self.care_dialogue_enabled = _config_bool(config, "enable_care_dialogue", True)
        self.allow_health_data_to_llm = _config_bool(
            config,
            "allow_health_data_to_llm",
            False,
            fail_closed=True,
        )
        self.allow_proactive_chat_context = _config_bool(
            config,
            "allow_proactive_chat_context",
            False,
            fail_closed=True,
        )
        conversation_health_mode = str(
            config.get("conversation_health_mode") or "auto"
        ).strip()
        if conversation_health_mode not in {
            "auto",
            "main_model",
            "decision_model",
            "local_rules",
        }:
            logger.warning(
                "[小米运动健康] 配置项 conversation_health_mode 无效，已使用兼容模式"
            )
            conversation_health_mode = "auto"
        self.conversation_health_mode = conversation_health_mode
        self.context_decision_provider_id = str(
            config.get("context_decision_provider_id") or ""
        ).strip()
        self.context_decision_timeout_seconds = _config_int(
            config, "context_decision_timeout_seconds", 8, 3, 30
        )
        self.context_decision_message_count = _config_int(
            config, "context_decision_message_count", 8, 0, 20
        )
        self.context_decision_include_bot_messages = _config_bool(
            config, "context_decision_include_bot_messages", True
        )
        self.context_decision_prompt = str(
            config.get("context_decision_prompt") or DEFAULT_CONTEXT_DECISION_PROMPT
        ).strip()
        self.health_dialogue_provider_id = str(
            config.get("health_dialogue_provider_id") or ""
        ).strip()
        self.health_dialogue_persona_id = str(
            config.get("health_dialogue_persona_id") or ""
        ).strip()
        self.proactive_reminder_provider_id = str(
            config.get("proactive_reminder_provider_id") or ""
        ).strip()
        self.proactive_decision_prompt = str(
            config.get("proactive_decision_prompt") or DEFAULT_PROACTIVE_DECISION_PROMPT
        ).strip()
        context_source = str(
            config.get("proactive_context_source") or "conversation_history"
        ).strip()
        self.proactive_context_source = (
            context_source
            if context_source
            in {"conversation_history", "platform_message_history", "hybrid"}
            else "conversation_history"
        )
        self.proactive_context_message_count = _config_int(
            config, "proactive_context_message_count", 8, 0, 50
        )
        self.proactive_context_prompt = str(
            config.get("proactive_context_prompt") or DEFAULT_PROACTIVE_CONTEXT_PROMPT
        ).strip()
        self.proactive_context_include_bot_messages = _config_bool(
            config, "proactive_context_include_bot_messages", True
        )
        self.proactive_reminder_persona_id = str(
            config.get("proactive_reminder_persona_id") or ""
        ).strip()
        self.proactive_monitor_enabled = _config_bool(
            config, "enable_proactive_health_monitor", True
        )
        self.monitor_interval = _config_int(
            config, "health_check_interval_minutes", 30, 5, 1440
        )
        self.natural_query_sync_minutes = _config_int(
            config, "natural_query_sync_minutes", 15, 1, 120
        )
        self.natural_query_cloud_wait_seconds = _config_int(
            config, "natural_query_cloud_wait_seconds", 5, 0, 30
        )
        self.sync_days = _config_int(config, "default_sync_days", 7, 1, 90)
        self.sync_interval = _config_int(config, "sync_interval_minutes", 60, 5, 1440)
        self.monitor_service = HealthMonitorService(
            self.database,
            self.owner_platform_id,
            self.query_service.timezone,
            _config_bool(config, "enable_late_night_activity_check", True),
            str(config.get("late_night_start") or "00:30"),
            str(config.get("late_night_end") or "06:00"),
            _config_int(config, "late_night_activity_window_minutes", 45, 5, 180),
            _config_int(config, "care_cooldown_minutes", 120, 30, 10080),
            _config_int(config, "proactive_daily_limit", 3, 1, 24),
        )
        self._auto_task: asyncio.Task[None] | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._natural_refresh_task: asyncio.Task[bool] | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._owner_activity_task: asyncio.Task[None] | None = None
        self._detached_tasks: set[asyncio.Task] = set()
        self._pending_owner_activity: tuple[str, datetime] | None = None
        self._maintenance_lock = asyncio.Lock()
        self._pending_refresh_types: set[str] = set()
        self._active_refresh_types: set[str] = set()
        self._auto_sync_paused = False
        self._context_decision_failures = 0
        self._context_decision_retry_at: datetime | None = None
        self._latest_owner_message: tuple[str, str, datetime] | None = None
        self._last_proactive_delivery_at: datetime | None = None
        self._last_manual_sync_at: datetime | None = None
        self._manual_sync_min_interval = 60
        self._last_natural_cloud_request_at: dict[str, datetime] = {}
        self._natural_hard_cooldown_seconds = 60
        self._local_data_clear_in_progress = False

    async def initialize(self) -> None:
        """Migrate the database and schedule the configured background loops."""
        await self.sync_service.initialize()
        if self.sync_service.activity_timezone_reset:
            logger.warning(
                "[小米运动健康] 用户时区已变化，旧活动日汇总及其同步状态已清除；"
                "下次同步会按新时区重建近期活动数据"
            )
        self._ensure_background_task()

    def _ensure_background_task(self) -> None:
        """Start each eligible background loop without creating duplicates."""
        monitor_ready = (
            self.proactive_monitor_enabled
            and self.allow_health_data_to_llm
            and self.allow_proactive_chat_context
            and self.owner_platform_id
            and self.owner_platform_instance_id
        )
        if monitor_ready and (self._monitor_task is None or self._monitor_task.done()):
            self._monitor_task = asyncio.create_task(
                self._health_monitor_loop(), name=f"{self.name}-health-monitor"
            )
        if (
            not self._auto_sync_paused
            and self.auto_sync_enabled
            and self.user_id
            and self.pass_token
            and (self._auto_task is None or self._auto_task.done())
        ):
            self._auto_task = asyncio.create_task(
                self._auto_sync_loop(), name=f"{self.name}-auto-sync"
            )

    async def terminate(self) -> None:
        """Cancel the periodic task and close plugin-owned HTTP resources."""
        for attribute in (
            "_auto_task",
            "_monitor_task",
            "_natural_refresh_task",
            "_connection_task",
            "_owner_activity_task",
        ):
            await self._cancel_owned_task(attribute)
        await self._drain_detached_tasks()
        try:
            await await_with_hard_timeout(
                self.sync_service.close(),
                SHUTDOWN_CLOUD_CLOSE_TIMEOUT_SECONDS,
                registry=self._detached_tasks,
            )
        except TimeoutError:
            logger.warning(
                "Mi Fitness cloud cleanup exceeded the shutdown time limit; "
                "remaining cleanup will finish in the background"
            )
        except Exception as error:
            logger.warning(
                "Mi Fitness cloud cleanup failed during plugin shutdown: %s",
                redact_error(error),
            )

    async def _drain_detached_tasks(self) -> None:
        """Cancel detached work and wait briefly without making reload unbounded."""
        tasks = tuple(task for task in self._detached_tasks if not task.done())
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        _done, pending = await asyncio.wait(
            tasks,
            timeout=DETACHED_TASK_DRAIN_SECONDS,
        )
        if pending:
            logger.warning(
                "Mi Fitness shutdown left %d cancellation-resistant background task(s); "
                "their results remain isolated and will be discarded",
                len(pending),
            )

    async def _cancel_owned_task(self, attribute: str) -> None:
        """Cancel one plugin task, absorb its terminal failure, and clear its slot."""
        task = getattr(self, attribute, None)
        if task and not task.done():
            task.cancel()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                logger.warning(
                    "Mi Fitness background task cleanup failed (%s): %s",
                    attribute,
                    redact_error(error),
                )
        setattr(self, attribute, None)

    async def _auto_sync_loop(self) -> None:
        """Synchronize periodically without unbounded retries or parallel runs."""
        failures = 0
        while not self._auto_sync_paused:
            try:
                await self._sync()
                failures = 0
            except asyncio.CancelledError:
                raise
            except MiFitnessAuthenticationError as error:
                reason = redact_error(error)
                logger.warning("Mi Fitness automatic sync paused: %s", reason)
                self._auto_sync_paused = True
                break
            except Exception as error:
                failures += 1
                reason = redact_error(error)
                retry_seconds = min(
                    self.sync_interval * 60, 30 * (2 ** min(failures - 1, 5))
                )
                logger.warning(
                    "Mi Fitness automatic sync retrying after a temporary error: %s",
                    reason,
                )
                await asyncio.sleep(retry_seconds)
                continue
            await asyncio.sleep(self.sync_interval * 60)

    async def _sync(
        self, data_types: set[str] | None = None, days: int | None = None
    ) -> dict[str, object]:
        """Run one synchronized Xiaomi cloud refresh."""
        return await self.sync_service.sync(days or self.sync_days, data_types)

    async def _send_connection_result(self, session: str, text: str) -> None:
        """Send one background connection result only to the verified owner chat."""
        if not await self._is_configured_owner_private_session(session):
            logger.warning(
                "Mi Fitness connection result target failed the owner private-session check"
            )
            return
        try:
            await self.context.send_message(session, MessageChain().message(text))
        except Exception as error:
            logger.warning(
                "Mi Fitness background connection result could not be delivered (%s)",
                type(error).__name__,
            )

    async def _connection_worker(self, session: str) -> None:
        """Check Xiaomi connectivity without occupying the command pipeline."""
        current_task = asyncio.current_task()
        try:
            connected = await await_with_hard_timeout(
                self.sync_service.connect(force=True),
                CONNECTION_COMMAND_TIMEOUT_SECONDS,
                registry=self._detached_tasks,
            )
            if not connected:
                text = (
                    "健康连接失败："
                    f"{redact_error(self.adapter.last_error or '未知错误')}\n"
                    "遇到验证码、二次验证或风控时，请在浏览器完成验证后更新 Cookie。"
                )
            else:
                self._auto_sync_paused = False
                self._ensure_background_task()
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
                text = (
                    "健康连接成功\n"
                    f"区域：{self.adapter.region}\n"
                    f"可用数据：{types}\n"
                    "不显示账号、Token、Cookie 或 ssecurity。"
                )
        except TimeoutError:
            text = "健康连接检查超过 120 秒，已停止等待；请稍后重试。"
        except asyncio.CancelledError:
            raise
        except Exception as error:
            text = f"健康连接失败：{redact_error(error)}"
        try:
            await self._send_connection_result(session, text)
        finally:
            if self._connection_task is current_task:
                self._connection_task = None

    async def _health_monitor_loop(self) -> None:
        """Evaluate cached private findings at the configured bounded interval."""
        failures = 0
        while True:
            try:
                state = await asyncio.to_thread(
                    self.database.private_owner_session, self.owner_platform_id
                )
                if not state:
                    failures = 0
                    await asyncio.sleep(self.monitor_interval * 60)
                    continue
                messages: list[str] = []
                late_finding = await self.monitor_service.evaluate_late_activity()
                if late_finding:
                    messages.append(late_finding.message)
                if (
                    messages
                    and not self._proactive_delivery_cooling_down()
                    and not await self.monitor_service.proactive_cooling_down()
                    and await self._should_send_proactive_care(
                        state["session"], messages
                    )
                ):
                    body = await self._compose_proactive_reply(
                        state["session"], messages
                    )
                    sent = bool(body) and await self._send_private_message(body)
                    if sent:
                        self._last_proactive_delivery_at = datetime.now(UTC)
                        if late_finding:
                            await self.monitor_service.mark_sent(late_finding)
                        await self.monitor_service.mark_proactive_sent(body)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                failures += 1
                reason = redact_error(error)
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

    def _proactive_delivery_cooling_down(self, now: datetime | None = None) -> bool:
        """Prevent a duplicate send if durable cooldown recording fails."""
        last_delivery = self._last_proactive_delivery_at
        if last_delivery is None:
            return False
        current = now or datetime.now(UTC)
        elapsed = current - last_delivery
        return elapsed < timedelta(minutes=self.monitor_service.cooldown_minutes)

    def _access_denial_reason(self, event: AstrMessageEvent) -> str | None:
        """Explain owner, platform-instance, and private-chat failures separately."""
        message_type = event.get_message_type()
        message_type_name = str(getattr(message_type, "value", message_type or "未知"))
        return owner_access_denial_reason(
            owner_platform_id=self.owner_platform_id,
            owner_platform_instance_id=self.owner_platform_instance_id,
            sender_id=event.get_sender_id(),
            platform_id=event.get_platform_id(),
            message_type=message_type_name,
            is_private=message_type == MessageType.FRIEND_MESSAGE,
        )

    def _is_private_owner_event(self, event: AstrMessageEvent) -> bool:
        """Conversational health data is available only in the owner's private chat."""
        return self._access_denial_reason(event) is None

    def _schedule_owner_activity_touch(self, session: str, seen_at: datetime) -> None:
        """Coalesce non-critical owner-session writes outside the chat pipeline."""
        self._pending_owner_activity = (session, seen_at)
        task = getattr(self, "_owner_activity_task", None)
        if task is None or task.done():
            self._owner_activity_task = asyncio.create_task(
                self._owner_activity_worker(),
                name=f"{self.name}-owner-activity",
            )

    async def _owner_activity_worker(self) -> None:
        """Persist the newest owner activity without delaying or aborting normal chat."""
        current_task = asyncio.current_task()
        try:
            while pending := self._pending_owner_activity:
                self._pending_owner_activity = None
                session, seen_at = pending
                try:
                    await asyncio.to_thread(
                        self.database.touch_private_owner_session,
                        self.owner_platform_id,
                        session,
                        seen_at,
                        bool(self.owner_platform_instance_id),
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    logger.warning(
                        "Mi Fitness could not persist owner private activity (%s); "
                        "normal conversation will continue",
                        type(error).__name__,
                    )
        finally:
            if self._owner_activity_task is current_task:
                self._owner_activity_task = None

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def remember_owner_private_activity(self, event: AstrMessageEvent):
        """Remember private owner activity as the only evidence for being awake."""
        if self._is_private_owner_event(event):
            seen_at = datetime.now(UTC)
            if self.allow_proactive_chat_context:
                message = " ".join(str(event.get_message_str() or "").split())[:600]
                if message:
                    self._latest_owner_message = (
                        event.unified_msg_origin,
                        message,
                        seen_at,
                    )
            else:
                self._latest_owner_message = None
            self._schedule_owner_activity_touch(
                event.unified_msg_origin,
                seen_at,
            )

    @filter.on_llm_request()
    async def add_owner_health_context(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """Route private health context without blocking unrelated conversations."""
        # LLM context can influence free-form replies, so it is stricter than
        # command authorization and never carries health data into a group.
        if (
            not self.care_dialogue_enabled
            or not self.allow_health_data_to_llm
            or not self._is_private_owner_event(event)
        ):
            return
        raw_question = self._decision_context_text(event.get_message_str())
        question = self._sanitize_focus(raw_question) if raw_question else ""
        if not question:
            return
        mode = self._effective_conversation_health_mode()
        if mode == "main_model":
            add_private_health_tool(
                req,
                self._load_main_model_private_context,
                self.context_decision_prompt,
            )
            return

        wait_seconds = float(self.natural_query_cloud_wait_seconds)
        force_refresh = self._wants_fresh_cloud_data(question)
        if mode == "decision_model":
            if not self.context_decision_provider_id:
                return
            use_data, focus = await self._decide_context_focus(
                event.unified_msg_origin,
                question,
                self._decision_history_from_request(req, question),
            )
        else:
            use_data, focus = self._fallback_context_decision(question)
        if not use_data:
            return
        message_focus = self.query_service.normalize_llm_focus(question)
        focus = message_focus or self.query_service.normalize_llm_focus(focus)
        if not focus:
            return
        focus = self._normalize_context_focus_for_message(question, focus)
        health_question = self._is_health_question(question)
        await self._refresh_for_natural_question(
            focus,
            wait_for_result=wait_seconds > 0,
            force_refresh=force_refresh,
            wait_timeout=max(wait_seconds, 0.001),
        )
        snapshot = await self.query_service.llm_care_snapshot(
            focus,
            include_missing_notice=False,
        )
        if not snapshot:
            return
        last_sync = await self.query_service.sync_at_for_focus(focus)
        displayed_last_sync = (
            self.query_service.display_timestamp(last_sync) if last_sync else None
        )
        dialogue = await self._compose_health_dialogue(
            event.unified_msg_origin,
            focus,
            snapshot,
            displayed_last_sync,
        )
        text = self._build_private_life_context(
            snapshot,
            displayed_last_sync,
            dialogue,
            health_question=health_question,
        )
        self._append_temporary_context(req, text)

    @filter.on_agent_done()
    async def scrub_owner_health_tool_history(
        self,
        event: AstrMessageEvent,
        run_context: ContextWrapper[AstrAgentContext],
        response: object,
    ) -> None:
        """Remove temporary health-tool messages before AstrBot saves the turn."""
        del event, response
        scrub_private_health_tool_messages(run_context.messages)

    async def _guard(self, event: AstrMessageEvent):
        """Require the configured owner and a private chat for all health commands."""
        denial_reason = self._access_denial_reason(event)
        if denial_reason is None:
            return
        yield event.plain_result(denial_reason)
