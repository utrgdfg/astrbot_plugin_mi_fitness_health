"""Credential redaction tests use synthetic values only."""

from __future__ import annotations

import ast
import json
import re
import unittest
from pathlib import Path

from astrbot_plugin_mi_fitness_health.utils.privacy import redact_error


class PrivacyTest(unittest.TestCase):
    def test_release_requires_temporary_context_capable_astrbot(self) -> None:
        metadata = (Path(__file__).parents[1] / "metadata.yaml").read_text(
            encoding="utf-8"
        )
        version_match = re.search(
            r"^version:\s*"
            r"(v\d+\.\d+\.\d+"
            r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)"
            r"\s*$",
            metadata,
            re.M,
        )
        self.assertIsNotNone(version_match)
        changelog = (Path(__file__).parents[1] / "CHANGELOG.md").read_text(
            encoding="utf-8"
        )
        self.assertIn(f"## [{version_match.group(1)}]", changelog)
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
        self.assertEqual(
            schema["context_decision_context_source"]["options"],
            [
                "conversation_history",
                "platform_message_history",
                "hybrid",
            ],
        )
        self.assertEqual(
            schema["context_decision_context_source"]["default"],
            "conversation_history",
        )
        self.assertEqual(
            schema["context_decision_platform_history_timeout_seconds"]["default"],
            3,
        )
        self.assertEqual(
            schema["context_decision_platform_history_timeout_seconds"]["slider"],
            {"min": 1, "max": 15, "step": 1},
        )
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
        self.assertIn(
            "判断表达的实际含义", schema["context_decision_prompt"]["default"]
        )
        self.assertIn("头晕", schema["context_decision_prompt"]["default"])
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

    def test_context_decision_prompt_default_matches_runtime_constant(self) -> None:
        root = Path(__file__).parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        module = ast.parse(
            (root / "features" / "conversation_routing.py").read_text(encoding="utf-8")
        )
        runtime_default = None
        for node in module.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(
                isinstance(target, ast.Name)
                and target.id == "DEFAULT_CONTEXT_DECISION_PROMPT"
                for target in node.targets
            ):
                runtime_default = ast.literal_eval(node.value)
                break

        self.assertIsNotNone(runtime_default)
        self.assertEqual(
            schema["context_decision_prompt"]["default"],
            runtime_default,
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

    def test_common_private_artifacts_are_ignored(self) -> None:
        patterns = set(
            (Path(__file__).parents[1] / ".gitignore")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        for pattern in (
            ".env",
            ".env.*",
            "*.log",
            "*.jpg",
            "*.zip",
            "Screenshot*",
            "截图*",
        ):
            self.assertIn(pattern, patterns)

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

    def test_paths_emails_sessions_and_identifier_values_are_redacted(self) -> None:
        samples = (
            r"cannot open X:\synthetic-user\health.sqlite3",
            r"cannot open \\synthetic-host\synthetic-share\health.sqlite3",
            "cannot open /home/synthetic-user/health.sqlite3",
            "owner@example.invalid failed",
            "session=qq:FriendMessage:synthetic-owner",
            "user_id=synthetic-user owner_platform_id=synthetic-owner",
            "yetAnotherServiceToken=synthetic-secret-value",
            "password=synthetic-password token=synthetic-token",
            "id_token=synthetic-secret auth_token=synthetic-secret",
            "db_password=synthetic-secret session_cookie=synthetic-secret",
            "sessionId=synthetic-session owner_id=synthetic-owner",
            "provider_id=synthetic-provider personaId=synthetic-persona",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                redacted = redact_error(sample)
                self.assertNotIn("private", redacted.lower())
                self.assertNotIn("synthetic", redacted.lower())
                self.assertNotIn("example.invalid", redacted.lower())

    def test_redaction_preserves_non_sensitive_error_reason(self) -> None:
        message = "temporary timeout while reading response; status 503"
        self.assertEqual(redact_error(message), message)
        self.assertIn(
            "permission denied",
            redact_error(r"open C:\Users\synthetic-user\secret.db: permission denied"),
        )
        self.assertIn(
            "permission denied",
            redact_error("open /usr/local/private/data.db: permission denied"),
        )
        self.assertEqual(redact_error("token expired"), "token expired")
        self.assertEqual(
            redact_error("password authentication failed"),
            "password authentication failed",
        )
        self.assertEqual(redact_error("cookie jar is empty"), "cookie jar is empty")
        self.assertEqual(
            redact_error("session closed by peer"), "session closed by peer"
        )
        self.assertEqual(redact_error("owner check failed"), "owner check failed")

    def test_urls_controls_and_newlines_never_reach_status_text(self) -> None:
        redacted = redact_error(
            "failure\nhttps://example.invalid/path?serviceToken=synthetic\r\nnext"
        )
        self.assertNotIn("https://", redacted)
        self.assertNotIn("\n", redacted)
        self.assertNotIn("\r", redacted)
