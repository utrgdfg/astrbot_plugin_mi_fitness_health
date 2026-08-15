"""Regression tests for lossless migration to grouped AstrBot configuration."""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

import astrbot_test_stub  # noqa: F401
from astrbot.api import logger
from astrbot_plugin_mi_fitness_health.utils.config_layout import (
    CONFIG_GROUPS,
    CONFIG_LAYOUT_VERSION_KEY,
    CURRENT_CONFIG_LAYOUT_VERSION,
    migrate_grouped_config,
)


class _SavingConfig(dict[str, Any]):
    def __init__(self, values: dict[str, Any], *, fail_save: bool = False) -> None:
        super().__init__(values)
        self.fail_save = fail_save
        self.save_calls = 0

    def save_config(self) -> None:
        self.save_calls += 1
        if self.fail_save:
            raise RuntimeError("synthetic save failure")


class ConfigLayoutTest(unittest.TestCase):
    @staticmethod
    def _schema() -> dict[str, dict[str, Any]]:
        return json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )

    @staticmethod
    def _synthetic_value(key: str, schema: dict[str, Any], index: int) -> Any:
        value_type = schema["type"]
        default = schema.get("default")
        if value_type == "bool":
            return not bool(default)
        if value_type == "int":
            return int(default or 0) + 1
        options = schema.get("options")
        if isinstance(options, list) and options:
            return options[-1]
        if key == "pass_token":
            return "synthetic-pass-token"
        return f"synthetic-{key}-{index}"

    def test_schema_exposes_only_seven_groups_and_hides_legacy_fields(self) -> None:
        schema = self._schema()
        visible = [
            key for key, value in schema.items() if not value.get("invisible", False)
        ]
        self.assertEqual(visible, list(CONFIG_GROUPS))
        self.assertTrue(schema[CONFIG_LAYOUT_VERSION_KEY]["invisible"])
        self.assertEqual(schema[CONFIG_LAYOUT_VERSION_KEY]["default"], 0)

        grouped_keys: list[str] = []
        for group_name, expected_keys in CONFIG_GROUPS.items():
            group = schema[group_name]
            self.assertEqual(group["type"], "object")
            self.assertEqual(tuple(group["items"]), expected_keys)
            grouped_keys.extend(group["items"])

        self.assertEqual(len(grouped_keys), 41)
        self.assertEqual(len(set(grouped_keys)), 41)
        for key in grouped_keys:
            self.assertTrue(schema[key]["invisible"])
            group_name = next(
                name for name, keys in CONFIG_GROUPS.items() if key in keys
            )
            expected = dict(schema[key])
            expected.pop("invisible")
            self.assertEqual(schema[group_name]["items"][key], expected)

    def test_all_legacy_values_are_migrated_without_conversion(self) -> None:
        schema = self._schema()
        legacy: dict[str, Any] = {}
        for index, key in enumerate(
            (key for keys in CONFIG_GROUPS.values() for key in keys),
            start=1,
        ):
            legacy[key] = self._synthetic_value(key, schema[key], index)
        original = dict(legacy)
        config = _SavingConfig(legacy)

        view = migrate_grouped_config(config)

        self.assertEqual(config.save_calls, 1)
        self.assertEqual(
            config[CONFIG_LAYOUT_VERSION_KEY], CURRENT_CONFIG_LAYOUT_VERSION
        )
        for group_name, keys in CONFIG_GROUPS.items():
            for key in keys:
                self.assertEqual(config[group_name][key], original[key])
                self.assertEqual(view.get(key), original[key])

    def test_migration_is_idempotent_and_grouped_values_are_authoritative(self) -> None:
        config = _SavingConfig(
            {
                CONFIG_LAYOUT_VERSION_KEY: CURRENT_CONFIG_LAYOUT_VERSION,
                "account": {"user_id": "synthetic-grouped-user"},
                "user_id": "synthetic-stale-legacy-user",
            }
        )

        first = migrate_grouped_config(config)
        second = migrate_grouped_config(config)

        self.assertEqual(first.get("user_id"), "synthetic-grouped-user")
        self.assertEqual(second.get("user_id"), "synthetic-grouped-user")
        self.assertEqual(config["user_id"], "synthetic-grouped-user")
        self.assertEqual(config.save_calls, 1)

    def test_failed_save_keeps_runtime_values_and_never_logs_credentials(self) -> None:
        secret = "synthetic-private-pass-token"
        config = _SavingConfig(
            {"pass_token": secret, "user_id": "synthetic-user"}, fail_save=True
        )
        logger.warning.reset_mock()

        view = migrate_grouped_config(config)

        self.assertEqual(view.get("pass_token"), secret)
        rendered_log = repr(logger.warning.call_args)
        self.assertNotIn(secret, rendered_log)
        self.assertIn("RuntimeError", rendered_log)


if __name__ == "__main__":
    unittest.main()
