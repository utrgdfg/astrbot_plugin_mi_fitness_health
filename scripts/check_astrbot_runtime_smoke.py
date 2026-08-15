"""Smoke-test plugin import and privacy isolation against a real AstrBot tree."""

from __future__ import annotations

import importlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace


def _check_grouped_config_migration() -> None:
    """Verify one real AstrBotConfig upgrade from the v1.0.3 flat layout."""
    from astrbot.core.config.astrbot_config import AstrBotConfig

    layout = importlib.import_module(
        "astrbot_plugin_mi_fitness_health.utils.config_layout"
    )
    repository_root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (repository_root / "_conf_schema.json").read_text(encoding="utf-8")
    )
    legacy_values = {
        "user_id": "synthetic-xiaomi-user",
        "pass_token": "synthetic-pass-token",
        "owner_platform_id": "synthetic-owner",
        "owner_platform_instance_id": "synthetic-bot",
        "region": "cn",
        "user_timezone": "Asia/Shanghai",
        "allow_health_data_to_llm": True,
        "allow_proactive_chat_context": True,
        "conversation_health_mode": "decision_model",
        "context_decision_timeout_seconds": 12,
        "context_decision_platform_history_timeout_seconds": 7,
        "enable_auto_sync": True,
        "sync_interval_minutes": 90,
        "database_path": "",
    }
    with tempfile.TemporaryDirectory() as directory:
        config_path = Path(directory) / "plugin_config.json"
        config_path.write_text(
            json.dumps(legacy_values, ensure_ascii=False), encoding="utf-8"
        )
        config = AstrBotConfig(config_path=str(config_path), schema=schema)
        view = layout.migrate_grouped_config(config)
        for key, expected in legacy_values.items():
            if view.get(key) != expected:
                raise AssertionError(f"legacy config value was not migrated: {key}")

        reloaded = AstrBotConfig(config_path=str(config_path), schema=schema)
        reloaded_view = layout.migrate_grouped_config(reloaded)
        for key, expected in legacy_values.items():
            if reloaded_view.get(key) != expected:
                raise AssertionError(f"grouped config value was not retained: {key}")


def main() -> int:
    """Import the plugin, install the guard, and verify one fallback scrub."""
    plugin = importlib.import_module("astrbot_plugin_mi_fitness_health.main")
    if plugin.MiFitnessHealthPlugin.__name__ != "MiFitnessHealthPlugin":
        raise AssertionError("plugin entrypoint import returned an unexpected class")

    guard = importlib.import_module(
        "astrbot_plugin_mi_fitness_health.compat.runner_privacy_guard"
    )
    if not guard.install_private_context_guard():
        raise AssertionError("real AstrBot runner privacy guard could not be installed")

    canary = (
        '<private_life_context source="mi_fitness">\n'
        "SYNTHETIC-PRIVATE-CANARY\n"
        "</private_life_context>"
    )
    request = SimpleNamespace(
        extra_user_content_parts=[SimpleNamespace(text=canary)],
        func_tool=None,
    )
    guard.isolate_private_context_request(request)
    raw_tool_set = request.func_tool
    if not raw_tool_set or not raw_tool_set.empty() or raw_tool_set.tools:
        raise AssertionError("isolated request exposed an ordinary tool schema")

    runner = SimpleNamespace(
        req=request,
        run_context=SimpleNamespace(messages=[{"role": "user", "content": canary}]),
        _skill_like_raw_tool_set=raw_tool_set,
    )
    if not guard._is_private_no_tool_request(runner):
        raise AssertionError("isolated request marker was not recognized")
    guard._strip_private_context_before_fallback(runner)

    rendered = repr(request.extra_user_content_parts) + repr(
        runner.run_context.messages
    )
    if "SYNTHETIC-PRIVATE-CANARY" in rendered:
        raise AssertionError("fallback scrub retained private context")
    if request.func_tool is not None:
        raise AssertionError("fallback scrub retained the isolated tool set")

    _check_grouped_config_migration()

    print("AstrBot runtime smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
