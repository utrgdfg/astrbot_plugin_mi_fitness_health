"""Backward-compatible access for the grouped AstrBot plugin configuration."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any

from astrbot.api import logger

CONFIG_LAYOUT_VERSION_KEY = "_config_layout_version"
CURRENT_CONFIG_LAYOUT_VERSION = 1

CONFIG_GROUPS: dict[str, tuple[str, ...]] = {
    "account": (
        "user_id",
        "pass_token",
        "owner_platform_id",
        "owner_platform_instance_id",
        "region",
        "user_timezone",
    ),
    "privacy": (
        "allow_health_data_to_llm",
        "allow_proactive_chat_context",
    ),
    "conversation_routing": (
        "enable_care_dialogue",
        "conversation_health_mode",
        "context_decision_provider_id",
        "context_decision_timeout_seconds",
        "context_decision_context_source",
        "context_decision_platform_history_timeout_seconds",
        "context_decision_message_count",
        "context_decision_include_bot_messages",
        "context_decision_prompt",
    ),
    "conversation_delivery": (
        "health_dialogue_provider_id",
        "health_dialogue_persona_id",
        "natural_query_sync_minutes",
        "natural_query_cloud_wait_seconds",
    ),
    "proactive_context": (
        "enable_proactive_health_monitor",
        "proactive_reminder_provider_id",
        "proactive_reminder_persona_id",
        "proactive_decision_prompt",
        "proactive_context_source",
        "proactive_context_message_count",
        "proactive_context_prompt",
        "proactive_context_include_bot_messages",
    ),
    "proactive_timing": (
        "health_check_interval_minutes",
        "enable_late_night_activity_check",
        "late_night_start",
        "late_night_end",
        "late_night_activity_window_minutes",
        "care_cooldown_minutes",
        "proactive_daily_limit",
    ),
    "sync_storage": (
        "enable_auto_sync",
        "sync_interval_minutes",
        "default_sync_days",
        "data_retention_days",
        "database_path",
    ),
}

CONFIG_KEY_TO_GROUP = {
    key: group_name for group_name, keys in CONFIG_GROUPS.items() for key in keys
}


class GroupedConfigView:
    """Read grouped values while retaining a safe legacy fallback."""

    def __init__(self, source: Mapping[str, Any]) -> None:
        self._source = source

    def get(self, key: str, default: Any = None) -> Any:
        """Return a grouped value, falling back to its hidden legacy key."""
        group_name = CONFIG_KEY_TO_GROUP.get(key)
        if group_name:
            group = self._source.get(group_name)
            if isinstance(group, Mapping) and key in group:
                return group.get(key, default)
        return self._source.get(key, default)


def migrate_grouped_config(config: MutableMapping[str, Any]) -> GroupedConfigView:
    """Copy a pre-grouped layout into its new cards exactly once.

    AstrBot validates configuration against the new schema before plugin
    construction. Hidden legacy fields therefore remain in the schema for one
    compatibility mirror, allowing upgrades and temporary downgrades to preserve
    every value. No value is logged, and a failed save is retried on the next load.
    """
    raw_version = config.get(CONFIG_LAYOUT_VERSION_KEY, 0)
    try:
        version = int(raw_version)
    except (TypeError, ValueError, OverflowError):
        version = 0

    needs_save = False
    if version < CURRENT_CONFIG_LAYOUT_VERSION:
        for group_name, keys in CONFIG_GROUPS.items():
            existing_group = config.get(group_name)
            group = dict(existing_group) if isinstance(existing_group, Mapping) else {}
            for key in keys:
                if key in config:
                    group[key] = config[key]
            config[group_name] = group
        config[CONFIG_LAYOUT_VERSION_KEY] = CURRENT_CONFIG_LAYOUT_VERSION
        needs_save = True

    # Keep invisible legacy keys synchronized so a temporary downgrade does not
    # restore stale credentials or switches. Grouped values remain authoritative.
    for group_name, keys in CONFIG_GROUPS.items():
        group = config.get(group_name)
        if not isinstance(group, Mapping):
            continue
        for key in keys:
            if key in group and config.get(key) != group[key]:
                config[key] = group[key]
                needs_save = True

    if needs_save:
        save_config = getattr(config, "save_config", None)
        if callable(save_config):
            try:
                save_config()
            except Exception as error:
                logger.warning(
                    "[小米运动健康] 分组配置迁移暂未写入磁盘，将在下次加载时重试 (%s)",
                    type(error).__name__,
                )

    return GroupedConfigView(config)
