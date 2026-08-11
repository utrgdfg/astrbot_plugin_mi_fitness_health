"""Background lifecycle, cloud connection, and periodic task coordination."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from astrbot.api import logger
from astrbot.api.event import MessageChain

from ..adapters import MiFitnessAuthenticationError
from ..utils.async_tools import await_cancellation_safe, await_with_hard_timeout
from ..utils.privacy import redact_error

CONNECTION_COMMAND_TIMEOUT_SECONDS = 120.0
DETACHED_TASK_DRAIN_SECONDS = 1.0
SHUTDOWN_CLOUD_CLOSE_TIMEOUT_SECONDS = 5.0
BACKGROUND_SEND_TIMEOUT_SECONDS = 20.0


class RuntimeCoordinationMixin:
    """Coordinate plugin-owned tasks without defining AstrBot entrypoints."""

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
        if self._terminating or self._terminated:
            return
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
        self._terminating = True
        await self._cancel_foreground_operations()
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
        finally:
            self._terminated = True

    def _begin_foreground_operation(self) -> asyncio.Task | None:
        """Register a command task so reload cannot leave an old writer alive."""
        if self._terminating or self._terminated:
            return None
        task = asyncio.current_task()
        if task is None:
            return None
        self._foreground_tasks.add(task)
        return task

    def _end_foreground_operation(self, task: asyncio.Task) -> None:
        """Release one command task from the reload barrier."""
        self._foreground_tasks.discard(task)

    async def _cancel_foreground_operations(self) -> None:
        """Cancel and fully drain plugin command writers before unloading."""
        current = asyncio.current_task()
        tasks = tuple(
            task
            for task in self._foreground_tasks
            if task is not current and not task.done()
        )
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._foreground_tasks.difference_update(tasks)

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
        if self._terminating or self._terminated:
            return
        if not await self._is_configured_owner_private_session(session):
            logger.warning(
                "Mi Fitness connection result target failed the owner private-session check"
            )
            return
        try:
            await await_with_hard_timeout(
                self.context.send_message(session, MessageChain().message(text)),
                BACKGROUND_SEND_TIMEOUT_SECONDS,
                registry=self._detached_tasks,
            )
        except TimeoutError:
            logger.warning("Mi Fitness background connection result delivery timed out")
        except Exception as error:
            logger.warning(
                "Mi Fitness background connection result could not be delivered (%s)",
                type(error).__name__,
            )

    async def _connection_worker(self, session: str) -> None:
        """Check Xiaomi connectivity without occupying the command pipeline."""
        current_task = asyncio.current_task()
        try:
            if self._terminating or self._terminated:
                return
            await await_cancellation_safe(
                asyncio.to_thread(
                    self.database.touch_private_owner_session,
                    self.owner_platform_id,
                    session,
                    None,
                    True,
                )
            )
            if self._terminating or self._terminated:
                return
            cloud_task = self._connection_cloud_task
            if cloud_task is None or cloud_task.done():
                cloud_task = asyncio.create_task(
                    self.sync_service.connect(force=True),
                    name=f"{self.name}-connection-cloud",
                )
                self._connection_cloud_task = cloud_task

                def clear_cloud_slot(done: asyncio.Task[bool]) -> None:
                    if self._connection_cloud_task is done:
                        self._connection_cloud_task = None

                cloud_task.add_done_callback(clear_cloud_slot)
            connected = await await_with_hard_timeout(
                cloud_task,
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
                    if body:
                        attempted_at = datetime.now(UTC)
                        # Reserve both in-memory and durable cooldowns before the
                        # platform send. A reload or lost acknowledgement may
                        # suppress one message, but can never produce duplicates.
                        self._last_proactive_delivery_at = attempted_at
                        reservation_acquired = True
                        if late_finding:
                            reservation_acquired = await self.monitor_service.mark_sent(
                                late_finding,
                                attempted_at,
                                delivery_confirmed=False,
                            )
                        if reservation_acquired:
                            await self.monitor_service.mark_proactive_sent(
                                body,
                                attempted_at,
                                delivery_confirmed=False,
                            )
                            delivery = await self._send_private_message(body)
                            if delivery is True:
                                if late_finding:
                                    await self.monitor_service.confirm_sent(
                                        late_finding, attempted_at
                                    )
                                await self.monitor_service.confirm_proactive_sent(
                                    attempted_at
                                )
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
