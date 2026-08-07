"""Credential redaction tests use synthetic values only."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from astrbot_plugin_mi_fitness_health.utils.privacy import redact_error


class PrivacyTest(unittest.TestCase):
    def test_release_requires_temporary_context_capable_astrbot(self) -> None:
        metadata = (Path(__file__).parents[1] / "metadata.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("version: v0.8.5", metadata)
        self.assertIn('astrbot_version: ">=4.24.2,<5"', metadata)

    def test_sensitive_llm_authorization_defaults_to_false_in_schema(self) -> None:
        schema = json.loads(
            (Path(__file__).parents[1] / "_conf_schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(schema["allow_health_data_to_llm"]["default"], False)
        self.assertIs(schema["allow_proactive_chat_context"]["default"], False)
        self.assertEqual(
            schema["context_decision_provider_id"]["_special"], "select_provider"
        )
        self.assertEqual(schema["context_decision_provider_id"]["default"], "")
        self.assertEqual(schema["conversation_health_mode"]["default"], "auto")
        self.assertIn(
            "main_model",
            schema["conversation_health_mode"]["options"],
        )
        self.assertEqual(
            schema["natural_query_cloud_wait_seconds"]["slider"]["min"],
            0,
        )
        self.assertEqual(schema["context_decision_prompt"]["type"], "text")
        self.assertTrue(schema["context_decision_prompt"]["default"])
        self.assertEqual(schema["proactive_decision_prompt"]["type"], "text")
        self.assertIn(
            "拿不准时不要发送", schema["proactive_decision_prompt"]["default"]
        )
        self.assertEqual(
            schema["proactive_context_source"]["options"],
            [
                "conversation_history",
                "platform_message_history",
                "hybrid",
            ],
        )
        self.assertEqual(schema["proactive_context_message_count"]["default"], 8)
        self.assertEqual(schema["proactive_context_message_count"]["slider"]["max"], 50)
        self.assertEqual(schema["proactive_context_prompt"]["type"], "text")
        self.assertIn(
            "{{context_lines}}", schema["proactive_context_prompt"]["default"]
        )
        self.assertIs(schema["proactive_context_include_bot_messages"]["default"], True)
        self.assertIs(schema["enable_auto_sync"]["default"], False)
        self.assertIn(
            "生活数据摘要",
            schema["health_dialogue_provider_id"]["hint"],
        )
        self.assertIn(
            "处理或保存",
            schema["health_dialogue_provider_id"]["hint"],
        )

    def test_sqlite_health_cache_artifacts_are_ignored(self) -> None:
        patterns = set(
            (Path(__file__).parents[1] / ".gitignore")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        for suffix in ("sqlite3", "sqlite", "db"):
            self.assertIn(f"*.{suffix}", patterns)
            self.assertIn(f"*.{suffix}-wal", patterns)
            self.assertIn(f"*.{suffix}-shm", patterns)
            self.assertIn(f"*.{suffix}-journal", patterns)

    def test_common_xiaomi_and_provider_secrets_are_redacted(self) -> None:
        synthetic_secret = "synthetic-secret-value"
        samples = (
            f"serviceToken={synthetic_secret}",
            f'"accessToken":"{synthetic_secret}"',
            f"Authorization: Bearer {synthetic_secret}",
            f"Set-Cookie: serviceToken={synthetic_secret}",
            f"Cookie: cUserId=synthetic-id; passToken={synthetic_secret}",
            f"_nonce={synthetic_secret}&signature={synthetic_secret}",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_error(sample)
                self.assertNotIn(synthetic_secret, redacted)
                self.assertNotIn("synthetic-id", redacted)

    def test_api_keys_and_common_provider_token_formats_are_redacted(self) -> None:
        samples = (
            "api_key=sk-synthetic-secret-value",
            '"apiKey":"AIzaSyntheticSecretValue123456"',
            "x-api-key: ghp_syntheticSecretValue1234567890",
            "client_secret=synthetic-secret-value",
            "private_key=synthetic-private-key-value",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_error(sample)
                self.assertNotIn("synthetic", redacted.lower())

    def test_urls_controls_and_newlines_never_reach_status_text(self) -> None:
        redacted = redact_error(
            "failure\nhttps://example.invalid/path?serviceToken=synthetic\r\nnext"
        )
        self.assertNotIn("https://", redacted)
        self.assertNotIn("\n", redacted)
        self.assertNotIn("\r", redacted)
