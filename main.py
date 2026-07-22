"""AstrBot entry point for private Xiaomi Mi Fitness cloud data."""

from __future__ import annotations

import os
from pathlib import Path

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .adapters import MiFitnessCloudAdapter


class MiFitnessHealthPlugin(Star):
    """Own the plugin lifecycle and protect private health commands."""

    def __init__(self, context: Context, config: AstrBotConfig):
        """Initialize the minimal, safe plugin shell.

        Args:
            context: AstrBot runtime context.
            config: Plugin configuration supplied by AstrBot.
        """
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(self.name))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.user_id = str(config.get("user_id") or os.getenv("MI_FITNESS_USER_ID", "")).strip()
        self.pass_token = str(
            config.get("pass_token") or os.getenv("MI_FITNESS_PASS_TOKEN", "")
        ).strip()
        self.owner_platform_id = str(config.get("owner_platform_id") or "").strip()
        self.adapter = MiFitnessCloudAdapter(
            user_id=self.user_id,
            pass_token=self.pass_token,
            region=str(config.get("region") or "").strip(),
        )

    async def initialize(self) -> None:
        """Prepare the plugin without network activity before an owner requests it."""

    async def terminate(self) -> None:
        """Release the adapter's HTTP resources during unload or reload."""
        await self.adapter.close()

    def _authorized(self, event: AstrMessageEvent) -> bool:
        """Return whether the sender is the configured data owner.

        Args:
            event: Incoming AstrBot message event.

        Returns:
            True only for the configured owner.
        """
        return bool(self.owner_platform_id) and str(event.get_sender_id()) == self.owner_platform_id

    async def _owner_message(self, event: AstrMessageEvent):
        """Yield a privacy-preserving refusal for an unauthorized health request.

        Args:
            event: Incoming AstrBot message event.
        """
        if not self.owner_platform_id:
            yield event.plain_result("健康数据所有者尚未配置，请由管理员设置 owner_platform_id。")
        else:
            yield event.plain_result("此健康数据仅允许已配置的所有者查询。")

    @filter.command("健康帮助")
    async def health_help(self, event: AstrMessageEvent):
        """Show safe setup guidance without exposing credential values."""
        yield event.plain_result(
            "小米运动健康插件（阶段 1）\n"
            "请由管理员在插件配置中填写 user_id、pass_token 和 owner_platform_id。\n"
            "pass_token 属于敏感凭证，请勿发送到聊天或日志。\n"
            "支持命令：健康连接、健康状态、今日健康、健康同步、心率记录、身体数据、健康趋势 7。\n"
            "数据来自已同步到小米健康云的历史记录，不是实时监护，也不构成医疗诊断。"
        )

    @filter.command("健康连接")
    async def health_connection(self, event: AstrMessageEvent):
        """Display a credential-safe connection setup status."""
        if not self._authorized(event):
            async for result in self._owner_message(event):
                yield result
            return
        if not self.user_id or not self.pass_token:
            yield event.plain_result(
                "健康连接：未配置凭证。请在 AstrBot 插件配置页填写 user_id 和 pass_token，"
                "然后重新加载插件。不会显示账号、Token、Cookie 或 ssecurity。"
            )
            return
        connected = await self.adapter.connect()
        if not connected:
            yield event.plain_result(
                "健康连接失败：" + (self.adapter.last_error or "未知错误")
                + "\n若出现验证码、二次验证或风控，请在浏览器完成验证后重新获取 Cookie；插件不会尝试绕过。"
            )
            return
        type_labels = {
            "daily_activity": "步数/距离/活动消耗",
            "heart_rate": "心率",
            "body_measurements": "体重/身体成分",
        }
        available = self.adapter.get_available_data_types()
        types = "、".join(type_labels[item] for item in available) if available else "未发现最近 30 天数据"
        yield event.plain_result(
            f"健康连接成功\n区域：{self.adapter.region}\n可用数据：{types}\n"
            "云端数据为已同步的历史记录；当前仅完成连接与数据类型探测，后续同步会显示采集时间。"
        )
